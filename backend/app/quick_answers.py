"""Free questions to the Telegram bot, answered without any AI (user's request 2026-11-26: no Anthropic API,
answer the questions it was taught and fetch the details itself).

The owner's message is matched to one of a few question kinds, in Egyptian Arabic or English, and answered from the
bot's own data:
  - profit / loss over a window ("كسبت كام النهارده؟", "خسرت كام امبارح", "مكسب الاسبوع")
  - open positions ("ايه الصفقات المفتوحة؟", "انا ماسك ايه")
  - last trades ("اخر صفقات", "اخر 5 صفقات")
  - a coin ("BI عاملة ايه؟", "$PERRO", an address)
  - a wallet's rules ("قواعد Smart2", "اعدادات المحافظ")
  - paper results ("نتايج البيبر", "احسن بوت paper")
  - the accounts ("حالة الحسابات", "الحساب")
Anything else gets the list of questions it knows. Read-only: it never trades or changes a setting.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from app.models import LivePosition

EGYPT = timezone(timedelta(hours=2))
ADDR_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

HELP = ("أقدر أجاوب على أسئلة زي:\n"
        "- كسبت كام النهارده؟ / امبارح / الاسبوع ده\n"
        "- ايه الصفقات المفتوحة؟\n"
        "- اخر 5 صفقات\n"
        "- BI عاملة ايه؟ (اسم العملة أو عنوانها)\n"
        "- قواعد Smart2\n"
        "- نتايج البيبر\n"
        "- حالة الحسابات\n"
        "أو الأوامر: /help")


def norm(text: str) -> str:
    """Arabic spelling variants folded together, lower case, no tashkeel."""
    t = re.sub(r"[ً-ْـ]", "", text.lower())
    for a, b in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ة", "ه"), ("ى", "ي"), ("؟", " "), ("?", " ")):
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t).strip()


def _has(t: str, *words: str) -> bool:
    return any(w in t for w in words)


def window(t: str) -> tuple[str, timedelta, timedelta]:
    """(label, start back from now, end back from now) of the period the question asks about."""
    m = re.search(r"(\d+)\s*(ساعه|ساعات|h)", t)
    if m:
        h = int(m.group(1))
        return f"آخر {h} ساعة", timedelta(hours=h), timedelta(0)
    m = re.search(r"(\d+)\s*(يوم|ايام|d)", t)
    if m:
        d = int(m.group(1))
        return f"آخر {d} يوم", timedelta(days=d), timedelta(0)
    if _has(t, "امبارح", "امس", "yesterday"):
        return "امبارح", timedelta(hours=48), timedelta(hours=24)
    if _has(t, "اسبوع", "week", "7 ايام"):
        return "آخر 7 أيام", timedelta(days=7), timedelta(0)
    if _has(t, "شهر", "month"):
        return "آخر 30 يوم", timedelta(days=30), timedelta(0)
    return "آخر 24 ساعة", timedelta(hours=24), timedelta(0)


def _money(v: float) -> str:
    return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"


def _when(dt: Any) -> str:
    if not dt:
        return "?"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(EGYPT).strftime("%d/%m %H:%M")


class QuickAnswers:
    """Uses the read-only tools of app.assistant.Assistant (same data, no model)."""

    def __init__(self, assistant) -> None:
        self.a = assistant

    async def _accounts(self) -> list[str]:
        async with self.a.Session() as s:
            return [a.name for a in await self.a._accounts(s)]

    async def answer(self, text: str) -> str:
        t = norm(text)
        accounts = await self._accounts()
        acct = next((n for n in accounts if n.lower() in t), "")
        if _has(t, "قواعد", "اعدادات", "rules", "settings", "قاعده") or re.search(r"\bsmart\d*\b", t):
            return await self.rules(t)
        if _has(t, "بيبر", "paper", "استراتيجي", "بوتات"):
            return await self.paper(t)
        if _has(t, "مفتوح", "ماسك", "شايل", "open", "positions", "بوزيشن"):
            return await self.open_positions(acct)
        if _has(t, "اخر صفق", "اخر الصفق", "last trade", "recent", "اخر") and _has(t, "صفق", "trade"):
            m = re.search(r"\d+", t)
            return await self.last_trades(acct, int(m.group()) if m else 5)
        if _has(t, "كسب", "خسر", "مكسب", "خساره", "ربح", "pnl", "profit", "loss", "عملت كام", "طلعت كام"):
            return await self.pnl(t, acct)
        if _has(t, "حساب", "account", "حاله", "status", "الحسابات"):
            return await self.accounts_text()
        coin = self._coin(text, accounts)
        if coin:
            return await self.coin(coin)
        return HELP

    # ---- the question kinds -------------------------------------------------------------------------------------
    async def pnl(self, t: str, acct: str) -> str:
        label, start, end = window(t)
        now = datetime.now(timezone.utc)
        async with self.a.Session() as s:
            q = select(LivePosition).where(LivePosition.status == "closed", LivePosition.closed_at >= now - start,
                                           LivePosition.closed_at < now - end)
            if acct:
                q = q.where(LivePosition.account == acct)
            rows = (await s.execute(q)).scalars().all()
        if not rows:
            return f"مفيش صفقات اتقفلت {label}{' على ' + acct if acct else ''}."
        lines = [f"المكسب والخسارة {label} (صفقات اتقفلت):"]
        by: dict[tuple[str, str], list[LivePosition]] = {}
        for p in rows:
            by.setdefault((p.account, p.mode), []).append(p)
        for (name, mode), ps in sorted(by.items()):
            tot = sum(p.pnl_usd or 0 for p in ps)
            wins = sum((p.pnl_usd or 0) > 0 for p in ps)
            tag = " (محاكاة)" if mode == "dry" else ""
            lines.append(f"- {name}{tag}: {_money(tot)} على {len(ps)} صفقة، {wins} كسبانة")
        best = max(rows, key=lambda p: p.pnl_usd or 0)
        worst = min(rows, key=lambda p: p.pnl_usd or 0)
        lines.append(f"أحسن صفقة: {best.symbol} {_money(best.pnl_usd or 0)} · أسوأ صفقة: {worst.symbol} "
                     f"{_money(worst.pnl_usd or 0)}")
        lines.append("ده المقفول بس؛ المفتوح: اسأل \"ايه الصفقات المفتوحة\".")
        return "\n".join(lines)

    async def open_positions(self, acct: str) -> str:
        data = await self.a.tool("get_accounts", {})
        lines = []
        for a in data["accounts"]:
            if acct and a["account"] != acct:
                continue
            ps = a["open_positions"]
            tag = " (محاكاة)" if a["mode"] == "dry" else ""
            if not ps:
                lines.append(f"{a['account']}{tag}: مفيش صفقات مفتوحة.")
                continue
            lines.append(f"{a['account']}{tag}: {len(ps)} صفقة مفتوحة بـ ${a['open_cost_usd']:,.2f}")
            for p in ps:
                now = f"قيمتها ${p['worth_now_usd']:,.2f} ({p['pnl_pct_now']:+.0f}%)" if p["worth_now_usd"] is not None \
                    else "مفيش سعر دلوقتي"
                sold = f"، باعت بـ ${p['sold_so_far_usd']:,.2f}" if p["sold_so_far_usd"] else ""
                lines.append(f"- {p['symbol']} من {p['wallet']}: دفعت ${p['cost_usd']:,.2f}{sold}، {now}")
        return "\n".join(lines) or "مفيش حسابات شغالة."

    async def last_trades(self, acct: str, n: int) -> str:
        n = min(max(n, 1), 15)
        data = await self.a.tool("get_trades", {"hours": 720, "account": acct})
        trades = data["trades"][:n]
        if not trades:
            return "مفيش صفقات اتقفلت آخر 30 يوم."
        lines = [f"آخر {len(trades)} صفقات اتقفلت{' على ' + acct if acct else ''}:"]
        for tr in trades:
            when = tr["closed"][5:16] if tr["closed"] else "?"
            lines.append(f"- {tr['symbol']} ({tr['account']}، {tr['wallet']}): {_money(tr['pnl_usd'])} "
                         f"({tr['pnl_pct']:+.0f}%) · {tr['exit']} · {when} UTC")
        return "\n".join(lines)

    async def rules(self, t: str) -> str:
        data = await self.a.tool("get_wallet_rules", {})
        lines = []
        for a in data:
            for w in a["wallets"]:
                if re.search(r"smart\d*", t) and w["label"].lower() not in t:
                    continue
                lines.append(f"- {a['account']} · {w['label']}: {w['rules']}")
        return ("قواعد المحافظ:\n" + "\n".join(lines)) if lines else "ملقتش المحفظة دي. جرّب \"قواعد المحافظ\"."

    async def paper(self, t: str) -> str:
        label, start, _ = window(t)
        days = max(1, min(30, round(start.total_seconds() / 86400)))
        data = await self.a.tool("get_paper_results", {"days": days})
        rows = [r for r in data["strategies"] if r.get("closed")]
        if not rows:
            return "مفيش صفقات paper اتقفلت في الفترة دي."
        rows.sort(key=lambda r: r.get("realized_usd") or 0, reverse=True)

        def line(r):
            wr = f"{(r.get('win_rate') or 0) * 100:.0f}%"
            p = " (متوقف)" if r.get("paused") else ""
            return f"- {r['name']}{p}: {_money(r.get('realized_usd') or 0)} على {r['closed']} صفقة، {wr} كسبانة"

        out = [f"نتايج الـ paper ({days} يوم) · الأحسن:"] + [line(r) for r in rows[:5]]
        losers = [r for r in rows if (r.get("realized_usd") or 0) < 0]
        if losers:
            out += ["الأسوأ:"] + [line(r) for r in losers[-3:]]
        return "\n".join(out)

    async def accounts_text(self) -> str:
        data = await self.a.tool("get_accounts", {})
        lines = ["الحسابات:"]
        for a in data["accounts"]:
            tag = "محاكاة" if a["mode"] == "dry" else "حقيقي"
            state = "متوقف" if a["paused"] else "شغال"
            lines.append(f"- {a['account']} ({tag}، {state}): ${a['entry_usd']:g} للصفقة، مفتوح "
                         f"${a['open_cost_usd']:,.2f} من ${a['max_open_usd']:,.0f}، آخر 24 ساعة "
                         f"{_money(a['realized_pnl_24h_usd'])}")
        return "\n".join(lines)

    def _coin(self, text: str, accounts: list[str]) -> str | None:
        m = ADDR_RE.search(text)
        if m:
            return m.group()
        skip = {a.lower() for a in accounts} | {"pnl", "paper", "mirror", "stop", "dry", "live", "usd", "sol"}
        m = re.search(r"\$([A-Za-z0-9]{2,15})", text)
        if m:
            return m.group(1)
        for w in re.findall(r"\b[A-Za-z][A-Za-z0-9]{1,14}\b", text):
            if w.lower() not in skip and not w.lower().startswith("smart"):
                return w
        return None

    async def coin(self, q: str) -> str:
        data = await self.a.tool("lookup_coin", {"query": q})
        pos, trades, scan = data["positions"], data["wallet_trades"], data["scanner"]
        if not (pos or trades or scan):
            return f"ملقتش حاجة عن {q} في البوت. جرّب اسمها زي ما بيظهر في الإشعارات أو عنوانها."
        lines = []
        name = (pos[0]["symbol"] if pos else trades[0]["symbol"] if trades else scan[0]["symbol"]) or q
        lines.append(f"{name}:")
        opened = [p for p in pos if p["status"] == "open"]
        if opened:
            live = await self.a.tool("get_accounts", {})
            now = {(a["account"], p["address"]): p for a in live["accounts"] for p in a["open_positions"]}
            for p in opened:
                cur = now.get((p["account"], p["address"]))
                v = f"قيمتها ${cur['worth_now_usd']:,.2f} ({cur['pnl_pct_now']:+.0f}%)" \
                    if cur and cur["worth_now_usd"] is not None else "مفيش سعر دلوقتي"
                lines.append(f"- مفتوحة على {p['account']}: دفعت ${p['cost_usd']:,.2f}، {v}")
        for p in [p for p in pos if p["status"] == "closed"][:3]:
            lines.append(f"- اتقفلت على {p['account']}: {_money(p['pnl_usd'] or 0)} ({p['exit']})")
        for tr in trades[:4]:
            side = "اشترت" if tr["side"] == "buy" else "باعت"
            pct = f" {tr['pct_sold']:.0f}%" if tr["side"] == "sell" and tr.get("pct_sold") else ""
            lines.append(f"- {tr['wallet']} {side}{pct} بـ ${tr['usd']:,.0f} · {tr['at'][5:16] if tr['at'] else '?'} UTC")
        if scan:
            sc = scan[0]
            liq = f"liquidity ${sc['liquidity_usd'] / 1000:,.0f}k" if sc.get("liquidity_usd") else ""
            mc = f"market cap ${sc['market_cap'] / 1e6:,.2f}M" if sc.get("market_cap") else ""
            extra = " · ".join(x for x in (liq, mc) if x)
            if extra:
                lines.append(f"- {extra}")
            lines.append(f"- {sc['address']}")
        return "\n".join(lines)
