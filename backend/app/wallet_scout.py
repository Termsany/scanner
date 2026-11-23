"""Wallet scout: find smart wallets worth copying, without trusting a nice-looking number.

Once a day (SCOUT_ENABLED, at SCOUT_HOUR_UTC):

1. Find: wallets that bought early in recent winners (our profitable live trades, paper trades that ran +200%):
   the swaps of each winner token just before our / the leader's buy.
2. Strict filter, from each wallet's own swaps (Helius, valued in USD: SOL and stablecoins), over several days:
   copyable pace (no HFT bots), enough closed trades, profitable even without its best trade, profit factor,
   win rate, holds long enough for a copy that is seconds late.
3. Paper trial: a wallet that passes is tracked quietly (no Telegram alerts) and copied on paper for a few days
   with live-like entries (the realistic paper engine) and the live exit rule ("scout_trial").
4. Judge: after the trial, passed (profitable on paper) or failed. Passed wallets are suggested on /admin; the
   operator adds them to an account by hand. Nothing here ever trades real money.
"""

from __future__ import annotations

import asyncio
import logging
import math
import statistics as st
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select

from app.config import Settings, get_settings
from app.models import (Chain, LivePosition, PaperPosition, SmartWallet, TradingAccount, TrackerCursor,
                        WalletCandidate, WalletScore, WalletSource, WalletTrade)
from app.repository import utc

log = logging.getLogger("scout")

WSOL = "So11111111111111111111111111111111111111112"
STABLES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCeUBenwNYB"}
TRIAL_STRATEGY = "scout_trial"
SCOUT_LABEL = "scout trial"
RUN_KEY = "scout:last_run"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---- scoring (pure) ---------------------------------------------------------------------------------------
def summarize(txs: list[dict[str, Any]], wallet: str, sol_usd: float, now_ts: float | None = None) -> dict[str, Any]:
    """A wallet's Helius-parsed swaps -> round trips per token in USD (SOL and stablecoin legs; swaps against any
    other token are skipped) -> the numbers the filter looks at."""
    tok: dict[str, dict[str, float]] = defaultdict(lambda: {"in": 0.0, "out": 0.0, "b": 0.0, "s": 0.0,
                                                            "t0": 0.0, "t1": 0.0})
    times = [t.get("timestamp") or 0 for t in txs if t.get("timestamp")]
    for t in txs:
        if t.get("transactionError"):
            continue
        usd = 0.0
        for a in t.get("accountData") or []:
            if a.get("account") == wallet:
                usd += (a.get("nativeBalanceChange") or 0) / 1e9 * sol_usd
        if t.get("feePayer") == wallet:
            usd += (t.get("fee") or 0) / 1e9 * sol_usd
        moved: dict[str, float] = {}
        for tr in t.get("tokenTransfers") or []:
            sign = 1 if tr.get("toUserAccount") == wallet else -1 if tr.get("fromUserAccount") == wallet else 0
            if not sign:
                continue
            m, amt = tr.get("mint"), float(tr.get("tokenAmount") or 0)
            if m == WSOL:
                usd += sign * amt * sol_usd
            elif m in STABLES:
                usd += sign * amt
            else:
                moved[m] = moved.get(m, 0.0) + sign * amt
        moved = {m: v for m, v in moved.items() if abs(v) > 1e-9}
        if len(moved) != 1:
            continue
        (m, amt), = moved.items()
        d, ts = tok[m], t.get("timestamp") or 0
        if amt > 0 and usd < -0.5:
            d["in"] += -usd
            d["b"] += amt
            d["t0"] = min(d["t0"] or ts, ts)
        elif amt < 0 and usd > 0.5:
            d["out"] += usd
            d["s"] += -amt
            d["t1"] = max(d["t1"], ts)
    closed = [d for d in tok.values() if d["in"] > 1 and d["b"] > 0 and d["s"] >= d["b"] * 0.95]
    res = sorted(d["out"] - d["in"] for d in closed)
    wins = [x for x in res if x > 0]
    losses = [x for x in res if x <= 0]
    days = (max(times) - min(times)) / 86400 if len(times) > 1 else 0.0
    holds = [(d["t1"] - d["t0"]) / 60 for d in closed if d["t0"] and d["t1"] >= d["t0"]]
    sizes = [d["in"] for d in tok.values() if d["in"] > 0]
    net = sum(res)
    # per-trade return (profit / cost) and how steady it is (arXiv 2601.08641: copy wallets whose average return is
    # statistically above zero, t > 1.645, and not a lottery, return std < 1)
    rets = [(d["out"] - d["in"]) / d["in"] for d in closed]
    mean_r = st.mean(rets) if rets else 0.0
    std_r = st.stdev(rets) if len(rets) >= 2 else 0.0
    t_stat = mean_r / (std_r / math.sqrt(len(rets))) if len(rets) >= 2 and std_r > 0 else 0.0
    newest = max(times) if times else 0
    return {
        "swaps": len(txs), "days": round(days, 2), "per_day": round(len(txs) / max(days, 0.01), 1),
        "tokens": len(tok), "closed": len(closed),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        "net_usd": round(net, 2), "net_wo_best": round(net - (res[-1] if res and res[-1] > 0 else 0.0), 2),
        "profit_factor": round(sum(wins) / -sum(losses), 2) if wins and losses else (99.0 if wins else 0.0),
        "turnover_usd": round(sum(d["in"] for d in closed), 2),
        "hold_min": round(st.median(holds), 1) if holds else 0.0,
        "buy_usd": round(st.median(sizes), 2) if sizes else 0.0,
        "mean_ret_pct": round(mean_r * 100, 1), "ret_std": round(std_r, 2), "t_stat": round(t_stat, 2),
        "idle_h": round(((now_ts or time.time()) - newest) / 3600, 1) if newest else None,
    }


def checks(m: dict[str, Any], s: Settings) -> list[dict[str, Any]]:
    """Every test of the strict filter, passed or not, for the scorecard (in the order judge() applies them)."""
    def row(key: str, ok: bool, label: str) -> dict[str, Any]:
        return {"key": key, "ok": bool(ok), "label": label}

    idle = m.get("idle_h")
    return [
        row("history", m["days"] >= s.scout_min_days or m["closed"] >= 3 * s.scout_min_closed,
            f"{m['days']:.1f} days of history (need {s.scout_min_days:g})"),
        row("pace", m["per_day"] <= s.scout_max_swaps_per_day,
            f"{m['per_day']:.0f} swaps/day (bot above {s.scout_max_swaps_per_day:g})"),
        row("trades", m["closed"] >= s.scout_min_closed, f"{m['closed']} closed trades (need {s.scout_min_closed})"),
        row("net", m["net_usd"] > 0, f"net ${m['net_usd']:,.0f}"),
        row("without_best", m["net_wo_best"] > 0, f"${m['net_wo_best']:,.0f} without its best trade"),
        row("profit_factor", m["profit_factor"] >= s.scout_min_profit_factor,
            f"profit factor {m['profit_factor']} (need {s.scout_min_profit_factor:g})"),
        row("win_rate", m["win_rate"] >= s.scout_min_win_rate,
            f"win rate {m['win_rate']:.0f}% (need {s.scout_min_win_rate:g}%)"),
        row("hold", m["hold_min"] >= s.scout_min_hold_minutes,
            f"median hold {m['hold_min']:.1f} min (need {s.scout_min_hold_minutes:g})"),
        row("t_stat", m.get("t_stat", 0.0) >= s.scout_min_tstat,
            f"t-stat {m.get('t_stat', 0.0):.2f}: average return {m.get('mean_ret_pct', 0.0):+.0f}% "
            f"(need {s.scout_min_tstat:g}: steady, not luck)"),
        row("steady", m.get("ret_std", 9.0) <= s.scout_max_ret_std,
            f"return spread {m.get('ret_std', 0.0):.2f} (max {s.scout_max_ret_std:g}: not a lottery wallet)"),
        row("active", idle is not None and idle <= s.scout_max_idle_hours,
            f"last swap {idle:.0f} h ago (max {s.scout_max_idle_hours:g})" if idle is not None else "no recent swap"),
    ]


def judge(m: dict[str, Any], s: Settings) -> tuple[bool, str]:
    """The strict filter. Returns (passed, why not / summary)."""
    for c in checks(m, s):
        if not c["ok"]:
            return False, c["label"]
    return True, (f"{m['closed']} trades, {m['win_rate']:.0f}% wins, net ${m['net_usd']:,.0f} "
                  f"(${m['net_wo_best']:,.0f} without the best), PF {m['profit_factor']}, "
                  f"t {m.get('t_stat', 0):.1f}, hold {m['hold_min']:.0f} min")


def trial_result(rows: list[PaperPosition]) -> dict[str, Any]:
    closed = [p for p in rows if p.status == "closed"]
    net = sum(p.pnl_usd or 0.0 for p in closed)
    unreal = 0.0
    for p in rows:
        if p.status == "open" and p.last_price and p.fill_price:
            frac = 1.0 if p.remaining_frac is None else p.remaining_frac
            unreal += (p.proceeds_usd or 0.0) + p.size_usd * frac / p.fill_price * p.last_price - p.size_usd
    size = rows[0].size_usd if rows else 0.0
    return {"trades": len(rows), "closed": len(closed), "wins": sum(1 for p in closed if (p.pnl_usd or 0) > 0),
            "net_usd": round(net, 2), "open_usd": round(unreal, 2), "size_usd": size}


# ---- the daily run ------------------------------------------------------------------------------------------
class WalletScout:
    def __init__(self, settings: Settings | None = None) -> None:
        self.s = settings or get_settings()
        self.calls = 0

    async def _get(self, c: httpx.AsyncClient, path: str, **params) -> Any:
        if self.calls >= self.s.scout_max_calls:
            return None
        key = self.s.live_rpc_url.split("api-key=")[1].split("&")[0]
        for attempt in range(3):
            await asyncio.sleep(0.4)            # the trading engine shares this Helius key
            self.calls += 1
            r = await c.get(f"https://api.helius.xyz/v0{path}", params={**params, "api-key": key})
            if r.status_code == 429:
                await asyncio.sleep(3 * (attempt + 1))
                continue
            return r.json() if r.status_code == 200 else None
        return None

    async def _history(self, c: httpx.AsyncClient, wallet: str) -> list[dict[str, Any]]:
        """Swaps back until they cover SCOUT_MIN_DAYS (+ a margin), up to SCOUT_HISTORY_PAGES pages of 100: a busy
        but human wallet needs more pages than a slow one; a bot stops early on the pace check."""
        txs, before = [], None
        want = self.s.scout_min_days * 86400 * 1.1
        for _ in range(self.s.scout_history_pages):
            page = await self._get(c, f"/addresses/{wallet}/transactions", type="SWAP", limit=100,
                                   **({"before": before} if before else {}))
            if not isinstance(page, list) or not page:
                break
            txs += page
            before = page[-1]["signature"]
            span = (txs[0].get("timestamp", 0) - txs[-1].get("timestamp", 0))
            if span >= want:
                break
            if span > 0 and len(txs) / (span / 86400) > self.s.scout_max_swaps_per_day * 2:
                break                            # far past the bot line: more pages would not change the verdict
        return txs

    async def judge_trials(self, session) -> int:
        done = 0
        for c in (await session.execute(select(WalletCandidate).where(WalletCandidate.status == "trial"))).scalars().all():
            if c.trial_end and utc(c.trial_end) > _now():
                continue
            rows = (await session.execute(select(PaperPosition).where(
                PaperPosition.strategy == TRIAL_STRATEGY, PaperPosition.trigger_wallet == c.address,
                PaperPosition.entry_time >= c.trial_start))).scalars().all()
            res = trial_result(rows)
            c.trial = res
            good = res["trades"] >= self.s.scout_trial_min_trades and res["net_usd"] + res["open_usd"] > 0
            c.status = "passed" if good else "failed"
            c.reason = (f"paper trial: {res['trades']} trades, net ${res['net_usd'] + res['open_usd']:+.2f} "
                        f"on ${res['size_usd']:.0f} each" if res["trades"] else "paper trial: it made no trade")
            sw = (await session.execute(select(SmartWallet).where(SmartWallet.address == c.address,
                                                                  SmartWallet.source == WalletSource.scout))).scalars().first()
            if sw is not None:
                sw.track_buys = False            # stop polling it; adding it to an account tracks it again
            done += 1
            log.info("scout: %s %s (%s)", c.address[:8], c.status, c.reason)
        return done

    async def discover(self, session, c: httpx.AsyncClient) -> Counter:
        since = _now() - timedelta(days=self.s.scout_lookback_days)
        refs: dict[str, str] = {}
        for p in (await session.execute(select(LivePosition).where(
                LivePosition.mode == "live", LivePosition.status == "closed", LivePosition.pnl_usd > 3,
                LivePosition.opened_at >= since, LivePosition.buy_signature.is_not(None)))).scalars().all():
            refs.setdefault(p.token_address, p.buy_signature)
        for p in (await session.execute(select(PaperPosition).where(
                PaperPosition.entry_time >= since, PaperPosition.peak_pct >= 200,
                PaperPosition.leader_trade_id.is_not(None)).order_by(PaperPosition.peak_pct.desc()).limit(40))).scalars().all():
            if p.token_address in refs:
                continue
            t = await session.get(WalletTrade, p.leader_trade_id)
            if t is not None and t.tx_hash:
                refs[p.token_address] = t.tx_hash
        hits: Counter = Counter()
        for mint, sig in list(refs.items())[:25]:
            before, seen = sig, set()
            for _ in range(2):
                txs = await self._get(c, f"/addresses/{mint}/transactions", type="SWAP", limit=100, before=before)
                if not isinstance(txs, list) or not txs:
                    break
                for t in txs:
                    if t.get("transactionError"):
                        continue
                    for tr in t.get("tokenTransfers") or []:
                        w = tr.get("toUserAccount")
                        if tr.get("mint") == mint and w and float(tr.get("tokenAmount") or 0) > 0:
                            seen.add(w)
                before = txs[-1]["signature"]
            hits.update(seen)
        log.info("scout: %d winners, %d early wallets", len(refs), len(hits))
        return hits

    async def run_once(self, session) -> dict[str, int]:
        from app.solana_pda import is_on_curve, pubkey_bytes

        self.calls = 0
        out = {"judged": await self.judge_trials(session), "found": 0, "scored": 0, "trials": 0}
        await session.commit()
        ours, leaders = set(), set()
        for a in (await session.execute(select(TradingAccount))).scalars().all():
            if a.pubkey:
                ours.add(a.pubkey)
            leaders |= {d.get("wallet") for d in a.leaders or []}
        retry = []                               # rejected only for a short history: look again with more pages
        known = set()
        for c in (await session.execute(select(WalletCandidate))).scalars().all():
            if c.status == "rejected" and "days of history" in (c.reason or ""):
                retry.append(c)
            else:
                known.add(c.address)

        def real(w: str) -> bool:
            try:
                return is_on_curve(pubkey_bytes(w))
            except Exception:
                return False

        sol_usd = 0.0
        async with httpx.AsyncClient(timeout=30) as c:
            try:
                r = await c.get(f"{self.s.jupiter_api_url}/price/v3", params={"ids": WSOL})
                sol_usd = float(r.json()[WSOL]["usdPrice"])
            except Exception:
                return out
            hits = await self.discover(session, c)
            old = {c.address: c for c in retry}
            fresh = [(w, max(n, old[w].hits if w in old else 0)) for w, n in hits.most_common()
                     if n >= self.s.scout_min_hits and w not in ours | leaders | known and real(w)]
            fresh += [(c.address, c.hits) for c in retry if c.address not in {w for w, _ in fresh}]
            out["found"] = len(fresh)
            trials = (await session.execute(select(WalletCandidate).where(WalletCandidate.status == "trial"))).scalars().all()
            room = self.s.scout_max_trials - len(trials)
            for w, n in fresh:
                if self.calls >= self.s.scout_max_calls:
                    break
                cand = old.get(w)
                if cand is None:
                    cand = WalletCandidate(address=w, source="early buyer of winners", hits=n)
                    session.add(cand)
                else:
                    cand.hits, cand.status = n, "new"
                txs = await self._history(c, w)
                if len(txs) < 10:
                    cand.status, cand.reason = "rejected", "too few swaps to judge"
                    continue
                m = summarize(txs, w, sol_usd)
                cand.metrics, cand.scored_at = m, _now()
                ok, why = judge(m, self.s)
                out["scored"] += 1
                if not ok:
                    cand.status, cand.reason = "rejected", why
                    continue
                if room <= 0:
                    cand.status, cand.reason = "new", f"passed the filter, waiting for a trial slot: {why}"
                    continue
                await self._start_trial(session, cand, why)
                room -= 1
                out["trials"] += 1
            # waiting ones take free slots (best first)
            if room > 0:
                waiting = (await session.execute(select(WalletCandidate).where(
                    WalletCandidate.status == "new", WalletCandidate.metrics.is_not(None)))).scalars().all()
                for cand in sorted(waiting, key=lambda x: -(x.metrics or {}).get("net_wo_best", 0))[:room]:
                    await self._start_trial(session, cand, cand.reason.split(": ", 1)[-1])
                    out["trials"] += 1
        await session.commit()
        out["helius_calls"] = self.calls
        log.info("scout: %s", out)
        return out

    async def _start_trial(self, session, cand: WalletCandidate, why: str) -> None:
        cand.status, cand.reason = "trial", f"on paper trial: {why}"
        cand.trial_start = _now()
        cand.trial_end = cand.trial_start + timedelta(days=self.s.scout_trial_days)
        sw = (await session.execute(select(SmartWallet).where(SmartWallet.chain == Chain.solana,
                                                              SmartWallet.address == cand.address))).scalars().first()
        if sw is None:
            session.add(SmartWallet(chain=Chain.solana, address=cand.address, label=SCOUT_LABEL,
                                    source=WalletSource.scout, enabled=False, track_buys=True))
        elif not sw.track_buys:
            sw.track_buys = True

    async def run_forever(self) -> None:
        if not self.s.scout_enabled:
            return
        from app.db import SessionLocal

        log.info("scout: on (daily at %02d:00 UTC, at most %d Helius calls)", self.s.scout_hour_utc, self.s.scout_max_calls)
        while True:
            try:
                now = _now()
                today = now.strftime("%Y-%m-%d")
                async with SessionLocal() as session:
                    row = await session.get(TrackerCursor, RUN_KEY)
                    due = now.hour >= self.s.scout_hour_utc and (row is None or row.value != today)
                    if due:
                        if row is None:
                            session.add(TrackerCursor(key=RUN_KEY, value=today))
                        else:
                            row.value = today
                        await session.commit()
                        await self.run_once(session)
                    elif row is not None:
                        await self.judge_trials(session)       # trials end through the day too
                        await session.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scout run failed")
            await asyncio.sleep(600)


async def score_wallet(session, wallet: str, settings: Settings | None = None,
                       scout: WalletScout | None = None) -> WalletScore:
    """Scorecard for one wallet: its swap history from Helius, the strict filter's numbers and every check. Stored
    (wallet_scores) and returned. Read-only on chain; nothing is copied or traded."""
    s = settings or get_settings()
    scout = scout or WalletScout(s)
    row = await session.get(WalletScore, wallet) or WalletScore(address=wallet)
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{s.jupiter_api_url}/price/v3", params={"ids": WSOL})
        sol_usd = float(r.json()[WSOL]["usdPrice"])
        txs = await scout._history(c, wallet)
    if len(txs) < 10:
        row.metrics, row.checks, row.passed = {"swaps": len(txs)}, [], False
        row.reason = f"only {len(txs)} swaps found: too few to judge"
    else:
        m = summarize(txs, wallet, sol_usd)
        row.metrics, row.checks = m, checks(m, s)
        row.passed, row.reason = judge(m, s)
        row.reason = row.reason[:240]
    row.scored_at = _now()
    session.add(row)
    await session.commit()
    return row


async def trial_wallets(session) -> set[str]:
    return {c.address for c in (await session.execute(select(WalletCandidate.address).where(
        WalletCandidate.status == "trial"))).all()}
