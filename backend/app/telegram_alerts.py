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
from app.models import Badge, Chain, Signal, Token, WalletTrade
from app.repository import get_token, list_tokens, scanner_status, token_detail
from app.schemas import LiveSignal, SortField, TokenListParams

log = logging.getLogger("telegram")

BADGE_ICON = {Badge.strong_alpha: "🟢", Badge.moderate: "🟠", Badge.high_risk: "🔴"}
BADGE_TEXT = {Badge.strong_alpha: "STRONG ALPHA", Badge.moderate: "MODERATE", Badge.high_risk: "HIGH RISK"}
BACKLOG_MINUTES = 10   # on startup, don't post signals older than this
SEND_INTERVAL = 1.1    # Telegram allows ~1 msg/s per chat


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


class TelegramAlerts:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is not set")
        self._base = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}"
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(40.0))
        self._offset = 0

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

    async def send(self, chat_id: str | int, text: str) -> bool:
        res = await self._call("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                               "disable_web_page_preview": True})
        return res is not None

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
        async with SessionLocal() as s:
            pending = (await s.execute(
                select(WalletTrade).where(WalletTrade.telegram_sent.is_(False)).order_by(WalletTrade.id.asc()).limit(20)
            )).scalars().all()
        sent = 0
        for t in pending:
            async with SessionLocal() as s:
                claimed = await s.execute(update(WalletTrade).where(WalletTrade.id == t.id,
                                                                    WalletTrade.telegram_sent.is_(False))
                                          .values(telegram_sent=True))
                await s.commit()
            if claimed.rowcount != 1:
                continue
            if await self.send(chat, render_trade(t, self.settings.public_dashboard_url)):
                sent += 1
            await asyncio.sleep(SEND_INTERVAL)
        return sent

    async def _alerts_loop(self) -> None:
        await self._skip_backlog()
        while True:
            try:
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

        if cmd in ("/start", "/help"):
            return ("<b>Alpha Scanner</b>\n"
                    "/top [solana|ethereum|base|bsc] — top 5 by Alpha Score\n"
                    "/token &lt;address&gt; — score card\n"
                    "/status — scanner health\n"
                    "/track &lt;solana|ethereum|base|bsc|evm&gt; &lt;address&gt; [name] — buy alerts for a wallet\n"
                    "/untrack &lt;chain|evm&gt; &lt;address&gt;\n"
                    "/tracked — wallets with buy alerts")

        if cmd in ("/track", "/untrack", "/tracked") and not self._is_owner(chat_id):
            return "Wallet tracking can only be managed from the owner's chat."

        if cmd in ("/track", "/untrack"):
            from app.smart_money import parse_track_target, set_tracking

            if len(args) < 2:
                return f"Usage: {cmd} &lt;solana|ethereum|base|bsc|evm&gt; &lt;address&gt;" + (" [name]" if cmd == "/track" else "")
            try:
                chains, addr = parse_track_target(args[0], args[1])
            except ValueError as exc:
                return e(str(exc))
            label = " ".join(args[2:])[:60]
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
                                                          "allowed_updates": ["message"]}, timeout=35.0)
                for u in updates or []:
                    self._offset = max(self._offset, int(u["update_id"]) + 1)
                    msg = u.get("message") or {}
                    text = msg.get("text") or ""
                    chat_id = (msg.get("chat") or {}).get("id")
                    if chat_id is None:
                        continue
                    owner = self._is_owner(chat_id)
                    if not text.startswith("/"):
                        log.info("non-command message in %s chat ignored", "owner" if owner else "other")
                        if owner and text.strip():
                            await self.send(chat_id, "Commands start with a slash, e.g. "
                                                     "<code>/track solana &lt;address&gt; name</code>. Try /help.")
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
