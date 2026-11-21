"""Paper trading: simulated entries and exits (no real money, no keys, no transactions).

Several strategies run side by side on the same live data so their results can be compared:

  wallet_follow   a tracked wallet buys            -> exit at TP / SL / max hold, or when that wallet sells
  wallet_cluster  >= 2 tracked wallets buy the same token within CLUSTER_MINUTES -> TP / SL / max hold
  wallet_mirror   a PAPER_MIRROR_WALLETS wallet buys -> sell the same share it sells (no TP / SL)
  signal_80       scanner signal with score >= 80  -> TP / SL / max hold
  signal_85       scanner signal with score >= 85  -> TP / SL / max hold

Entries use the market price recorded with the trigger (the wallet trade or the signal); every fill pays
PAPER_SLIPPAGE_PCT on entry and again on exit. Open positions are re-priced from DexScreener every
PAPER_INTERVAL_SECONDS. Only triggers that happen after the engine first starts are traded (no backfill).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models import Chain, PaperPosition, Signal, Token, TrackerCursor, WalletTrade
from app.repository import utc

log = logging.getLogger("paper")

CLUSTER_MINUTES = 10
DULX = "DULX3e6vt1ZpHq3AdCUBZLPZ1CrhApdNKcewMPxZQRXf"
CLUSTER_MIN_WALLETS = 2
CURSOR_TRADES = "paper:wallet_trades"
CURSOR_SIGNALS = "paper:signals"


@dataclass(frozen=True)
class Strategy:
    name: str
    title: str
    tp_pct: float = 50.0
    sl_pct: float = -25.0
    max_hold_minutes: int = 360
    min_score: int | None = None
    mirror: bool = False        # no TP / SL: sell the same share the leader sells
    leader: str | None = None   # only this wallet's buys (first buy of a token)
    min_usd: float | None = None   # ... and only when its buy is this big
    max_usd: float | None = None
    live_rule: bool = False     # exit like the live engine: leader trims before 2x -> follow every leader sell;
                                # otherwise 50% at 2x, the rest only on the leader's full exit


# priced and closed by their own loops (app/sniper.py, app/grad.py), second by second
SELF_PRICED = ("snipe_all", "snipe_momentum", "grad_all", "grad_confirm")

STRATEGIES: dict[str, Strategy] = {s.name: s for s in (
    Strategy("wallet_follow", "Follow a tracked wallet (exit when it sells)"),
    Strategy("wallet_cluster", f"{CLUSTER_MIN_WALLETS}+ tracked wallets buy within {CLUSTER_MINUTES} min"),
    Strategy("wallet_mirror", "Copy the wallet: buy with it, sell the same share when it sells",
             tp_pct=0.0, sl_pct=0.0, mirror=True),
    # DULX3e test (user's request 2026-11-21): same exits as live; all buys vs only its $50-1000 buys
    Strategy("dulx_all", "Copy DULX3e like live (50% at 2x, rest on its full exit)", tp_pct=0.0, sl_pct=0.0,
             leader=DULX, live_rule=True),
    Strategy("dulx_50_1000", "Copy DULX3e like live, only its $50-1000 buys", tp_pct=0.0, sl_pct=0.0,
             leader=DULX, min_usd=50.0, max_usd=1000.0, live_rule=True),
    Strategy("signal_80", "Scanner signal, score >= 80", min_score=80),
    Strategy("signal_85", "Scanner signal, score >= 85", min_score=85),
    # run by app/sniper.py (own live feed and exits); listed here so /paper shows them
    Strategy("snipe_all", "Sniper: sample of every new pump.fun token", tp_pct=50.0, sl_pct=-30.0, max_hold_minutes=5),
    Strategy("grad_all", "Graduation: every pump.fun token just moved to PumpSwap", tp_pct=50.0, sl_pct=-20.0,
             max_hold_minutes=30),
    Strategy("grad_confirm", "Graduation: just moved to PumpSwap and still rising a minute later", tp_pct=50.0,
             sl_pct=-20.0, max_hold_minutes=30),
    Strategy("snipe_momentum", "Sniper: new pump.fun token with early buying", tp_pct=50.0, sl_pct=-30.0,
             max_hold_minutes=5),
)}

# (chain, [addresses]) -> {address: price}
PriceFetcher = Callable[[Chain, list[str]], Awaitable[dict[str, float]]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_buy(t: WalletTrade) -> bool:
    return (t.side or "buy") == "buy" and (t.kind or "buy") == "buy"


def pnl(size_usd: float, entry_price: float, exit_price: float, slippage_pct: float) -> tuple[float, float]:
    """Net P&L in USD and % after paying slippage on both fills."""
    fill_in = entry_price * (1 + slippage_pct / 100)
    fill_out = exit_price * (1 - slippage_pct / 100)
    usd = size_usd / fill_in * fill_out - size_usd
    return usd, usd / size_usd * 100


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------


async def _cursor(session: AsyncSession, key: str, current_max: int) -> int | None:
    """Last processed id; the first time, start from the current max (trade from now on only)."""
    row = await session.get(TrackerCursor, key)
    if row is None:
        session.add(TrackerCursor(key=key, value=str(current_max)))
        return None
    return int(row.value)


async def _set_cursor(session: AsyncSession, key: str, value: int) -> None:
    row = await session.get(TrackerCursor, key)
    if row is None:
        session.add(TrackerCursor(key=key, value=str(value)))
    else:
        row.value = str(value)


async def _open_keys(session: AsyncSession) -> set[tuple[str, str, str]]:
    rows = (await session.execute(
        select(PaperPosition.strategy, PaperPosition.chain, PaperPosition.token_address)
        .where(PaperPosition.status == "open")
    )).all()
    return {(s, c.value if isinstance(c, Chain) else str(c), a) for s, c, a in rows}


def _open(session: AsyncSession, keys: set, strategy: Strategy, *, chain: Chain, token: str, symbol: str,
          price: float, trigger: str, at: datetime, s: Settings, wallet: str | None = None,
          leader_trade_id: int | None = None) -> PaperPosition | None:
    key = (strategy.name, chain.value, token)
    if key in keys or not price or price <= 0:
        return None
    keys.add(key)
    pos = PaperPosition(
        strategy=strategy.name, chain=chain, token_address=token, symbol=symbol or "", trigger=trigger[:160],
        trigger_wallet=wallet, status="open", size_usd=s.paper_size_usd, entry_time=at, entry_price=price,
        fill_price=price * (1 + s.paper_slippage_pct / 100), tp_pct=strategy.tp_pct, sl_pct=strategy.sl_pct,
        max_hold_minutes=(s.paper_mirror_max_hold_hours * 60 if strategy.mirror or strategy.live_rule
                          else strategy.max_hold_minutes),
        last_price=price, last_priced_at=at, peak_pct=0.0, trough_pct=0.0,
        remaining_frac=1.0, proceeds_usd=0.0, leader_trade_id=leader_trade_id,
    )
    session.add(pos)
    log.info("paper OPEN %s %s %s @ %.10g (%s)", strategy.name, chain.value, symbol or token, price, trigger)
    return pos


async def process_entries(session: AsyncSession, s: Settings | None = None) -> int:
    s = s or get_settings()
    opened = 0
    keys = await _open_keys(session)
    now = _now()
    mirror_set = set(s.paper_mirror_wallets)

    # --- tracked-wallet buys ------------------------------------------------
    max_trade = (await session.execute(select(func.max(WalletTrade.id)))).scalar() or 0
    since = await _cursor(session, CURSOR_TRADES, max_trade)
    if since is not None and max_trade > since:
        trades = (await session.execute(
            select(WalletTrade).where(WalletTrade.id > since).order_by(WalletTrade.id)
        )).scalars().all()
        for t in trades:
            if not _is_buy(t) or not t.price_usd:
                continue
            label = t.wallet_label or t.wallet[:6]
            usd = f" ${t.usd_value:,.0f}" if t.usd_value else ""
            if _open(session, keys, STRATEGIES["wallet_follow"], chain=t.chain, token=t.token_address,
                     symbol=t.symbol, price=t.price_usd, trigger=f"{label} bought{usd}", at=now, s=s,
                     wallet=t.wallet):
                opened += 1
            if (not mirror_set or t.wallet in mirror_set) and _open(
                    session, keys, STRATEGIES["wallet_mirror"], chain=t.chain, token=t.token_address,
                    symbol=t.symbol, price=t.price_usd, trigger=f"copy {label}{usd}", at=now, s=s,
                    wallet=t.wallet, leader_trade_id=t.id):
                opened += 1
            for strat in STRATEGIES.values():          # one leader's first buys, maybe sized
                if strat.leader != t.wallet or not t.first_buy:
                    continue
                if strat.min_usd is not None and (t.usd_value or 0) < strat.min_usd:
                    continue
                if strat.max_usd is not None and (t.usd_value or 0) > strat.max_usd:
                    continue
                if _open(session, keys, strat, chain=t.chain, token=t.token_address, symbol=t.symbol,
                         price=t.price_usd, trigger=f"copy {label}{usd}", at=now, s=s, wallet=t.wallet,
                         leader_trade_id=t.id):
                    opened += 1
            # cluster: distinct tracked wallets buying this token in the last CLUSTER_MINUTES
            ref = utc(t.block_time) or utc(t.detected_at) or now
            window_start = ref - timedelta(minutes=CLUSTER_MINUTES)
            recent = (await session.execute(
                select(WalletTrade.wallet, WalletTrade.wallet_label, WalletTrade.side, WalletTrade.kind)
                .where(WalletTrade.chain == t.chain, WalletTrade.token_address == t.token_address,
                       WalletTrade.id <= t.id,
                       or_(WalletTrade.block_time >= window_start,
                           (WalletTrade.block_time.is_(None)) & (WalletTrade.detected_at >= window_start)))
            )).all()
            buyers = {}
            for w, wl, side, kind in recent:
                if (side or "buy") == "buy" and (kind or "buy") == "buy":
                    buyers[w] = wl or w[:6]
            if len(buyers) >= CLUSTER_MIN_WALLETS:
                names = ", ".join(list(buyers.values())[:4])
                if _open(session, keys, STRATEGIES["wallet_cluster"], chain=t.chain, token=t.token_address,
                         symbol=t.symbol, price=t.price_usd, trigger=f"{len(buyers)} wallets: {names}", at=now, s=s):
                    opened += 1
        await _set_cursor(session, CURSOR_TRADES, max_trade)

    # --- scanner signals ----------------------------------------------------
    max_sig = (await session.execute(select(func.max(Signal.id)))).scalar() or 0
    since = await _cursor(session, CURSOR_SIGNALS, max_sig)
    if since is not None and max_sig > since:
        rows = (await session.execute(
            select(Signal, Token).join(Token, Token.id == Signal.token_id)
            .where(Signal.id > since).order_by(Signal.id)
        )).all()
        for sig, tok in rows:
            for strat in STRATEGIES.values():
                if strat.min_score is None or sig.score < strat.min_score:
                    continue
                if _open(session, keys, strat, chain=tok.chain, token=tok.address, symbol=tok.symbol,
                         price=sig.price_usd or 0.0, trigger=f"score {sig.score}", at=now, s=s):
                    opened += 1
        await _set_cursor(session, CURSOR_SIGNALS, max_sig)
    return opened


# ---------------------------------------------------------------------------
# Exits
# ---------------------------------------------------------------------------


def _value(pos: PaperPosition, price: float, s: Settings) -> float:
    """Proceeds already banked plus what the remaining share would fetch at `price` (after slippage)."""
    frac = 1.0 if pos.remaining_frac is None else pos.remaining_frac
    return (pos.proceeds_usd or 0.0) + pos.size_usd * frac / pos.fill_price * price * (1 - s.paper_slippage_pct / 100)


def _close(pos: PaperPosition, price: float, reason: str, at: datetime, s: Settings) -> None:
    pos.pnl_usd = _value(pos, price, s) - pos.size_usd
    pos.pnl_pct = pos.pnl_usd / pos.size_usd * 100
    pos.proceeds_usd = pos.pnl_usd + pos.size_usd
    pos.remaining_frac = 0.0
    pos.status = "closed"
    pos.exit_time = at
    pos.exit_price = price
    pos.exit_reason = reason
    log.info("paper CLOSE %s %s %s: %s %.1f%% ($%.2f)", pos.strategy, pos.chain.value, pos.symbol or pos.token_address,
             reason, pos.pnl_pct, pos.pnl_usd)


async def process_exits(session: AsyncSession, prices: PriceFetcher, s: Settings | None = None) -> int:
    s = s or get_settings()
    open_pos = (await session.execute(select(PaperPosition).where(
        PaperPosition.status == "open", PaperPosition.strategy.notin_(SELF_PRICED)))).scalars().all()
    if not open_pos:            # (sniper / grad positions are priced by their own loops)
        return 0
    now = _now()

    by_chain: dict[Chain, set[str]] = defaultdict(set)
    for p in open_pos:
        by_chain[p.chain].add(p.token_address)
    live: dict[tuple[Chain, str], float] = {}
    for chain, addrs in by_chain.items():
        addr_list = sorted(addrs)
        for i in range(0, len(addr_list), 30):
            try:
                got = await prices(chain, addr_list[i:i + 30])
            except Exception:
                log.warning("paper: price fetch failed for %s", chain.value, exc_info=True)
                got = {}
            for a, px in got.items():
                if px and px > 0:
                    live[(chain, a)] = px

    closed = 0
    for p in open_pos:
        entry_time = utc(p.entry_time)
        px = live.get((p.chain, p.token_address))
        if px is not None:
            p.last_price, p.last_priced_at = px, now
            pct = (px / p.entry_price - 1) * 100
            p.peak_pct = max(p.peak_pct or 0.0, pct)
            p.trough_pct = min(p.trough_pct or 0.0, pct)

        strat = STRATEGIES.get(p.strategy)
        mirror = bool(strat and strat.mirror)

        if strat and strat.live_rule and p.trigger_wallet:
            if await _live_rule_step(session, p, px, now, s):
                closed += 1
            continue

        # wallet_mirror: copy each leader sell with the same share of what is left
        if mirror and p.trigger_wallet:
            sells = (await session.execute(
                select(WalletTrade).where(
                    WalletTrade.chain == p.chain, WalletTrade.token_address == p.token_address,
                    WalletTrade.wallet == p.trigger_wallet, WalletTrade.side == "sell",
                    WalletTrade.kind == "sell", WalletTrade.id > (p.leader_trade_id or 0),
                ).order_by(WalletTrade.id)
            )).scalars().all()
            done = False
            for sell in sells:
                p.leader_trade_id = sell.id
                share = 1.0 if (sell.remaining_amount or 0) <= 0 or not sell.pct_sold else min(sell.pct_sold / 100, 1.0)
                at_px = px or sell.price_usd or p.last_price or p.entry_price
                frac = (p.remaining_frac if p.remaining_frac is not None else 1.0)
                sold = frac * share
                p.proceeds_usd = (p.proceeds_usd or 0.0) + p.size_usd * sold / p.fill_price * at_px * (
                    1 - s.paper_slippage_pct / 100)
                p.remaining_frac = frac - sold
                log.info("paper MIRROR sell %s %s: %.0f%% of position @ %.10g", p.chain.value,
                         p.symbol or p.token_address, sold * 100, at_px)
                if p.remaining_frac <= 0.01:
                    _close(p, at_px, "leader_sold", now, s)
                    closed += 1
                    done = True
                    break
            if done:
                continue

        # leader sold (wallet_follow): exit with the wallet
        if p.trigger_wallet and not mirror:
            sold = (await session.execute(
                select(WalletTrade.price_usd).where(
                    WalletTrade.chain == p.chain, WalletTrade.token_address == p.token_address,
                    WalletTrade.wallet == p.trigger_wallet, WalletTrade.side == "sell",
                    WalletTrade.kind == "sell", WalletTrade.detected_at >= entry_time,
                ).order_by(WalletTrade.id).limit(1)
            )).first()
            if sold is not None:
                _close(p, px or sold[0] or p.last_price or p.entry_price, "leader_sold", now, s)
                closed += 1
                continue

        if px is not None and not mirror:
            pct = (px / p.entry_price - 1) * 100
            if pct >= p.tp_pct:
                _close(p, px, "take_profit", now, s)
                closed += 1
                continue
            if pct <= p.sl_pct:
                _close(p, px, "stop_loss", now, s)
                closed += 1
                continue
        if now - entry_time >= timedelta(minutes=p.max_hold_minutes):
            _close(p, p.last_price or p.entry_price, "time", now, s)
            closed += 1
            continue
        last = utc(p.last_priced_at) or entry_time
        if px is None and now - last >= timedelta(minutes=s.paper_no_price_minutes):
            _close(p, p.last_price or p.entry_price, "no_price", now, s)
            closed += 1
    return closed


def _sell_share(p: PaperPosition, share: float, at_px: float, s: Settings) -> None:
    frac = 1.0 if p.remaining_frac is None else p.remaining_frac
    sold = frac * share
    p.proceeds_usd = (p.proceeds_usd or 0.0) + p.size_usd * sold / p.fill_price * at_px * (1 - s.paper_slippage_pct / 100)
    p.remaining_frac = frac - sold


async def _live_rule_step(session: AsyncSession, p: PaperPosition, px: float | None, now: datetime,
                          s: Settings) -> bool:
    """The live engine's exit rule on paper. Returns True when the position closed."""
    sells = (await session.execute(
        select(WalletTrade).where(
            WalletTrade.chain == p.chain, WalletTrade.token_address == p.token_address,
            WalletTrade.wallet == p.trigger_wallet, WalletTrade.side == "sell",
            WalletTrade.kind == "sell", WalletTrade.id > (p.leader_trade_id or 0),
        ).order_by(WalletTrade.id)
    )).scalars().all()
    for sell in sells:
        p.leader_trade_id = sell.id
        at_px = px or sell.price_usd or p.last_price or p.entry_price
        full = (sell.remaining_amount is not None and sell.remaining_amount <= 0) or (sell.pct_sold or 0) >= 99
        if full:
            _close(p, at_px, "leader_sold", now, s)
            return True
        if p.exit_mode == "half":
            continue                                   # capital is out: only the leader's full exit counts
        p.exit_mode = "follow"                         # a trim before our 2x: follow every leader sell
        _sell_share(p, min((sell.pct_sold or 0) / 100, 1.0), at_px, s)
        if (p.remaining_frac or 0) <= 0.01:
            _close(p, at_px, "leader_sold", now, s)
            return True
    if px is not None and p.exit_mode is None and px >= 2 * p.fill_price:
        _sell_share(p, 0.5, px, s)                     # 2x: half out, the rest waits for the leader's full exit
        p.exit_mode = "half"
        log.info("paper %s %s: 50%% at 2x @ %.10g", p.strategy, p.symbol or p.token_address, px)
    if now - utc(p.entry_time) >= timedelta(minutes=p.max_hold_minutes):
        _close(p, p.last_price or p.entry_price, "time", now, s)
        return True
    last = utc(p.last_priced_at) or utc(p.entry_time)
    if px is None and now - last >= timedelta(minutes=s.paper_no_price_minutes):
        _close(p, p.last_price or p.entry_price, "no_price", now, s)
        return True
    return False


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def _row(p: PaperPosition, s: Settings) -> dict[str, Any]:
    d = {
        "id": p.id, "strategy": p.strategy, "chain": p.chain.value, "token_address": p.token_address,
        "symbol": p.symbol, "trigger": p.trigger, "status": p.status, "size_usd": p.size_usd,
        "entry_time": utc(p.entry_time).isoformat() if p.entry_time else None,
        "entry_price": p.entry_price, "last_price": p.last_price,
        "peak_pct": p.peak_pct, "trough_pct": p.trough_pct,
        "exit_time": utc(p.exit_time).isoformat() if p.exit_time else None,
        "exit_price": p.exit_price, "exit_reason": p.exit_reason,
        "pnl_usd": p.pnl_usd, "pnl_pct": p.pnl_pct,
        "remaining_pct": round((1.0 if p.remaining_frac is None else p.remaining_frac) * 100, 1),
    }
    if p.status == "open" and p.last_price and not (p.strategy in SELF_PRICED and p.pnl_usd is not None):
        d["pnl_usd"] = _value(p, p.last_price, s) - p.size_usd
        d["pnl_pct"] = d["pnl_usd"] / p.size_usd * 100
    return d


async def paper_summary(session: AsyncSession, hours: int = 24 * 7, recent_limit: int = 50,
                        settings: Settings | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    since = _now() - timedelta(hours=hours)
    rows = (await session.execute(
        select(PaperPosition).where(or_(PaperPosition.entry_time >= since, PaperPosition.status == "open"))
        .order_by(PaperPosition.entry_time.desc())
    )).scalars().all()

    strategies = []
    for strat in STRATEGIES.values():
        mine = [r for r in rows if r.strategy == strat.name]
        closed = [r for r in mine if r.status == "closed"]
        opened = [r for r in mine if r.status == "open"]
        wins = [r for r in closed if (r.pnl_usd or 0) > 0]
        unreal = sum(_row(r, s)["pnl_usd"] or 0.0 for r in opened)
        reasons: dict[str, int] = defaultdict(int)
        for r in closed:
            reasons[r.exit_reason or "?"] += 1
        realized = sum(r.pnl_usd or 0.0 for r in closed)
        strategies.append({
            "name": strat.name, "title": strat.title, "tp_pct": strat.tp_pct, "sl_pct": strat.sl_pct,
            "mirror": strat.mirror or strat.live_rule,
            "max_hold_minutes": s.paper_mirror_max_hold_hours * 60 if strat.mirror or strat.live_rule else strat.max_hold_minutes,
            "trades": len(mine), "open": len(opened), "closed": len(closed), "wins": len(wins),
            "win_rate": round(len(wins) / len(closed), 3) if closed else None,
            "realized_usd": round(realized, 2), "unrealized_usd": round(unreal, 2),
            "invested_usd": round(sum(r.size_usd for r in closed), 2),
            "return_pct": round(realized / sum(r.size_usd for r in closed) * 100, 2) if closed else None,
            "avg_pnl_pct": round(sum(r.pnl_pct or 0 for r in closed) / len(closed), 2) if closed else None,
            "best_pct": round(max(r.pnl_pct or 0 for r in closed), 2) if closed else None,
            "worst_pct": round(min(r.pnl_pct or 0 for r in closed), 2) if closed else None,
            "exit_reasons": dict(reasons),
        })
    return {
        "hours": hours, "size_usd": s.paper_size_usd, "slippage_pct": s.paper_slippage_pct,
        "generated_at": _now().isoformat(), "strategies": strategies,
        "mirror_wallets": list(s.paper_mirror_wallets),
        "positions": [_row(r, s) for r in rows[:recent_limit]] if recent_limit else [],
    }


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------


def dexscreener_prices(dex) -> PriceFetcher:
    from app.scanner_worker import _f, select_primary_pair

    async def fetch(chain: Chain, addrs: list[str]) -> dict[str, float]:
        pairs = await dex.pairs_for_tokens(chain, addrs)
        out = {}
        for a in addrs:
            pair = select_primary_pair(chain, a, pairs)
            px = _f(pair.get("priceUsd")) if pair else None
            if px:
                out[a] = px
        return out

    return fetch


class PaperEngine:
    def __init__(self, dex, settings: Settings | None = None) -> None:
        self.prices = dexscreener_prices(dex)
        self.s = settings or get_settings()

    async def run_once(self) -> tuple[int, int]:
        from app.db import SessionLocal

        async with SessionLocal() as session:
            closed = await process_exits(session, self.prices, self.s)
            opened = await process_entries(session, self.s)
            await session.commit()
        return opened, closed

    async def run_forever(self) -> None:
        if not self.s.paper_enabled:
            log.info("Paper trading disabled")
            return
        log.info("Paper trading started (every %ss, $%.0f per trade, %.1f%% slippage per fill)",
                 self.s.paper_interval_seconds, self.s.paper_size_usd, self.s.paper_slippage_pct)
        while True:
            t0 = time.monotonic()
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("paper trading step failed")
            await asyncio.sleep(max(self.s.paper_interval_seconds - (time.monotonic() - t0), 5))
