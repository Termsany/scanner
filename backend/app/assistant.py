"""Free questions to the Telegram bot, answered by Claude (user's request 2026-11-26).

The owner writes anything that is not a command ("how much did I make today?", "how is BI doing?") and Claude
answers in Egyptian Arabic from the bot's own data, read through a few read-only tools: the accounts and their open
positions (valued now), recent closed trades, each copied wallet's rules, paper results and a coin lookup. It can
never trade, change settings or move funds: actions stay with the commands (/live off, /sell) and the dashboard.

Needs ANTHROPIC_API_KEY. Each chat keeps its conversation for ASSISTANT_MEMORY_MINUTES, then starts fresh.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import or_, select

from app.config import Settings, get_settings
from app.models import LivePosition, Token, WalletTrade

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
MAX_TOOL_ROUNDS = 6

SYSTEM = """You are the assistant inside the owner's Telegram bot for "Alpha Scanner", their Solana copy-trading and
paper-trading system. The owner writes in Egyptian Arabic; answer in Egyptian Arabic, keeping technical terms
(wallet, mirror, stop, paper, dry run, PnL...) in English as they do.

How the system works, in short:
- Live accounts copy "smart wallets" (labels such as Smart1, Smart2). Each copied wallet has its own rules: mirror
  (sell the same share the wallet sells), take the cost back at a multiple, sell everything at a multiple, stop
  loss, trailing stop, time limit, one buy per coin a day. Accounts in mode "dry" are simulations: no real money.
- Paper strategies simulate trades only.
- Times in the data are UTC; Egypt is UTC+2: say times in Egypt time.

Rules:
- Use the tools to get real numbers; never guess a figure. If the data doesn't answer the question, say so.
- You cannot buy, sell, pause, or change any setting. If asked, say how the owner does it: /live off and /live on
  pause and resume buys, /sell sells a position by hand, the Accounts page changes a wallet's rules.
- Answer briefly, like a chat message: lead with the answer, then at most a few lines of figures. Plain text, no
  markdown tables or headings; short lines starting with "-" are fine. Money as $12.34, percentages as +12%.
"""

TOOLS: list[dict[str, Any]] = [
    {"name": "get_accounts", "strict": True,
     "description": "Every trading account: mode (live / dry), paused or not, entry size, budget, daily loss limit, "
                    "realized PnL of the last 24 h, and its open positions valued at the current price.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False, "required": []}},
    {"name": "get_trades", "strict": True,
     "description": "Closed trades of the last `hours` hours (all accounts, or one), newest first, with the totals.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["hours", "account"],
                      "properties": {"hours": {"type": "integer", "description": "1 to 720"},
                                     "account": {"type": "string",
                                                 "description": "account name, or empty for all accounts"}}}},
    {"name": "get_wallet_rules", "strict": True,
     "description": "The copied wallets of every account and the rules the bot runs for each one.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False, "required": []}},
    {"name": "get_paper_results", "strict": True,
     "description": "Paper strategy results over the last `days` days: trades, win rate, PnL, paused or not.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["days"],
                      "properties": {"days": {"type": "integer", "description": "1 to 30"}}}},
    {"name": "lookup_coin", "strict": True,
     "description": "A coin by symbol (e.g. BI) or address: our positions in it (open and recent), the copied "
                    "wallets' latest trades in it, and its scanner data.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["query"],
                      "properties": {"query": {"type": "string"}}}},
]


def _iso(dt: Any) -> str | None:
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else None


async def _dex_prices(mints: list[str]) -> dict[str, float]:
    """USD price per token from DexScreener (deepest pair), up to 30 per call."""
    out: dict[str, float] = {}
    async with httpx.AsyncClient(timeout=10) as c:
        for i in range(0, len(mints), 30):
            try:
                r = await c.get(f"https://api.dexscreener.com/latest/dex/tokens/{','.join(mints[i:i + 30])}")
                pairs = (r.json() or {}).get("pairs") or []
            except Exception:
                continue
            best: dict[str, tuple[float, float]] = {}
            for p in pairs:
                a = (p.get("baseToken") or {}).get("address")
                liq = float((p.get("liquidity") or {}).get("usd") or 0)
                try:
                    px = float(p.get("priceUsd") or 0)
                except (TypeError, ValueError):
                    continue
                if a and px and (a not in best or liq > best[a][0]):
                    best[a] = (liq, px)
            out.update({a: v[1] for a, v in best.items()})
    return out


class Assistant:
    def __init__(self, settings: Settings | None = None, session_factory=None, client=None) -> None:
        self.s = settings or get_settings()
        if session_factory is None:
            from app.db import SessionLocal

            session_factory = SessionLocal
        self.Session = session_factory
        self._client = client
        self.chats: dict[str, tuple[float, list[dict[str, Any]]]] = {}   # chat -> (last use, messages)
        self._day: tuple[str, int] = ("", 0)

    @property
    def ai(self) -> bool:
        """Claude answers only when ASSISTANT_AI is on and there is a key; otherwise the fixed questions do."""
        return bool(self.s.assistant_ai and (self._client or self.s.anthropic_api_key))

    @property
    def enabled(self) -> bool:
        return True                                   # the fixed questions need nothing

    def client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=self.s.anthropic_api_key)
        return self._client

    # ---- tools (read-only) --------------------------------------------------------------------------------------
    def _labels(self, accounts) -> dict[str, str]:
        out: dict[str, str] = {}
        for a in accounts:
            for w, ld in a.leaders.items():
                if ld.label:
                    out.setdefault(w, ld.label)
        return out

    async def _accounts(self, session):
        from app.accounts import load_accounts

        return await load_accounts(session, self.s)

    async def tool(self, name: str, args: dict[str, Any]) -> Any:
        from app.live import live_status, rules_text

        async with self.Session() as session:
            if name == "get_accounts":
                st = await live_status(session, self.s)
                labels = self._labels(await self._accounts(session))
                opened = [p for a in st["accounts"] for p in a["open"]]
                px = await _dex_prices(sorted({p.token_address for p in opened})) if opened else {}
                out = []
                for a in st["accounts"]:
                    if not a["enabled"]:
                        continue
                    pos = []
                    for p in a["open"]:
                        price = px.get(p.token_address)
                        worth = int(p.tokens_raw or 0) / 1e6 * price if price else None
                        total = (worth or 0) + (p.proceeds_usd or 0)
                        pos.append({"symbol": p.symbol, "address": p.token_address,
                                    "wallet": labels.get(p.leader_wallet or "", (p.leader_wallet or "")[:6]),
                                    "opened": _iso(p.opened_at), "cost_usd": round(p.cost_usd, 2),
                                    "sold_so_far_usd": round(p.proceeds_usd or 0, 2),
                                    "worth_now_usd": round(worth, 2) if worth is not None else None,
                                    "pnl_pct_now": round((total / p.cost_usd - 1) * 100, 1)
                                    if worth is not None and p.cost_usd else None})
                    out.append({"account": a["name"], "mode": "dry" if a["dry_run"] else st["mode"],
                                "paused": a["paused"] or st["paused"], "entry_usd": a["size_usd"],
                                "max_open_usd": a["max_open_usd"], "daily_loss_limit_usd": a["max_daily_loss_usd"],
                                "realized_pnl_24h_usd": round(a["realized_24h"], 2),
                                "open_cost_usd": round(a["open_cost_usd"], 2), "open_positions": pos})
                return {"now": _iso(_now()), "accounts": out,
                        "note": "worth_now uses DexScreener prices; 6-decimal pump.fun tokens assumed"}
            if name == "get_trades":
                hours = min(max(int(args.get("hours") or 24), 1), 720)
                acct = (args.get("account") or "").strip()
                q = select(LivePosition).where(LivePosition.status == "closed",
                                               LivePosition.closed_at >= _now() - timedelta(hours=hours))
                if acct:
                    q = q.where(LivePosition.account == acct)
                rows = (await session.execute(q.order_by(LivePosition.closed_at.desc()).limit(60))).scalars().all()
                labels = self._labels(await self._accounts(session))
                trades = [{"account": p.account, "mode": p.mode, "symbol": p.symbol, "address": p.token_address,
                           "wallet": labels.get(p.leader_wallet or "", (p.leader_wallet or "")[:12]),
                           "opened": _iso(p.opened_at), "closed": _iso(p.closed_at), "cost_usd": round(p.cost_usd, 2),
                           "pnl_usd": round(p.pnl_usd or 0, 2),
                           "pnl_pct": round((p.pnl_usd or 0) / p.cost_usd * 100, 1) if p.cost_usd else None,
                           "exit": p.exit_reason} for p in rows]
                by: dict[str, dict[str, float]] = {}
                for t in trades:
                    k = f"{t['account']} ({t['mode']})"
                    d = by.setdefault(k, {"trades": 0, "wins": 0, "pnl_usd": 0.0})
                    d["trades"] += 1
                    d["wins"] += int(t["pnl_usd"] > 0)
                    d["pnl_usd"] = round(d["pnl_usd"] + t["pnl_usd"], 2)
                return {"hours": hours, "totals": by, "trades": trades}
            if name == "get_wallet_rules":
                from app.live import CopyEngine

                out = []
                for a in await self._accounts(session):
                    eng = CopyEngine.__new__(CopyEngine)
                    eng.a, eng.s = a, self.s
                    out.append({"account": a.name, "dry": a.dry, "wallets": [
                        {"label": ld.label or w[:6], "address": w, "rules": rules_text(ld, eng)}
                        for w, ld in a.leaders.items()]})
                return out
            if name == "get_paper_results":
                from app.paper import paper_summary

                days = min(max(int(args.get("days") or 7), 1), 30)
                summ = await paper_summary(session, hours=days * 24, recent_limit=0, settings=self.s)
                keep = ("name", "trades", "closed", "open", "wins", "win_rate", "realized_usd", "unrealized_usd",
                        "avg_pnl_pct", "paused")
                return {"days": days, "strategies": [{k: v for k, v in st.items() if k in keep}
                                                     for st in summ.get("strategies", [])
                                                     if st.get("trades") or st.get("closed")]}
            if name == "lookup_coin":
                qy = (args.get("query") or "").strip().lstrip("$")
                if not qy:
                    return {"error": "empty query"}
                cond = (LivePosition.token_address == qy) if len(qy) > 25 else (LivePosition.symbol.ilike(qy))
                pos = (await session.execute(select(LivePosition).where(cond, or_(
                    LivePosition.status == "open", LivePosition.opened_at >= _now() - timedelta(days=7)))
                    .order_by(LivePosition.opened_at.desc()).limit(10))).scalars().all()
                addrs = {p.token_address for p in pos} or ({qy} if len(qy) > 25 else set())
                tcond = WalletTrade.token_address.in_(addrs) if addrs else WalletTrade.symbol.ilike(qy)
                trades = (await session.execute(select(WalletTrade).where(tcond)
                                                .order_by(WalletTrade.id.desc()).limit(12))).scalars().all()
                addrs |= {t.token_address for t in trades}
                toks = (await session.execute(select(Token).where(Token.address.in_(addrs)))).scalars().all() \
                    if addrs else []
                labels = self._labels(await self._accounts(session))
                return {
                    "positions": [{"account": p.account, "mode": p.mode, "status": p.status, "symbol": p.symbol,
                                   "address": p.token_address, "opened": _iso(p.opened_at), "closed": _iso(p.closed_at),
                                   "cost_usd": round(p.cost_usd, 2), "sold_usd": round(p.proceeds_usd or 0, 2),
                                   "pnl_usd": round(p.pnl_usd, 2) if p.pnl_usd is not None else None,
                                   "exit": p.exit_reason} for p in pos],
                    "wallet_trades": [{"wallet": labels.get(t.wallet, t.wallet_label or t.wallet[:6]), "side": t.side,
                                       "symbol": t.symbol, "usd": round(t.usd_value or 0, 2), "pct_sold": t.pct_sold,
                                       "at": _iso(t.block_time)} for t in trades],
                    "scanner": [{"symbol": t.symbol, "address": t.address, "chain": str(t.chain),
                                 "liquidity_usd": t.liquidity_usd, "market_cap": t.market_cap,
                                 "alpha_score": getattr(t, "alpha_score", None)} for t in toks],
                }
        return {"error": f"unknown tool {name}"}

    # ---- the conversation ---------------------------------------------------------------------------------------
    def _limit_ok(self) -> bool:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        d, n = self._day
        n = n + 1 if d == day else 1
        self._day = (day, n)
        return n <= self.s.assistant_daily_limit

    async def ask(self, chat_id: str | int, text: str) -> str:
        if not self.ai:
            from app.quick_answers import QuickAnswers

            try:
                return await QuickAnswers(self).answer(text)
            except Exception as exc:
                log.warning("quick answer: %s", exc)
                return "حصلت مشكلة وأنا بجيب البيانات، جرّب تاني."
        if not self._limit_ok():
            return f"وصلت لحد الأسئلة النهارده ({self.s.assistant_daily_limit}). جرّب الأوامر: /help."
        key = str(chat_id)
        last, msgs = self.chats.get(key, (0.0, []))
        if time.monotonic() - last > self.s.assistant_memory_minutes * 60 or len(msgs) > 40:
            msgs = []                                     # a new conversation (history is only ever appended to)
        msgs = [*msgs, {"role": "user", "content": text[:2000]}]
        try:
            reply = await self._run(msgs)
        except Exception as exc:
            log.warning("assistant: %s", exc)
            why = str(exc).lower()
            if "credit balance" in why:
                return "رصيد Anthropic خلص، فمقدرش أرد. اشحن من console.anthropic.com ← Plans & Billing."
            if "authentication" in why or "invalid x-api-key" in why or "401" in why:
                return "الـ ANTHROPIC_API_KEY اللي على السيرفر مش شغال (Anthropic رفضته)."
            return "حصلت مشكلة وأنا بجهّز الرد، جرّب تاني بعد شوية."
        self.chats[key] = (time.monotonic(), msgs)
        return reply

    async def _run(self, msgs: list[dict[str, Any]]) -> str:
        import anthropic

        client = self.client()
        for _ in range(MAX_TOOL_ROUNDS):
            try:
                resp = await client.beta.messages.create(
                    model=self.s.assistant_model, max_tokens=4000, system=SYSTEM, tools=TOOLS, messages=msgs,
                    output_config={"effort": "low"}, cache_control={"type": "ephemeral"},
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default")
            except anthropic.RateLimitError:
                return "الخدمة مشغولة دلوقتي، جرّب تاني بعد دقيقة."
            msgs.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "refusal":
                return "مقدرتش أرد على السؤال ده."
            if resp.stop_reason != "tool_use":
                return "\n".join(b.text for b in resp.content if b.type == "text").strip() or "…"
            results = []
            for b in resp.content:
                if b.type != "tool_use":
                    continue
                try:
                    out = await self.tool(b.name, dict(b.input or {}))
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "content": json.dumps(out, default=str)[:60_000]})
                except Exception as exc:
                    log.warning("assistant tool %s: %s", b.name, exc)
                    results.append({"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                                    "content": f"tool failed: {str(exc)[:200]}"})
            msgs.append({"role": "user", "content": results})
        return "السؤال محتاج بيانات أكتر من اللي أقدر أجيبها مرة واحدة، جرّب تسأله بشكل أبسط."


def _now():
    from app.live import _now as now

    return now()
