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
W67Q = "67Q1NscZdLxBMNgbjJ4jprm9J5GSUX1c9aVFigBzxFWg"   # auto 18/34: fast trader (4 min median hold)
W5Y9 = "5Y9pyDnwdaJ32kWjeChBNvVRubhe6k4Z8bnioUfM3bNd"   # auto 13/25: +$5.9k in 4 days, 8.5 min median hold
W8ZK = "8zkgFGVZrDLieViwqiXFCydSX6WL5hsxmUu55yBdsNsZ"   # scout trial: +$2.9k on 161 exits in a day, 110 min hold
CLUSTER_MIN_WALLETS = 2
CURSOR_TRADES = "paper:wallet_trades"
CURSOR_SIGNALS = "paper:signals"
AFTER_WALLET_MINUTES = 60


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
    after_wallet: str | None = None   # signal strategies: only when this wallet bought the token in the
                                      # AFTER_WALLET_MINUTES before the signal
    chains: tuple[str, ...] | None = None   # signal strategies: only tokens on these chains
    first_signal: bool = False              # signal strategies: only the token's first signal
    sources_any: tuple[str, ...] | None = None   # signal strategies: only tokens found by one of these feeds
    min_liquidity: float | None = None   # signal strategies: only tokens with at least this much liquidity (USD)
    # signal strategies on Ethereum: skip a token whose quoted buy-and-sell-back (KyberSwap, at the paper size, gas
    # in) loses more than this %; no route either way is a skip too (app/eth_quotes.py)
    max_roundtrip_pct: float | None = None
    costs: bool = False     # also charge what a real trade pays on top of slippage: the chain's gas on each swap
                            # (PAPER_ETH_GAS_USD on Ethereum) and the token's buy / sell tax; skip honeypots


# priced and closed by their own loops (app/sniper.py, app/grad.py), second by second
SELF_PRICED = ("snipe_all", "snipe_momentum", "grad_all", "grad_confirm", "curve_70", "curve_85",
               "curve_70_half", "curve_70_trail", "curve_70_filter", "curve_85_half40", "curve_30_70",
               "curve_30_70_sl", "snipe_fast_tp", "curve_fast19", "curve_fast34", "curve_93_fast",
               "curve_70_live", "curve_fast34_mom", "curve_fast34_mom2", "curve_70_organic",
               "curve_70_organic_slow", "curve_70_organic_grad", "curve_70_organic_v2",
               "curve_70_organic_v2_late_sl", "curve_70_quiet", "curve_70_quiet_wide_sl",
               "curve_70_slow3", "curve_70_organic_2min", "curve_70_organic_be50",
               "curve_70_organic_safe")

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
    # 67Q1Ns test (user's request 2026-11-25): it made ~+$3.7k in a week but holds 4 minutes for +3% median;
    # does a copy keep any of that? Its first buys, copied two ways
    Strategy("w67q_mirror", "Copy 67Q1Ns: buy with it, sell the same share when it sells", tp_pct=0.0, sl_pct=0.0,
             leader=W67Q, mirror=True),
    Strategy("w67q_live", "Copy 67Q1Ns like live (50% at 2x, rest on its full exit)", tp_pct=0.0, sl_pct=0.0,
             leader=W67Q, live_rule=True),
    # 5Y9pyD test (user's request 2026-11-25): 62% of 104 full exits won, +$5.9k in 4 days; wallet_follow copies of it
    # +$937 on 61. Its first buys, copied the same two ways as 67Q1Ns
    Strategy("w5y9_mirror", "Copy 5Y9pyD: buy with it, sell the same share when it sells", tp_pct=0.0, sl_pct=0.0,
             leader=W5Y9, mirror=True),
    Strategy("w5y9_live", "Copy 5Y9pyD like live (50% at 2x, rest on its full exit)", tp_pct=0.0, sl_pct=0.0,
             leader=W5Y9, live_rule=True),
    # 8zkgFG test (user's request 2026-11-26): +$2.9k on 161 full exits in its first day tracked, 50% won, holds
    # ~110 min (slow enough to follow). wallet_follow and scout_trial are paused, so its own two copies
    Strategy("w8zk_mirror", "Copy 8zkgFG: buy with it, sell the same share when it sells", tp_pct=0.0, sl_pct=0.0,
             leader=W8ZK, mirror=True),
    Strategy("w8zk_live", "Copy 8zkgFG like live (50% at 2x, rest on its full exit)", tp_pct=0.0, sl_pct=0.0,
             leader=W8ZK, live_rule=True),
    # wallet scout trials (app/wallet_scout.py): each candidate's first buys, copied like live
    Strategy("scout_trial", "Wallet scout: paper trial of a candidate wallet (live exit rule)", tp_pct=0.0,
             sl_pct=0.0, live_rule=True),
    Strategy("signal_80", "Scanner signal, score >= 80", min_score=80),
    Strategy("signal_85", "Scanner signal, score >= 85", min_score=85),
    # (2026-11-25, 704 coins' first signals): when DULX3e bought in the hour before, 50% reached +100% (28% for
    # all signals), but 96% also fell 30%; two exits to see which comes first
    # (2026-11-25, 1,682 signal_80 trades 11-19..11-23: -5.2% a trade, every day; score didn't help): the two simple
    # filters that held in both halves of the data. Same exits as signal_80 to compare
    # costs=True (user's request 2026-11-25, before trading it for real): gas on both swaps and the token's taxes
    Strategy("signal_eth", "Scanner signal, score >= 80, Ethereum only (with gas and token tax)", min_score=80,
             chains=("ethereum",), costs=True),
    # (user's request 2026-11-26) 126 Ethereum signal_80 trades: coins with $100k+ liquidity +13.8% avg (55), under
    # $20k +0.7% (35); the profit leans on one coin (mihao, $706 of ~$1,070). Same rule, only the deeper pools
    # (user's request 2026-11-26) signal_eth's paper profit leaned on mihao, whose real round trip loses ~78%:
    # the same rule, only coins a real quote says can be sold back (round trip under 5%)
    Strategy("signal_eth_rt", "Scanner signal, score >= 80, Ethereum only, round trip under 5% on a real quote "
             "(with gas and tax)", min_score=80, chains=("ethereum",), costs=True, max_roundtrip_pct=5.0),
    Strategy("signal_eth_liq100", "Scanner signal, score >= 80, Ethereum only, liquidity $100k+ (with gas and tax)",
             min_score=80, chains=("ethereum",), costs=True, min_liquidity=100_000.0),
    Strategy("signal_gecko_first", "Scanner signal, score >= 80: the token's first signal, found by GeckoTerminal "
             "(new or trending pools)", min_score=80, first_signal=True, sources_any=("gecko_new", "gecko_trending")),
    Strategy("signal_dulx", "Scanner signal after DULX3e bought it (within 1 h): TP +100%, stop -40%, 6 h",
             tp_pct=100.0, sl_pct=-40.0, min_score=0, after_wallet=DULX),
    Strategy("signal_dulx_tight", "Scanner signal after DULX3e bought it (within 1 h): TP +50%, stop -25%, 6 h",
             tp_pct=50.0, sl_pct=-25.0, min_score=0, after_wallet=DULX),
    # run by app/sniper.py (own live feed and exits); listed here so /paper shows them
    Strategy("snipe_all", "Sniper: sample of every new pump.fun token", tp_pct=50.0, sl_pct=-30.0, max_hold_minutes=5),
    Strategy("snipe_fast_tp", "Sniper: same tokens as snipe_all, out at +25% or 15% off the high", tp_pct=25.0,
             sl_pct=-30.0, max_hold_minutes=5),
    Strategy("grad_all", "Graduation: every pump.fun token just moved to PumpSwap", tp_pct=50.0, sl_pct=-20.0,
             max_hold_minutes=30),
    Strategy("grad_confirm", "Graduation: just moved to PumpSwap and still rising a minute later", tp_pct=50.0,
             sl_pct=-20.0, max_hold_minutes=30),
    Strategy("snipe_momentum", "Sniper: new pump.fun token with early buying", tp_pct=50.0, sl_pct=-30.0,
             max_hold_minutes=5),
    # run by app/curve.py: bought late on the bonding curve, sold when it graduates
    Strategy("curve_70", "Bonding curve: buy at 70% sold, sell at graduation", tp_pct=0.0, sl_pct=-25.0,
             max_hold_minutes=60),
    Strategy("curve_85", "Bonding curve: buy at 85% sold, sell at graduation", tp_pct=0.0, sl_pct=-25.0,
             max_hold_minutes=60),
    # user's rule (2026-11-23): $10, no stop loss, half at 2x then a stop at the entry; two ways to exit the rest
    Strategy("curve_70_half", "Bonding curve 70%: $10, no stop, half at 2x then stop at entry, rest at graduation",
             tp_pct=100.0, sl_pct=0.0, max_hold_minutes=60),
    Strategy("curve_70_trail", "Bonding curve 70%: $10, no stop, half at 2x then stop at entry, rest rides "
             "after graduation (trailing stop)", tp_pct=100.0, sl_pct=0.0, max_hold_minutes=120),
    # from the first curve trades (2026-11-23): skip buys that fell 10%+ before the fill; take half early
    Strategy("curve_70_filter", "Bonding curve 70%: $10, skip if it fell 10% before our buy, half at +20% then "
             "stop at entry, rest at graduation", tp_pct=20.0, sl_pct=0.0, max_hold_minutes=60),
    Strategy("curve_85_half40", "Bonding curve 85%: $10, half at +40% then stop at entry, rest at graduation",
             tp_pct=40.0, sl_pct=0.0, max_hold_minutes=60),
    # user's idea (2026-11-23): in at 30% sold, out at 70% (~2.6x if it gets there)
    Strategy("curve_30_70", "Bonding curve: $10, buy at 30% sold, sell all at 70% (no stop, 1 h max)",
             tp_pct=160.0, sl_pct=0.0, max_hold_minutes=60),
    Strategy("curve_30_70_sl", "Bonding curve: $10, buy at 30% sold, sell all at 70%, stop at -30% (1 h max)",
             tp_pct=160.0, sl_pct=-30.0, max_hold_minutes=60),
    # from the research (arXiv 2610.39458, 2602.14860): fast early curves, sold just before graduation
    Strategy("curve_fast19", "Bonding curve: $10, 5 SOL in within 30 s (19% sold), sell at 97% sold, stop -30%",
             tp_pct=0.0, sl_pct=-30.0, max_hold_minutes=15),
    Strategy("curve_fast34", "Bonding curve: $10, 10 SOL in within 60 s (34% sold), sell at 97% sold, stop -25%",
             tp_pct=0.0, sl_pct=-25.0, max_hold_minutes=15),
    Strategy("curve_93_fast", "Bonding curve: $10, 93% sold within 4 min, sell at 99% (before graduation), stop -20%",
             tp_pct=0.0, sl_pct=-20.0, max_hold_minutes=10),
    Strategy("curve_fast34_mom2", "Bonding curve: curve_fast34_mom + bought in the minute before the signal, no "
             "bundler launch tools", tp_pct=0.0, sl_pct=-25.0, max_hold_minutes=15),
    Strategy("curve_70_organic", "Bonding curve: 70% with organic trading (10+ buys, 5+ sells a minute, dev under 25%) "
             "within 5 min; rides graduation, sells 30% under the high", tp_pct=0.0, sl_pct=-25.0,
             max_hold_minutes=60),
    Strategy("curve_70_organic_v2", "Bonding curve: curve_70_organic without chasing (fill under +3% of the "
             "decision) and under 80 buys a minute; rides graduation, 30% trailing", tp_pct=0.0, sl_pct=-25.0,
             max_hold_minutes=60),
    Strategy("curve_70_organic_v2_late_sl", "Bonding curve: curve_70_organic_v2 with a later stop (-40% in the first "
             "minute, then -25%)", tp_pct=0.0, sl_pct=-25.0, max_hold_minutes=60),
    Strategy("curve_70_organic_grad", "Bonding curve: curve_70_organic sold at 99% sold, just before graduation "
             "(no trailing after it), stop -25%", tp_pct=0.0, sl_pct=-25.0, max_hold_minutes=60),
    Strategy("curve_70_organic_slow", "Bonding curve: curve_70_organic for coins that took 15+ min to reach 70% "
             "(organic trading, dev under 25%); rides graduation, sells 30% under the high", tp_pct=0.0, sl_pct=-25.0,
             max_hold_minutes=60),
    Strategy("curve_fast34_mom", "Bonding curve: curve_fast34 only if still bought after the signal (up 5+ points, "
             "40%+ sold at the fill), the dev holds under 25% and no copycat name", tp_pct=0.0, sl_pct=-25.0, max_hold_minutes=15),
    # (2026-11-24, 48 h of trades checked on-chain): 70 % reached quietly graduates more; paper only for now
    Strategy("curve_70_quiet", "Bonding curve: 70% reached quietly (under 2 SOL net in the minute before, 3+ buyers "
             "if known), sell at 99% sold, stop -25%", tp_pct=0.0, sl_pct=-25.0, max_hold_minutes=60),
    Strategy("curve_70_quiet_wide_sl", "Bonding curve: curve_70_quiet with a wider stop (-45%)", tp_pct=0.0,
             sl_pct=-45.0, max_hold_minutes=60),
    Strategy("curve_70_organic_2min", "Bonding curve: curve_70_organic, only coins that took 2-5 min to reach 70%",
             tp_pct=0.0, sl_pct=-25.0, max_hold_minutes=60),
    Strategy("curve_70_organic_safe", "Bonding curve: curve_70_organic, only when the launch snipers have sold out "
             "and no holder has 12%+", tp_pct=0.0, sl_pct=-25.0, max_hold_minutes=60),
    Strategy("curve_70_organic_be50", "Bonding curve: curve_70_organic, stop moved to the entry once it is +50%",
             tp_pct=0.0, sl_pct=-25.0, max_hold_minutes=60),
    Strategy("curve_70_slow3", "Bonding curve: 70% reached 3+ min after launch; rides graduation, sells 30% under "
             "the high, stop -25%", tp_pct=0.0, sl_pct=-25.0, max_hold_minutes=60),
    # user's choice (2026-11-23): curve_70 traded with real money ($10); sells just before graduation
    Strategy("curve_70_live", "Bonding curve: $10, buy at 70% sold, sell at 99% (before graduation), stop -25% "
             "(curve_70 for real)", tp_pct=0.0, sl_pct=-25.0, max_hold_minutes=60),
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
          leader_trade_id: int | None = None, costs: dict[str, float] | None = None) -> PaperPosition | None:
    from app import paper_switches

    key = (strategy.name, chain.value, token)
    if key in keys or not price or price <= 0 or paper_switches.paused(strategy.name):
        return None
    keys.add(key)
    fill = price * (1 + s.paper_slippage_pct / 100)
    if costs:
        # the buy's gas comes out of the stake and its tax out of the tokens: both raise the price paid per token
        fill = fill / (1 - costs["buy_tax"] / 100) * s.paper_size_usd / (s.paper_size_usd - costs["gas_usd"])
    pos = PaperPosition(
        strategy=strategy.name, chain=chain, token_address=token, symbol=symbol or "", trigger=trigger[:160],
        trigger_wallet=wallet, status="open", size_usd=s.paper_size_usd, entry_time=at, entry_price=price,
        fill_price=fill, tp_pct=strategy.tp_pct, sl_pct=strategy.sl_pct,
        max_hold_minutes=(s.paper_mirror_max_hold_hours * 60 if strategy.mirror or strategy.live_rule
                          else strategy.max_hold_minutes),
        last_price=price, last_priced_at=at, peak_pct=0.0, trough_pct=0.0,
        remaining_frac=1.0, proceeds_usd=0.0, leader_trade_id=leader_trade_id,
        features={"costs": costs} if costs else None,
    )
    session.add(pos)
    log.info("paper OPEN %s %s %s @ %.10g (%s)", strategy.name, chain.value, symbol or token, price, trigger)
    return pos


async def process_entries(session: AsyncSession, s: Settings | None = None,
                          prices: PriceFetcher | None = None) -> int:
    """`prices`: the market price when this runs (a few seconds after the trade, like the live bot), so a wallet
    buy is entered where we could really buy, not at the wallet's own price."""
    from app import paper_switches

    s = s or get_settings()
    opened = 0
    await paper_switches.refresh(session)
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
        now_px: dict[tuple[Chain, str], float] = {}
        if prices is not None:
            by_chain: dict[Chain, set[str]] = defaultdict(set)
            for t in trades:
                if _is_buy(t) and t.price_usd:
                    by_chain[t.chain].add(t.token_address)
            for chain, addrs in by_chain.items():
                lst = sorted(addrs)
                for i in range(0, len(lst), 30):
                    try:
                        got = await prices(chain, lst[i:i + 30])
                    except Exception:
                        log.warning("paper: entry prices failed", exc_info=True)
                        got = {}
                    now_px.update({(chain, a): p for a, p in got.items() if p and p > 0})
        try:
            from app.wallet_scout import trial_wallets
            trials = await trial_wallets(session) if trades else set()
        except Exception:
            trials = set()
        for t in trades:
            if not _is_buy(t) or not t.price_usd:
                continue
            # where we could buy now (never better than the wallet itself)
            entry_px = max(now_px.get((t.chain, t.token_address)) or t.price_usd, t.price_usd)
            label = t.wallet_label or t.wallet[:6]
            usd = f" ${t.usd_value:,.0f}" if t.usd_value else ""
            if _open(session, keys, STRATEGIES["wallet_follow"], chain=t.chain, token=t.token_address,
                     symbol=t.symbol, price=entry_px, trigger=f"{label} bought{usd}", at=now, s=s,
                     wallet=t.wallet, leader_trade_id=t.id):
                opened += 1
            if (not mirror_set or t.wallet in mirror_set) and _open(
                    session, keys, STRATEGIES["wallet_mirror"], chain=t.chain, token=t.token_address,
                    symbol=t.symbol, price=entry_px, trigger=f"copy {label}{usd}", at=now, s=s,
                    wallet=t.wallet, leader_trade_id=t.id):
                opened += 1
            if t.wallet in trials and t.first_buy and _open(
                    session, keys, STRATEGIES["scout_trial"], chain=t.chain, token=t.token_address, symbol=t.symbol,
                    price=entry_px, trigger=f"scout trial {t.wallet[:6]}{usd}", at=now, s=s, wallet=t.wallet,
                    leader_trade_id=t.id):
                opened += 1
            for strat in STRATEGIES.values():          # one leader's first buys, maybe sized
                if strat.leader != t.wallet or not t.first_buy:
                    continue
                if strat.min_usd is not None and (t.usd_value or 0) < strat.min_usd:
                    continue
                if strat.max_usd is not None and (t.usd_value or 0) > strat.max_usd:
                    continue
                if _open(session, keys, strat, chain=t.chain, token=t.token_address, symbol=t.symbol,
                         price=entry_px, trigger=f"copy {label}{usd}", at=now, s=s, wallet=t.wallet,
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
                         symbol=t.symbol, price=entry_px, trigger=f"{len(buyers)} wallets: {names}", at=now, s=s):
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
            bought_by: dict[str, bool] = {}
            for strat in STRATEGIES.values():
                if strat.min_score is None or sig.score < strat.min_score:
                    continue
                trigger = f"score {sig.score}"
                chain = tok.chain.value if isinstance(tok.chain, Chain) else str(tok.chain)
                if strat.chains and chain not in strat.chains:
                    continue
                if strat.min_liquidity is not None and (tok.liquidity_usd or 0.0) < strat.min_liquidity:
                    continue
                if strat.sources_any and not set(strat.sources_any) & set(tok.discovery_sources or []):
                    continue
                if strat.first_signal:
                    earlier = (await session.execute(select(Signal.id).where(
                        Signal.token_id == sig.token_id, Signal.id < sig.id).limit(1))).first()
                    if earlier is not None:
                        continue
                    trigger += " first signal"
                if strat.after_wallet:
                    w = strat.after_wallet
                    if w not in bought_by:
                        at = utc(sig.created_at)
                        bought_by[w] = (await session.execute(select(WalletTrade.id).where(
                            WalletTrade.wallet == w, WalletTrade.chain == tok.chain,
                            WalletTrade.token_address == tok.address, WalletTrade.side == "buy",
                            WalletTrade.block_time >= at - timedelta(minutes=AFTER_WALLET_MINUTES),
                            WalletTrade.block_time <= at).limit(1))).first() is not None
                    if not bought_by[w]:
                        continue
                    trigger += f" after {w[:6]} bought"
                costs = None
                if strat.costs:
                    if tok.is_honeypot:
                        continue
                    costs = trade_costs(chain, tok.buy_tax, tok.sell_tax, s)
                if strat.max_roundtrip_pct is not None:
                    from app import eth_quotes

                    if not s.paper_eth_quotes:
                        continue
                    loss = await eth_quotes.roundtrip_loss_pct(tok.address, s.paper_size_usd)
                    if loss is None or loss > strat.max_roundtrip_pct:
                        log.info("paper %s: skip %s (round trip %s%%)", strat.name, tok.symbol, loss)
                        continue
                    trigger += f" rt {loss:.1f}%"
                if _open(session, keys, strat, chain=tok.chain, token=tok.address, symbol=tok.symbol,
                         price=sig.price_usd or 0.0, trigger=trigger, at=now, s=s, costs=costs):
                    opened += 1
        await _set_cursor(session, CURSOR_SIGNALS, max_sig)
    return opened


# ---------------------------------------------------------------------------
# Exits
# ---------------------------------------------------------------------------


def trade_costs(chain: str, buy_tax: float | None, sell_tax: float | None, s: Settings) -> dict[str, float]:
    """What a real trade pays besides slippage (strategy costs=True): gas per swap and the token's taxes (%)."""
    gas = s.paper_eth_gas_usd if chain == "ethereum" else 0.0
    return {"gas_usd": gas, "buy_tax": min(max(buy_tax or 0.0, 0.0), 99.0),
            "sell_tax": min(max(sell_tax or 0.0, 0.0), 100.0)}


def _value(pos: PaperPosition, price: float, s: Settings) -> float:
    """Proceeds already banked plus what the remaining share would fetch at `price` (after slippage, and for a
    costs=True position after the sell tax and the sell's gas)."""
    frac = 1.0 if pos.remaining_frac is None else pos.remaining_frac
    rest = pos.size_usd * frac / pos.fill_price * price * (1 - s.paper_slippage_pct / 100)
    costs = (pos.features or {}).get("costs") if isinstance(pos.features, dict) else None
    if costs and frac > 0:
        rest = rest * (1 - costs.get("sell_tax", 0.0) / 100) - costs.get("gas_usd", 0.0)
    return (pos.proceeds_usd or 0.0) + rest


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


# A price more than JUMP times (or under 1/JUMP of) the last one is a bad read until it repeats: a wrong pair once
# priced hSOL (~$81) at $396,965 and booked +$470k of paper profit (2026-11-23). Repeating is not enough on its
# own: one source priced ZEC (~$800, $2M liquidity) at $8.2M three reads in a row and booked +$1.88M across
# wallet_follow and wallet_cluster (2026-11-23). So a repeated jump must also be confirmed by every other price
# source (Jupiter and DexScreener) when there are two.
JUMP = 20.0
JUMP_CONFIRM = 3
AGREE = 2.0          # sources agree when within this factor of each other
_suspect: dict[int, tuple[float, int]] = {}

# (chain, [addresses]) -> {address: [price from each source that has one]}
PriceSources = Callable[[Chain, list[str]], Awaitable[dict[str, list[float]]]]


def _jump(ref: float | None, px: float) -> bool:
    return bool(ref) and not 1 / JUMP <= px / ref <= JUMP


def _sane(p: PaperPosition, px: float) -> bool | None:
    """True: a normal price, or a jump that has repeated JUMP_CONFIRM readings in a row (still to be checked
    against the other sources: None means "repeated, confirm it"). False: a jump not yet repeated."""
    ref = p.last_price or p.entry_price
    if not _jump(ref, px):
        _suspect.pop(p.id, None)
        return True
    prev = _suspect.get(p.id)
    n = prev[1] + 1 if prev and 0.8 <= px / prev[0] <= 1.25 else 1
    if n >= JUMP_CONFIRM:
        _suspect.pop(p.id, None)
        return None
    _suspect[p.id] = (px, n)
    log.warning("paper: %s price %.6g is %.0fx its last %.6g: ignored until it repeats (%d/%d)",
                p.symbol or p.token_address[:8], px, px / ref, ref, n, JUMP_CONFIRM)
    return False


def _agreed(px: float, others: list[float]) -> bool:
    """A repeated jump is real when at least two sources have a price and all of them are within AGREE of it."""
    return len(others) >= 2 and all(1 / AGREE <= o / px <= AGREE for o in others)


async def process_exits(session: AsyncSession, prices: PriceFetcher, s: Settings | None = None,
                        verify: PriceSources | None = None) -> int:
    """`verify`: every price source for a token, asked only when a huge jump has repeated; without it (tests, or
    no second source) a repeated jump is accepted as before."""
    s = s or get_settings()
    open_pos = (await session.execute(select(PaperPosition).where(
        PaperPosition.status == "open", PaperPosition.strategy.notin_(SELF_PRICED),
        PaperPosition.strategy.notlike("cc%")))).scalars().all()        # cc<id>: the owner's curve strategies
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

    sane: dict[int, bool | None] = {}
    for p in open_pos:
        px = live.get((p.chain, p.token_address))
        if px is not None:
            sane[p.id] = _sane(p, px)
    confirm = [p for p in open_pos if sane.get(p.id) is None and p.id in sane]
    if confirm and verify is not None:
        checked: dict[tuple[Chain, str], list[float]] = {}
        for chain in {p.chain for p in confirm}:
            addrs = sorted({p.token_address for p in confirm if p.chain == chain})
            try:
                got = await verify(chain, addrs)
            except Exception:
                log.warning("paper: price check failed for %s", chain.value, exc_info=True)
                got = {}
            for a in addrs:
                checked[(chain, a)] = [x for x in got.get(a, []) if x and x > 0]
        for p in confirm:
            px = live[(p.chain, p.token_address)]
            others = checked.get((p.chain, p.token_address), [])
            ok = _agreed(px, others)
            sane[p.id] = ok
            if not ok:
                log.warning("paper: %s jump to %.6g not confirmed by the other sources %s: ignored",
                            p.symbol or p.token_address[:8], px, [round(o, 10) for o in others])

    closed = 0
    for p in open_pos:
        entry_time = utc(p.entry_time)
        px = live.get((p.chain, p.token_address))
        if px is not None and sane.get(p.id) is False:
            px = None
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
            after = (WalletTrade.id > p.leader_trade_id) if p.leader_trade_id else (WalletTrade.detected_at >= entry_time)
            sold = (await session.execute(
                select(WalletTrade.price_usd, WalletTrade.detected_at).where(
                    WalletTrade.chain == p.chain, WalletTrade.token_address == p.token_address,
                    WalletTrade.wallet == p.trigger_wallet, WalletTrade.side == "sell",
                    WalletTrade.kind == "sell", after,
                ).order_by(WalletTrade.id).limit(1)
            )).first()
            if sold is not None:
                if utc(sold[1]) and utc(sold[1]) <= entry_time:
                    at_px = p.entry_price       # it sold before we were even in: in and straight out (fees only)
                else:
                    ref = p.last_price or p.entry_price
                    trade_px = sold[0] if sold[0] and not _jump(ref, sold[0]) else None   # its recorded price, if sane
                    at_px = px or trade_px or ref
                _close(p, at_px, "leader_sold", now, s)
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
        if utc(sell.detected_at) and utc(sell.detected_at) <= utc(p.entry_time):
            at_px = p.entry_price               # it sold before we were in: that share goes out at our entry
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
    self_priced = p.strategy in SELF_PRICED or p.strategy.startswith("cc")
    if p.status == "open" and p.last_price and not (self_priced and p.pnl_usd is not None):
        d["pnl_usd"] = _value(p, p.last_price, s) - p.size_usd
        d["pnl_pct"] = d["pnl_usd"] / p.size_usd * 100
    return d


async def custom_strategies(session: AsyncSession) -> list[Any]:
    """The owner's own curve strategies (made on /paper), oldest first."""
    from app.models import CurveCustom

    return list((await session.execute(select(CurveCustom).order_by(CurveCustom.id))).scalars().all())


def _custom_strategy(row: Any) -> Strategy:
    from app.curve import _shown_tp, custom_rule

    rule = custom_rule(row)
    return Strategy(row.name, rule.title, tp_pct=_shown_tp(rule, 0.0), sl_pct=float(row.sl_pct or 0.0),
                    max_hold_minutes=int(row.max_hold_min or 60))


RESET_KEY = "paper_reset:"     # tracker_cursors key + strategy -> ISO time the owner reset its numbers
# exits that are not the strategy's: the bot restarted (open trades are closed at their last price) or the feed was
# down too long. They stay in the numbers at that price: leaving them out biased long holds upward, since winners
# close fast at their target and the slow losers are the ones still open at a restart (2026-11-24: curve_30_70 went
# from -18% to +53% without its 178 interrupted trades). Instead a strategy with many of them is flagged.
INTERRUPTED = ("restart", "feed_lost")
UNRELIABLE_INTERRUPTED = 0.10    # more than this share of closed trades interrupted: its numbers are not the rule's


async def paper_resets(session: AsyncSession) -> dict[str, datetime]:
    rows = (await session.execute(select(TrackerCursor).where(TrackerCursor.key.like(RESET_KEY + "%")))).scalars().all()
    out = {}
    for r in rows:
        try:
            out[r.key[len(RESET_KEY):]] = datetime.fromisoformat(r.value)
        except ValueError:
            continue
    return out


async def reset_strategy(session: AsyncSession, strategy: str) -> datetime:
    """Start a strategy's numbers from zero: /paper counts only trades opened from now on. Nothing is deleted."""
    if strategy not in STRATEGIES and strategy not in {c.name for c in await custom_strategies(session)}:
        raise KeyError(strategy)
    now = _now()
    await _set_cursor_str(session, RESET_KEY + strategy, now.isoformat())
    await session.commit()
    return now


async def _set_cursor_str(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(TrackerCursor, key)
    if row is None:
        session.add(TrackerCursor(key=key, value=value))
    else:
        row.value = value


async def paper_summary(session: AsyncSession, hours: int = 24 * 7, recent_limit: int = 50,
                        settings: Settings | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    since = _now() - timedelta(hours=hours)
    rows = (await session.execute(
        select(PaperPosition).where(or_(PaperPosition.entry_time >= since, PaperPosition.status == "open"))
        .order_by(PaperPosition.entry_time.desc())
    )).scalars().all()
    from app import paper_switches

    resets = await paper_resets(session)
    paused, hidden = await paper_switches.read(session)
    customs = await custom_strategies(session)
    custom_rows = {c.name: c for c in customs}
    rows = [r for r in rows if r.strategy not in resets or (utc(r.entry_time) or since) >= resets[r.strategy]]

    strategies = []
    for strat in [*STRATEGIES.values(), *(_custom_strategy(c) for c in customs)]:
        mine = [r for r in rows if r.strategy == strat.name]
        closed = [r for r in mine if r.status == "closed"]
        interrupted = [r for r in closed if r.exit_reason in INTERRUPTED]
        opened = [r for r in mine if r.status == "open"]
        wins = [r for r in closed if (r.pnl_usd or 0) > 0]
        unreal = sum(_row(r, s)["pnl_usd"] or 0.0 for r in opened)
        reasons: dict[str, int] = defaultdict(int)
        for r in closed:
            reasons[r.exit_reason or "?"] += 1
        share = len(interrupted) / len(closed) if closed else 0.0
        realized = sum(r.pnl_usd or 0.0 for r in closed)
        strategies.append({
            "name": strat.name, "title": strat.title, "tp_pct": strat.tp_pct, "sl_pct": strat.sl_pct,
            "mirror": strat.mirror or strat.live_rule,
            "max_hold_minutes": s.paper_mirror_max_hold_hours * 60 if strat.mirror or strat.live_rule else strat.max_hold_minutes,
            "trades": len(mine), "open": len(opened), "closed": len(closed), "wins": len(wins),
            "interrupted": len(interrupted), "interrupted_pct": round(share * 100, 1),
            "unreliable": share > UNRELIABLE_INTERRUPTED,
            "win_rate": round(len(wins) / len(closed), 3) if closed else None,
            "realized_usd": round(realized, 2), "unrealized_usd": round(unreal, 2),
            "invested_usd": round(sum(r.size_usd for r in closed), 2),
            "return_pct": round(realized / sum(r.size_usd for r in closed) * 100, 2) if closed else None,
            "avg_pnl_pct": round(sum(r.pnl_pct or 0 for r in closed) / len(closed), 2) if closed else None,
            "best_pct": round(max(r.pnl_pct or 0 for r in closed), 2) if closed else None,
            "worst_pct": round(min(r.pnl_pct or 0 for r in closed), 2) if closed else None,
            "exit_reasons": dict(reasons),
            "reset_at": resets[strat.name].isoformat() if strat.name in resets else None,
            "paused": strat.name in paused, "hidden": strat.name in hidden,
            "custom": {"id": custom_rows[strat.name].id, "enabled": custom_rows[strat.name].enabled}
                      if strat.name in custom_rows else None,
        })
    return {
        "hours": hours, "size_usd": s.paper_size_usd, "slippage_pct": s.paper_slippage_pct,
        "generated_at": _now().isoformat(), "strategies": strategies,
        "mirror_wallets": list(s.paper_mirror_wallets),
        # every open trade (the Open tab must show them all), then the newest closed ones
        "positions": ([_row(r, s) for r in rows if r.status == "open"]
                      + [_row(r, s) for r in rows if r.status != "open"][:recent_limit]) if recent_limit else [],
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


def jupiter_prices() -> PriceFetcher:
    """Solana only: Jupiter's price (fresh; through the live bot's shared Jupiter gate)."""
    import httpx

    from app.live import JUPITER

    http = httpx.AsyncClient(timeout=10)

    async def fetch(chain: Chain, addrs: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        if chain == Chain.solana and addrs:
            try:
                s = get_settings()
                r = await JUPITER.request(http, "GET", f"{s.jupiter_api_url}/price/v3", background=True,
                                          params={"ids": ",".join(addrs[:50])})
                if r is not None and r.status_code == 200:
                    for a, d in (r.json() or {}).items():
                        if d and d.get("usdPrice"):
                            out[a] = float(d["usdPrice"])
            except Exception:
                log.debug("paper: jupiter prices failed", exc_info=True)
        return out

    return fetch


def jupiter_first_prices(dex) -> PriceFetcher:
    """Solana: Jupiter's price, DexScreener for the rest."""
    fast, slow = jupiter_prices(), dexscreener_prices(dex)

    async def fetch(chain: Chain, addrs: list[str]) -> dict[str, float]:
        out = await fast(chain, addrs)
        missing = [a for a in addrs if a not in out]
        if missing:
            out.update(await slow(chain, missing))
        return out

    return fetch


def all_sources(dex) -> PriceSources:
    """Every source's price for each token (Jupiter on Solana, DexScreener everywhere): to confirm a huge jump."""
    sources = (jupiter_prices(), dexscreener_prices(dex))

    async def fetch(chain: Chain, addrs: list[str]) -> dict[str, list[float]]:
        out: dict[str, list[float]] = defaultdict(list)
        for src in sources:
            try:
                got = await src(chain, addrs)
            except Exception:
                got = {}
            for a, px in got.items():
                out[a].append(px)
        return dict(out)

    return fetch


class PaperEngine:
    def __init__(self, dex, settings: Settings | None = None) -> None:
        self.s = settings or get_settings()
        self.prices = jupiter_first_prices(dex) if self.s.paper_live_prices else dexscreener_prices(dex)
        self.verify = all_sources(dex)
        self._exits_at = self._full_at = -1e9

    async def run_once(self) -> tuple[int, int]:
        from app.db import SessionLocal

        async with SessionLocal() as session:
            closed = await process_exits(session, self.prices, self.s, self.verify)
            opened = await process_entries(session, self.s, self.prices if self.s.paper_live_prices else None)
            await session.commit()
        return opened, closed

    async def _tick(self) -> None:
        """Entries every PAPER_ENTRY_SECONDS, exits every PAPER_EXIT_SECONDS (with fresh prices)."""
        from app.db import SessionLocal

        now = time.monotonic()
        async with SessionLocal() as session:
            if now - self._exits_at >= self.s.paper_exit_seconds:
                self._exits_at = now
                await process_exits(session, self.prices, self.s, self.verify)
            await process_entries(session, self.s, self.prices)
            await session.commit()

    async def run_forever(self) -> None:
        if not self.s.paper_enabled:
            log.info("Paper trading disabled")
            return
        log.info("Paper trading started (every %ss, $%.0f per trade, %.1f%% slippage per fill)",
                 self.s.paper_interval_seconds, self.s.paper_size_usd, self.s.paper_slippage_pct)
        while True:
            t0 = time.monotonic()
            try:
                if self.s.paper_live_prices:
                    await self._tick()
                else:
                    await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("paper trading step failed")
            gap = self.s.paper_entry_seconds if self.s.paper_live_prices else self.s.paper_interval_seconds
            await asyncio.sleep(max(gap - (time.monotonic() - t0), 1 if self.s.paper_live_prices else 5))
