"""Telegram alerts + bot commands over the Bot HTTP API (https://core.telegram.org/bots/api).

* Alerts: signals with score >= TELEGRAM_MIN_SCORE are sent to TELEGRAM_CHAT_ID. Each signal is claimed
  with a conditional UPDATE before sending, so several API replicas never double-post (at-most-once).
* Commands (long polling, any chat that messages the bot):
    /top [chain]     top 5 tokens by Alpha Score
    /token <address> score card for a token on any chain
    /status          scanner health and 24h KPIs
    /help

Runs inside the API process (see main.py lifespan) or standalone:  python -m app.telegram_alerts
"""

from __future__ import annotations

import asyncio
import html
import re
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select, update

from app.config import Settings, get_settings
from app.db import SessionLocal, init_db
from app.models import Badge, Chain, Signal, SmartWallet, Token, TrackerCursor, WalletTrade
from app.repository import get_token, list_tokens, scanner_status, token_detail, utc
from app.schemas import LiveSignal, SortField, TokenListParams

log = logging.getLogger("telegram")

BADGE_ICON = {Badge.strong_alpha: "🟢", Badge.moderate: "🟠", Badge.high_risk: "🔴"}
BADGE_TEXT = {Badge.strong_alpha: "STRONG ALPHA", Badge.moderate: "MODERATE", Badge.high_risk: "HIGH RISK"}
BACKLOG_MINUTES = 10   # on startup, don't post signals older than this
SEND_INTERVAL = 1.1    # Telegram allows ~1 msg/s per chat
MENU_LIMIT = 30        # wallets per /wallets menu
SLASHLESS = {"track", "untrack", "tracked", "wallets", "help", "top", "status", "token", "performance", "paper", "live", "sell"}


def fmt_usd(v: float | None) -> str:
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1e9:
        return f"${v / 1e9:.2f}B"
    if a >= 1e6:
        return f"${v / 1e6:.2f}M"
    if a >= 1e3:
        return f"${v / 1e3:.1f}K"
    if a >= 1:
        return f"${v:,.2f}"
    if a == 0:
        return "$0"
    # small prices: keep 4 significant digits
    return f"${v:.{max(4, -int(f'{a:e}'.split('e')[1]) + 3)}f}"


def fmt_pct(v: float | None) -> str:
    return "—" if v is None else f"{v:+.1f}%"


def e(s: Any) -> str:
    return html.escape(str(s), quote=False)


def render_signal(sig: LiveSignal, dashboard_url: str | None) -> str:
    lines = [
        f"{BADGE_ICON[sig.badge]} <b>{BADGE_TEXT[sig.badge]}</b> · <b>{sig.score}/100</b>",
        f"<b>${e(sig.symbol)}</b> — {e(sig.name)}  <i>({e(sig.chain.value)})</i>",
        "",
        f"Price {fmt_usd(sig.price_usd)} · 1h {fmt_pct(sig.price_change_h1)}",
        f"Liq {fmt_usd(sig.liquidity_usd)} · Vol 1h {fmt_usd(sig.volume_h1)} · MC {fmt_usd(sig.market_cap)}",
    ]
    if sig.top_reasons:
        lines.append("")
        lines.extend(f"• {e(r)}" for r in sig.top_reasons[:3])
    lines += ["", f"<code>{e(sig.address)}</code>"]
    links = []
    if sig.pair_url:
        links.append(f'<a href="{e(sig.pair_url)}">DexScreener</a>')
    if dashboard_url:
        links.append(f'<a href="{e(dashboard_url.rstrip("/"))}/token/{sig.chain.value}/{e(sig.address)}">Inspector</a>')
    if links:
        lines.append(" · ".join(links))
    lines.append("<i>Automated signal, not financial advice.</i>")
    return "\n".join(lines)


EXPLORER_TX = {
    Chain.solana: "https://solscan.io/tx/{}", Chain.ethereum: "https://etherscan.io/tx/{}",
    Chain.base: "https://basescan.org/tx/{}", Chain.bsc: "https://bscscan.com/tx/{}",
}
EXPLORER_WALLET = {
    Chain.solana: "https://solscan.io/account/{}", Chain.ethereum: "https://etherscan.io/address/{}",
    Chain.base: "https://basescan.org/address/{}", Chain.bsc: "https://bscscan.com/address/{}",
}


def render_trade(t: WalletTrade, dashboard_url: str | None) -> str:
    who = t.wallet_label or f"{t.wallet[:4]}…{t.wallet[-4:]}"
    if t.side == "sell":
        if t.pct_sold is not None and t.pct_sold >= 99:
            how = "<b>FULL EXIT</b>"
        elif t.pct_sold is not None:
            how = f"sold <b>{t.pct_sold:.0f}%</b> of position"
        else:
            how = "partial sell"
        if t.pnl_usd is not None:
            icon = "🟢" if t.pnl_usd >= 0 else "🔴"
            sign = "+" if t.pnl_usd >= 0 else "-"
            pnl = (f"{icon} P&amp;L on this sale ≈ <b>{sign}{fmt_usd(abs(t.pnl_usd))}</b> ({fmt_pct(t.pnl_pct)}) "
                   f"· avg buy {fmt_usd(t.cost_basis_usd)}")
        else:
            pnl = "P&amp;L unknown (bought before tracking started)"
        lines = [
            f"🔻 <b>WALLET SELL</b> · {e(t.chain.value)} · {how}",
            f"<b>{e(who)}</b> sold <b>${e(t.symbol)}</b>" + (f" — {e(t.name)}" if t.name else ""),
            "",
            f"Size ≈ <b>{fmt_usd(t.usd_value)}</b> · Price {fmt_usd(t.price_usd)}",
            pnl,
            f"Liq {fmt_usd(t.liquidity_usd)} · MC {fmt_usd(t.market_cap)}",
            "",
            f"<code>{e(t.token_address)}</code>",
        ]
    else:
        lines = [
            f"🐋 <b>WALLET BUY</b> · {e(t.chain.value)}" + (" · <i>first buy</i>" if t.first_buy else " · <i>adding</i>"),
            f"<b>{e(who)}</b> bought <b>${e(t.symbol)}</b>" + (f" — {e(t.name)}" if t.name else ""),
            "",
            f"Size ≈ <b>{fmt_usd(t.usd_value)}</b> · Price {fmt_usd(t.price_usd)}",
            f"Liq {fmt_usd(t.liquidity_usd)} · MC {fmt_usd(t.market_cap)}"
            + (f" · Alpha Score <b>{t.alpha_score}</b>" if t.alpha_score is not None else ""),
            "",
            f"<code>{e(t.token_address)}</code>",
        ]
    links = [f'<a href="{EXPLORER_TX[t.chain].format(e(t.tx_hash))}">Tx</a>',
             f'<a href="{EXPLORER_WALLET[t.chain].format(e(t.wallet))}">Wallet</a>']
    if t.pair_url:
        links.append(f'<a href="{e(t.pair_url)}">DexScreener</a>')
    if dashboard_url:
        links.append(f'<a href="{e(dashboard_url.rstrip("/"))}/token/{t.chain.value}/{e(t.token_address)}">Inspector</a>')
    lines.append(" · ".join(links))
    lines.append("<i>On-chain activity, not a recommendation.</i>")
    return "\n".join(lines)


def render_trade_group(trades: list[WalletTrade], dashboard_url: str | None) -> str:
    """One alert for several trades of the same token and side."""
    t0 = max(trades, key=lambda t: t.id)  # freshest market data
    sell = (t0.side or "buy") == "sell"
    wallets: dict[str, list[WalletTrade]] = {}
    for t in trades:
        wallets.setdefault(t.wallet, []).append(t)
    n = len(wallets)
    total = sum(t.usd_value or 0 for t in trades)
    times = [utc(t.block_time) for t in trades if t.block_time]
    same_moment = n >= 2 and len(times) == len(trades) and (max(times) - min(times)).total_seconds() <= 2
    verb = "sold" if sell else "bought"
    head = (f"{'🔻' if sell else '🐋'} <b>{n} TRACKED WALLETS {verb.upper()}</b> ${e(t0.symbol)} · {e(t0.chain.value)}"
            if n >= 2 else
            f"{'🔻' if sell else '🐋'} <b>WALLET {'SELL' if sell else 'BUY'}</b> ×{len(trades)} · {e(t0.chain.value)}")
    lines = [head, f"Total ≈ <b>{fmt_usd(total)}</b>" + (f" — {e(t0.name)}" if t0.name else ""), ""]
    for addr, ts in list(wallets.items())[:8]:
        who = ts[0].wallet_label or f"{addr[:4]}…{addr[-4:]}"
        extra = ""
        if sell:
            last = max(ts, key=lambda x: x.id)
            if last.pct_sold is not None:
                extra = " · FULL EXIT" if last.pct_sold >= 99 else f" · sold {last.pct_sold:.0f}%"
            if last.pnl_usd is not None:
                extra += f" · P&amp;L {'+' if last.pnl_usd >= 0 else '-'}{fmt_usd(abs(last.pnl_usd))}"
        elif any(x.first_buy for x in ts):
            extra = " · first buy"
        lines.append(f"• <b>{e(who)}</b> {fmt_usd(sum(x.usd_value or 0 for x in ts))}{extra}")
    if len(wallets) > 8:
        lines.append(f"• …and {len(wallets) - 8} more")
    if same_moment:
        lines.append("⚠️ <i>Same second: likely one owner or one bot, not independent conviction.</i>")
    lines += ["",
              f"Price {fmt_usd(t0.price_usd)} · Liq {fmt_usd(t0.liquidity_usd)} · MC {fmt_usd(t0.market_cap)}"
              + (f" · Alpha Score <b>{t0.alpha_score}</b>" if t0.alpha_score is not None else ""),
              "", f"<code>{e(t0.token_address)}</code>"]
    links = [f'<a href="{EXPLORER_TX[t0.chain].format(e(t0.tx_hash))}">Latest tx</a>']
    if t0.pair_url:
        links.append(f'<a href="{e(t0.pair_url)}">DexScreener</a>')
    if dashboard_url:
        links.append(f'<a href="{e(dashboard_url.rstrip("/"))}/token/{t0.chain.value}/{e(t0.token_address)}">Inspector</a>')
    lines.append(" · ".join(links))
    lines.append("<i>On-chain activity, not a recommendation.</i>")
    return "\n".join(lines)


class TelegramAlerts:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is not set")
        self._base = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}"
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(40.0))
        self._offset = 0
        self.username: str | None = None

    # ---- transport --------------------------------------------------------
    async def _call(self, method: str, payload: dict[str, Any], timeout: float | None = None) -> Any | None:
        for attempt in range(3):
            try:
                resp = await self.http.post(f"{self._base}/{method}", json=payload,
                                            timeout=timeout or httpx.USE_CLIENT_DEFAULT)
                data = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                # never log the URL: it contains the bot token
                log.warning("Telegram %s failed: %s", method, type(exc).__name__)
                await asyncio.sleep(2 ** attempt)
                continue
            if data.get("ok"):
                return data.get("result")
            if data.get("error_code") == 429:
                await asyncio.sleep(float((data.get("parameters") or {}).get("retry_after", 5)))
                continue
            log.warning("Telegram %s error %s: %s", method, data.get("error_code"), data.get("description"))
            return None
        return None

    async def send(self, chat_id: str | int, text: str, markup: dict[str, Any] | None = None) -> bool:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": True}
        if markup:
            payload["reply_markup"] = markup
        return await self._call("sendMessage", payload) is not None

    # ---- smart wallet menu with Track / Untrack buttons --------------------
    async def wallets_menu(self) -> tuple[str, dict[str, Any] | None]:
        async with SessionLocal() as s:
            rows = (await s.execute(
                select(SmartWallet).where((SmartWallet.enabled.is_(True)) | (SmartWallet.track_buys.is_(True)))
                .order_by(SmartWallet.track_buys.desc().nulls_last(), SmartWallet.wins.desc(), SmartWallet.id)
                .limit(MENU_LIMIT)
            )).scalars().all()
        if not rows:
            return "No smart wallets yet. They are added automatically (Birdeye + auto-discovery).", None
        lines = ["<b>Smart wallets</b> — tap to turn buy alerts on/off\n"]
        keyboard = []
        for w in rows:
            st = w.stats or {}
            perf = (f" · {st['win_rate']:.0%} win · {fmt_usd(st.get('realized_usd_30d'))} 30d"
                    if st.get("win_rate") is not None else "")
            lines.append(f"{'👀' if w.track_buys else '▫️'} <b>{e(w.label)}</b> ({w.chain.value}, {w.source.value}){perf}\n"
                         f"<code>{e(w.address)}</code>")
            short = f"{w.label[:22]} · {w.chain.value[:3].upper()}"
            keyboard.append([{"text": f"✖ Untrack {short}" if w.track_buys else f"👀 Track {short}",
                              "callback_data": f"{'u' if w.track_buys else 't'}:{w.id}"}])
        return "\n".join(lines), {"inline_keyboard": keyboard}

    async def tracked_menu(self) -> tuple[str, dict[str, Any] | None]:
        """Tracked wallets, one ✖ Untrack button per address (an EVM address covers all its chains)."""
        from app.smart_money import tracked_wallets

        async with SessionLocal() as s:
            rows = await tracked_wallets(s)
        if not rows:
            return ("No tracked wallets. Add one with <code>track &lt;address&gt; [name]</code> "
                    "or the Track buttons in /wallets."), None
        by_addr: dict[str, list[SmartWallet]] = {}
        for w in rows:
            by_addr.setdefault(w.address, []).append(w)
        lines = [f"<b>Tracked wallets ({len(by_addr)})</b> — tap to stop buy/sell alerts\n"]
        keyboard = []
        for addr, ws in by_addr.items():
            label = next((w.label for w in ws if w.enabled), ws[0].label)
            nets = ", ".join(w.chain.value for w in ws)
            lines.append(f"👀 <b>{e(label)}</b> ({nets}{'' if any(w.enabled for w in ws) else ', watch-only'})\n"
                         f"<code>{e(addr)}</code>")
            keyboard.append([{"text": f"✖ Untrack {label[:28]}", "callback_data": f"x:{ws[0].id}"}])
        return "\n".join(lines), {"inline_keyboard": keyboard}

    async def handle_callback(self, cq: dict[str, Any]) -> None:
        msg = cq.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        data = str(cq.get("data") or "")
        if self._is_owner(chat_id) and re.fullmatch(r"(s|sc):\d+:\d+|sx", data):
            await self._sell_callback(cq, chat_id, msg.get("message_id"), data)
            return
        if not self._is_owner(chat_id) or not re.fullmatch(r"[tux]:\d+", data):
            await self._call("answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": "Not allowed"})
            return
        from app.smart_money import set_tracking

        action, wid = data.split(":")
        async with SessionLocal() as s:
            w = await s.get(SmartWallet, int(wid))
            if w is not None:
                label = w.label
                if action == "x":  # from /tracked: untrack this address on every chain it is tracked on
                    chains = [c for (c,) in (await s.execute(select(SmartWallet.chain).where(
                        SmartWallet.address == w.address, SmartWallet.track_buys.is_(True)))).all()] or [w.chain]
                    await set_tracking(s, chains, w.address, "", False)
                else:
                    await set_tracking(s, [w.chain], w.address, "", action == "t")
                await s.commit()
        note = ("Wallet not found" if w is None else
                f"Tracking {label}" if action == "t" else f"Stopped tracking {label}")
        log.info("callback %s -> %s", action, note)
        await self._call("answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": note[:190]})
        text, markup = await (self.tracked_menu() if action == "x" else self.wallets_menu())
        await self._call("editMessageText", {"chat_id": chat_id, "message_id": msg.get("message_id"), "text": text,
                                             "parse_mode": "HTML", "disable_web_page_preview": True,
                                             "reply_markup": markup or {"inline_keyboard": []}})

    async def sell_menu(self) -> tuple[str, dict[str, Any] | None]:
        from app.live import live_status

        async with SessionLocal() as s:
            st = await live_status(s, self.settings)
        opened = [(a["name"], p) for a in st["accounts"] for p in a["open"]]
        if st["mode"] == "off" or not opened:
            return "No open copy-trading positions.", None
        rows = []
        lines = ["<b>Open positions</b> — tap to sell (you confirm next)"]
        multi = len(st["accounts"]) > 1
        for acct, p in opened:
            sym = (p.symbol or p.token_address[:6]) + (f" [{acct}]" if multi else "")
            pending = f" · selling {p.manual_sell_frac * 100:.0f}%…" if p.manual_sell_frac else ""
            sold = f", sold ${p.proceeds_usd:,.2f}" if p.proceeds_usd else ""
            lines.append(f"${e(sym)}: cost ${p.cost_usd:,.2f}{sold}{pending}")
            rows.append([{"text": f"${sym} sell 50%", "callback_data": f"s:{p.id}:50"},
                         {"text": f"${sym} sell 100%", "callback_data": f"s:{p.id}:100"}])
        return "\n".join(lines), {"inline_keyboard": rows}

    async def _sell_callback(self, cq: dict[str, Any], chat_id, message_id, data: str) -> None:
        from app.live import request_manual_sell
        from app.models import LivePosition

        async def edit(text: str, markup: dict[str, Any] | None = None) -> None:
            await self._call("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text,
                                                 "parse_mode": "HTML",
                                                 "reply_markup": markup or {"inline_keyboard": []}})

        if data == "sx":
            await self._call("answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": "Cancelled"})
            text, markup = await self.sell_menu()
            await edit(text, markup)
            return
        kind, pid, pct = data.split(":")
        if kind == "s":   # ask for confirmation first
            async with SessionLocal() as s:
                pos = await s.get(LivePosition, int(pid))
            sym = (pos.symbol or pos.token_address[:6]) if pos else "?"
            await self._call("answerCallbackQuery", {"callback_query_id": cq.get("id")})
            await edit(f"Sell <b>{pct}%</b> of ${e(sym)} now at market price?",
                       {"inline_keyboard": [[{"text": f"✅ Yes, sell {pct}%", "callback_data": f"sc:{pid}:{pct}"},
                                             {"text": "Cancel", "callback_data": "sx"}]]})
            return
        async with SessionLocal() as s:
            ok, note = await request_manual_sell(s, int(pid), int(pct) / 100)
        log.info("manual sell %s %s%% -> %s", pid, pct, note)
        await self._call("answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": note[:190]})
        tail = "\nThe result arrives here in a few seconds." if ok else ""
        await edit(("⏳ " if ok else "⚠️ ") + e(note) + tail)

    # ---- alerts -----------------------------------------------------------
    async def _skip_backlog(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=BACKLOG_MINUTES)
        async with SessionLocal() as s:
            await s.execute(update(Signal).where(Signal.telegram_sent.is_(False), Signal.created_at < cutoff)
                            .values(telegram_sent=True))
            await s.execute(update(WalletTrade).where(WalletTrade.telegram_sent.is_(False),
                                                      WalletTrade.detected_at < cutoff).values(telegram_sent=True))
            await s.commit()

    async def deliver_pending(self) -> int:
        chat = self.settings.telegram_chat_id
        if not chat:
            return 0
        if not self.settings.telegram_signal_alerts:
            # Signal alerts off: only wallet buy/sell alerts go out. Signals stay on the dashboard, /top and
            # /performance; they are not queued (switching back on later does not replay old ones).
            return await self.deliver_trades(chat)
        async with SessionLocal() as s:
            pending = (await s.execute(
                select(Signal).where(Signal.telegram_sent.is_(False), Signal.score >= self.settings.telegram_min_score)
                .order_by(Signal.id.asc()).limit(20)
            )).scalars().all()
        sent = 0
        for sig in pending:
            async with SessionLocal() as s:
                claimed = await s.execute(update(Signal).where(Signal.id == sig.id, Signal.telegram_sent.is_(False))
                                          .values(telegram_sent=True))
                await s.commit()
            if claimed.rowcount != 1:
                continue  # another replica took it
            try:
                live = LiveSignal.model_validate(sig.payload)
            except ValueError:
                log.warning("Signal %s has an invalid payload; skipped", sig.id)
                continue
            if await self.send(chat, render_signal(live, self.settings.public_dashboard_url)):
                sent += 1
            await asyncio.sleep(SEND_INTERVAL)
        return sent + await self.deliver_trades(chat)

    async def deliver_trades(self, chat: str) -> int:
        """Send pending wallet-trade alerts. Trades of the same token and side are held for
        TELEGRAM_TRADE_GROUP_SECONDS after the first one so that several wallets buying (or selling) the same
        token arrive as one alert."""
        async with SessionLocal() as s:
            pending = (await s.execute(
                select(WalletTrade).where(WalletTrade.telegram_sent.is_(False)).order_by(WalletTrade.id.asc()).limit(100)
            )).scalars().all()
        wait = max(0, self.settings.telegram_trade_group_seconds)
        now = datetime.now(timezone.utc)
        groups: dict[tuple, list[WalletTrade]] = {}
        for t in pending:
            groups.setdefault((t.chain, t.token_address, t.side or "buy"), []).append(t)
        sent = 0
        for trades in groups.values():
            first = min(utc(t.detected_at) for t in trades)
            if wait and (now - first).total_seconds() < wait:
                continue  # still collecting other wallets for this token
            ids = [t.id for t in trades]
            async with SessionLocal() as s:
                claimed = await s.execute(update(WalletTrade).where(WalletTrade.id.in_(ids),
                                                                    WalletTrade.telegram_sent.is_(False))
                                          .values(telegram_sent=True))
                await s.commit()
            if claimed.rowcount != len(ids):
                continue  # another replica took (part of) it
            text = (render_trade(trades[0], self.settings.public_dashboard_url) if len(trades) == 1
                    else render_trade_group(trades, self.settings.public_dashboard_url))
            if await self.send(chat, text):
                sent += 1
            await asyncio.sleep(SEND_INTERVAL)
        return sent

    async def performance_text(self, hours: int = 24) -> str:
        from app.performance import performance_summary

        async with SessionLocal() as s:
            p = await performance_summary(s, hours=hours, recent_limit=0)
        o = p["overall"]
        window = f"{hours // 24}d" if hours % 24 == 0 and hours >= 48 else f"{hours}h"
        if not p["measured"]:
            return f"📊 <b>Signal performance · last {window}</b>\nNo signal is old enough to measure yet."

        def pc(v):
            return "—" if v is None else f"{v:.0f}%"

        lines = [f"📊 <b>Signal performance · last {window}</b>",
                 f"{p['measured']} of {p['signals_in_window']} signals measured (with 24h result: {o['n_24h']})", "",
                 f"Median change: 1h {fmt_pct(o['median_1h'])} · 6h {fmt_pct(o['median_6h'])} · "
                 f"24h {fmt_pct(o['median_24h'])}",
                 f"Up after 6h: {pc(o['positive_6h_pct'])} · after 24h: {pc(o['positive_24h_pct'])}",
                 f"Reached +50%: <b>{pc(o['hit_50_pct'])}</b> · median drawdown {fmt_pct(o['median_trough'])}",
                 "", "<b>By score</b>"]
        for b in p["by_score"]:
            lines.append(f"{e(b['label'])}: n={b['n']} · 6h {fmt_pct(b['median_6h'])} · "
                         f"24h {fmt_pct(b['median_24h'])} · +50% hit {pc(b['hit_50_pct'])}")
        if p["best"]:
            lines += ["", "<b>Best</b>"] + [f"${e(x['symbol'])} ({x['chain']}, {x['score']}): peak {fmt_pct(x['peak_pct'])}"
                                            for x in p["best"][:3]]
        lines.append("<i>Peaks are highs, not executed trades. Not financial advice.</i>")
        return "\n".join(lines)

    async def paper_text(self, hours: int = 24 * 7) -> str:
        from app.paper import paper_summary

        async with SessionLocal() as s:
            p = await paper_summary(s, hours=hours, recent_limit=0, settings=self.settings)
        window = f"{hours // 24}d" if hours % 24 == 0 and hours >= 48 else f"{hours}h"
        lines = [f"🧪 <b>Paper trading · last {window}</b>",
                 f"${p['size_usd']:.0f} per trade · {p['slippage_pct']:.1f}% slippage per fill (simulated)", ""]
        if not any(st["trades"] for st in p["strategies"]):
            lines.append("No paper trades yet. They open on the next tracked-wallet buy or 80+ signal.")
            return "\n".join(lines)
        for st in p["strategies"]:
            if not st["trades"]:
                lines.append(f"<b>{e(st['name'])}</b>: no trades")
                continue
            wr = "—" if st["win_rate"] is None else f"{st['win_rate'] * 100:.0f}%"
            sign = "+" if st["realized_usd"] >= 0 else "−"
            lines.append(f"<b>{e(st['name'])}</b>: {st['closed']} closed · win {wr} · "
                         f"{sign}${abs(st['realized_usd']):,.2f} ({fmt_pct(st['return_pct'])})")
            if st["open"]:
                u = st["unrealized_usd"]
                lines.append(f"   {st['open']} open · unrealized {'+' if u >= 0 else '−'}${abs(u):,.2f}")
        lines.append("<i>Simulation only, no real trades. Not financial advice.</i>")
        return "\n".join(lines)

    async def _maybe_daily_report(self) -> None:
        hour = self.settings.telegram_daily_report_hour
        chat = self.settings.telegram_chat_id
        now = datetime.now(timezone.utc)
        if hour is None or not chat or now.hour != hour:
            return
        today = now.strftime("%Y-%m-%d")
        async with SessionLocal() as s:  # claim today's report once, even across restarts / replicas
            row = await s.get(TrackerCursor, "daily_report")
            if row is not None and row.value == today:
                return
            if row is None:
                s.add(TrackerCursor(key="daily_report", value=today))
            else:
                row.value = today
            await s.commit()
        await self.send(chat, await self.performance_text(24))
        if self.settings.paper_enabled:
            await self.send(chat, await self.paper_text(24))

    async def _alerts_loop(self) -> None:
        await self._skip_backlog()
        while True:
            try:
                await self._maybe_daily_report()
                await self.deliver_pending()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Telegram delivery failed")
            await asyncio.sleep(3)

    # ---- commands ---------------------------------------------------------
    def _is_owner(self, chat_id: str | int | None) -> bool:
        return chat_id is not None and str(chat_id) == str(self.settings.telegram_chat_id or "")

    async def handle_command(self, text: str, chat_id: str | int | None = None) -> str:
        parts = text.strip().split()
        if not parts:
            return ""
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]

        # Deep link from the dashboard's Track button: t.me/<bot>?start=track_sol_<addr> | track_evm_<addr>
        if cmd == "/start" and args and args[0].startswith(("track_sol_", "track_evm_")):
            kind, addr = args[0][6:9], args[0][10:]
            cmd, args = "/track", ["solana" if kind == "sol" else "evm", addr]
        elif cmd == "/start" and args and args[0].startswith(("untrack_sol_", "untrack_evm_")):
            kind, addr = args[0][8:11], args[0][12:]
            cmd, args = "/untrack", ["solana" if kind == "sol" else "evm", addr]

        if cmd in ("/start", "/help"):
            return ("<b>Alpha Scanner</b>\n"
                    "/top [solana|ethereum|base|bsc] — top 5 by Alpha Score\n"
                    "/token &lt;address&gt; — score card\n"
                    "/status — scanner health\n"
                    "/performance [24h|7d] — how signals did after they fired\n"
                    "/paper [24h|7d] — paper-trading results per strategy\n"
                    "/live [on|off] — copy-trading status, pause or resume buys\n"
                    "/sell — sell an open copy-trading position by hand\n"
                    "/wallets — smart wallets with Track buttons\n"
                    "/track &lt;address&gt; [name] — buy alerts for any wallet (chain auto-detected)\n"
                    "/untrack &lt;address&gt;\n"
                    "/tracked — tracked wallets with Untrack buttons")

        if cmd in ("/track", "/untrack", "/tracked", "/wallets") and not self._is_owner(chat_id):
            return "Wallet tracking can only be managed from the owner's chat."

        if cmd in ("/track", "/untrack"):
            from app.smart_money import parse_track_target, set_tracking

            if not args:
                return f"Usage: {cmd} &lt;address&gt;" + (" [name]" if cmd == "/track" else "")
            # Chain is optional: base58 -> Solana, 0x -> EVM (Ethereum + Base + BSC)
            if args[0].lower() in ("sol", "solana", "evm", "eth", "ethereum", "base", "bsc"):
                if len(args) < 2:
                    return f"Usage: {cmd} [chain] &lt;address&gt;"
                chain_arg, addr_arg, rest = args[0], args[1], args[2:]
            else:
                chain_arg = "evm" if args[0].lower().startswith("0x") else "solana"
                addr_arg, rest = args[0], args[1:]
            try:
                chains, addr = parse_track_target(chain_arg, addr_arg)
            except ValueError as exc:
                return e(str(exc))
            label = " ".join(rest)[:60]
            async with SessionLocal() as s:
                n = await set_tracking(s, chains, addr, label, cmd == "/track")
                await s.commit()
            nets = ", ".join(c.value for c in chains)
            if cmd == "/untrack":
                return f"Buy alerts off for <code>{e(addr)}</code> ({nets})." if n else "That wallet was not tracked."
            return (f"👀 Tracking <code>{e(addr)}</code> on {nets}"
                    + (f" as <b>{e(label)}</b>" if label else "")
                    + ".\nYou'll get an alert for each buy ≥ $"
                    + f"{self.settings.tracker_min_buy_usd:,.0f} from now on (past trades are not replayed).")

        if cmd == "/tracked":
            from app.smart_money import tracked_wallets

            async with SessionLocal() as s:
                rows = await tracked_wallets(s)
            if not rows:
                return "No tracked wallets. Add one with /track solana &lt;address&gt; [name]"
            lines = [f"• <b>{e(w.label)}</b> ({w.chain.value}{'' if w.enabled else ', watch-only'})\n"
                     f"  <code>{e(w.address)}</code>" for w in rows]
            return f"<b>Tracked wallets ({len(rows)})</b>\n" + "\n".join(lines)

        if cmd == "/top":
            chain = None
            if args:
                try:
                    chain = Chain(args[0].lower())
                except ValueError:
                    return "Unknown chain. Use solana, ethereum, base or bsc."
            async with SessionLocal() as s:
                page = await list_tokens(s, TokenListParams(chain=chain, sort=SortField.score, page_size=5))
            if not page.items:
                return "No active tokens yet."
            rows = [f"{BADGE_ICON[t.badge]} <b>{t.alpha_score}</b> ${e(t.symbol)} ({t.chain.value}) · "
                    f"Liq {fmt_usd(t.liquidity_usd)} · 1h {fmt_pct(t.price_change_h1)}\n<code>{e(t.address)}</code>"
                    for t in page.items]
            return "<b>Top Alpha</b>" + (f" · {chain.value}" if chain else "") + "\n\n" + "\n\n".join(rows)

        if cmd == "/token":
            if not args:
                return "Usage: /token &lt;address&gt;"
            address = args[0].strip()
            async with SessionLocal() as s:
                token = None
                for ch in Chain:
                    if ch != Chain.solana and not address.lower().startswith("0x"):
                        continue
                    token = await get_token(s, ch, address)
                    if token:
                        break
                if token is None:
                    return "Token not found in the scanner (it may be below the liquidity/volume filters)."
                d = await token_detail(s, token, history_points=1)
            sc = d.score
            lines = [f"{BADGE_ICON[token.badge]} <b>${e(token.symbol)}</b> — {e(token.name)} ({token.chain.value})",
                     f"Alpha Score <b>{token.alpha_score}/100</b>"]
            if sc:
                lines.append(f"Security {sc.security.points:g}/35 · Momentum {sc.momentum.points:g}/35 · "
                             f"Smart money {sc.smart_money.points:g}/30")
                if sc.hard_fail:
                    lines.append(f"⛔ Hard fail: {e(sc.hard_fail)}")
                for c in (*sc.security.components, *sc.momentum.components, *sc.smart_money.components):
                    lines.append(f"{'✅' if c.passed else ('➖' if c.points else '❌')} {e(c.detail or c.label)}")
            lines.append(f"Price {fmt_usd(token.price_usd)} · Liq {fmt_usd(token.liquidity_usd)} · "
                         f"MC {fmt_usd(token.market_cap or token.fdv)}")
            if token.pair_url:
                lines.append(f'<a href="{e(token.pair_url)}">DexScreener</a>')
            return "\n".join(lines)

        if cmd == "/live":
            if not self._is_owner(chat_id):
                return "Only the bot owner can use /live."
            from app.live import live_status, set_paused

            async with SessionLocal() as s:
                if args and args[0].lower() in ("off", "stop", "pause"):
                    await set_paused(s, True)
                    await s.commit()
                    return "⏸ Copy trading paused: no new buys. Open positions still sell when the wallet sells."
                if args and args[0].lower() in ("on", "start", "resume"):
                    await set_paused(s, False)
                    await s.commit()
                    return "▶️ Copy trading resumed."
                st = await live_status(s, self.settings)
            if st["mode"] == "off":
                return "Copy trading is off (LIVE_MODE=off on the server)."
            lines = [f"<b>Copy trading · {'🟢 LIVE' if st['mode'] == 'live' else '🧪 DRY-RUN'}</b>"
                     f"{' · ⏸ paused' if st['paused'] else ''}"]
            for a in st["accounts"]:
                lines += ["", f"<b>[{e(a['name'])}]</b>{'' if a['active'] else ' · idle (no key)'}",
                          f"Copying: {', '.join(e(w[:4] + '…' + w[-4:]) for w in a['wallets'])}",
                          f"${a['size_usd']:.0f} per buy · ${a['open_cost_usd']:,.0f} of ${a['max_open_usd']:.0f} open"]
                if a["pubkey"]:
                    lines.append(f"Trading wallet: <code>{e(a['pubkey'])}</code>")
                r = a["realized_usd"]
                lines.append(f"Realized: {'+' if r >= 0 else '−'}${abs(r):,.2f} ({len(a['closed'])} closed, "
                             f"{len(a['failed'])} failed)")
                for p in a["open"]:
                    sold = f", sold ${p.proceeds_usd:,.2f}" if p.proceeds_usd else ""
                    lines.append(f"  • ${e(p.symbol or p.token_address[:6])}: cost ${p.cost_usd:,.2f}{sold}")
            lines.append("\n/live off — pause new buys (all accounts) · /live on — resume · /sell")
            return "\n".join(lines)

        if cmd == "/paper":
            hours = 24 * 7
            if args and args[0].rstrip("dh").isdigit():
                hours = int(args[0].rstrip("dh")) * (24 if args[0].endswith("d") else 1)
            return await self.paper_text(min(max(hours, 1), 24 * 90))

        if cmd == "/performance":
            hours = 24
            if args and args[0].rstrip("dh").isdigit():
                hours = int(args[0].rstrip("dh")) * (24 if args[0].endswith("d") else 1)
            return await self.performance_text(min(max(hours, 1), 24 * 30))

        if cmd == "/status":
            async with SessionLocal() as s:
                st = await scanner_status(s)
            lines = [f"Scanner: {'🟢 live' if st.live else '🔴 stalled'}",
                     f"Active tokens (1h): {st.active_tokens}",
                     f"Signals 24h: {st.signals_24h} · High alpha (≥80): {st.high_alpha_24h}"]
            if st.top_gainer:
                g = st.top_gainer
                lines.append(f"Top gainer since signal: ${e(g.symbol)} {fmt_pct(g.gain_pct)}")
            return "\n".join(lines)

        return "Unknown command. Try /help."

    async def _commands_loop(self) -> None:
        while True:
            try:
                updates = await self._call("getUpdates", {"offset": self._offset, "timeout": 25,
                                                          "allowed_updates": ["message", "callback_query"]},
                                           timeout=35.0)
                for u in updates or []:
                    self._offset = max(self._offset, int(u["update_id"]) + 1)
                    if u.get("callback_query"):
                        await self.handle_callback(u["callback_query"])
                        continue
                    msg = u.get("message") or {}
                    text = (msg.get("text") or "").strip()
                    chat_id = (msg.get("chat") or {}).get("id")
                    if chat_id is None:
                        continue
                    owner = self._is_owner(chat_id)
                    # Owner may skip the slash: "track <address> [name]", "wallets", ...
                    if owner and text and not text.startswith("/") and text.split()[0].lower() in SLASHLESS:
                        text = "/" + text
                    if not text.startswith("/"):
                        log.info("non-command message in %s chat ignored", "owner" if owner else "other")
                        if owner and text:
                            await self.send(chat_id, "Try <code>track &lt;address&gt; [name]</code>, "
                                                     "<code>wallets</code> or /help.")
                        continue
                    first = text.split()[0].split("@")[0].lower()
                    if first in ("/wallets", "/tracked", "/sell") and owner:
                        body, markup = await (self.wallets_menu() if first == "/wallets" else
                                              self.tracked_menu() if first == "/tracked" else self.sell_menu())
                        log.info("command %s in owner chat -> %d buttons", first,
                                 len((markup or {}).get("inline_keyboard", [])))
                        await self.send(chat_id, body, markup)
                        continue
                    reply = await self.handle_command(text, chat_id)
                    log.info("command %s in %s chat -> %s", text.split()[0][:20], "owner" if owner else "other",
                             re.sub(r"<[^>]+>", "", reply.split("\n")[0])[:90] if reply else "(no reply)")
                    if reply:
                        await self.send(chat_id, reply)
                if updates is None:
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Telegram command loop error")
                await asyncio.sleep(5)

    async def run(self) -> None:
        me = await self._call("getMe", {})
        self.username = (me or {}).get("username")  # used by the dashboard's Track deep links
        tasks = [asyncio.create_task(self._alerts_loop())]
        if self.settings.telegram_commands_enabled:
            tasks.append(asyncio.create_task(self._commands_loop()))
        log.info("Telegram started (alerts to chat=%s, commands=%s)", bool(self.settings.telegram_chat_id),
                 self.settings.telegram_commands_enabled)
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()

    async def close(self) -> None:
        await self.http.aclose()


async def _main() -> None:
    await init_db()
    bot = TelegramAlerts()
    try:
        await bot.run()
    finally:
        await bot.close()


if __name__ == "__main__":
    s = get_settings()
    logging.basicConfig(level=s.log_level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
