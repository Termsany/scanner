"""Paper "pre-graduation" trades: pump.fun tokens bought late on the bonding curve, before they graduate.
Simulated only (no keys, no transactions, no money).

Question it answers: if we bought a pump.fun token once its bonding curve is CURVE_xx % sold and held it until
it graduates to PumpSwap, would it make money after fees, slippage and our delay? On a classic curve the price
at graduation is ~3.4x the price at 70 % and ~2x the price at 85 %, so the bet is "does it finish the curve or
turn around first".

Feeds (CURVE_FEED):
  helius      PumpPortal `subscribeNewToken` for new tokens; every curve watched on Helius (accountSubscribe):
              its reserves give the price (vSol / vTokens), the progress (share of the 793.1M sellable tokens
              sold) and the `complete` flag at graduation.
  hybrid      as helius, and once a token passes CURVE_PP_FROM_PCT it is also streamed from PumpPortal's
              trade feed (PUMPPORTAL_API_KEY): every trade within ~1 s, billed per event, so only the few
              tokens that get that far are paid for.
  pumpportal  no Helius: every new token streamed from PumpPortal until it stalls; graduation from
              `subscribeMigration`. Simplest, and the most PumpPortal events.
  auto        (default) hybrid with a Helius URL and a PumpPortal key, helius with only Helius, pumpportal
              with only the key.
Tokens that stall are dropped early (CURVE_PRUNE_*), so few stay watched.

Strategies (side by side on /paper):
  curve_70   first time progress reaches 70 %, bought CURVE_LATENCY_S later
  curve_85   first time progress reaches 85 %, bought CURVE_LATENCY_S later
Exits: graduation (the curve's `complete` flag) at the final curve price, paying CURVE_GRAD_EXIT_EXTRA_PCT
more slippage because the real sale would be in the new PumpSwap pool; stop loss CURVE_SL_PCT; optional
take profit CURVE_TP_PCT (0 = off); anything still open after CURVE_MAX_HOLD_S is sold. Fee + slippage on
every fill.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import itertools
from collections import deque
import json
import logging
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from app import curve_live
from app.config import Settings, get_settings
from app.models import Chain, PaperPosition

log = logging.getLogger("curve")

FEED_URL = "wss://pumpportal.fun/api/data"
REAL_TOKENS_START = 793_100_000.0          # sellable tokens on a fresh classic pump.fun curve (6 decimals)
V_TOKENS_START = 1_073_000_000.0          # virtual tokens at launch: virtual - real stays 279.9M
@dataclass(frozen=True)
class CurveRule:
    level: float        # buy the first time the curve is this % sold
    mode: str           # grad: all out at graduation (stop / time) | half: $10, no stop, 50% at +half_at, stop at
                        # entry, rest at graduation | trail: like half, the rest rides after graduation (trailing stop)
    half_at: float = 100.0          # half / trail: sell half once the price is up this % (100 = 2x)
    skip_drop: float | None = None  # don't buy if the price fell more than this % between our decision and the fill
    exit_at: float | None = None    # range / custom: sell everything once the curve is this % sold
    sl: float | None = None         # range / custom: stop loss, % from our entry (None = none)
    tp: float | None = None         # custom: sell everything once the price is up this %
    half: float | None = None       # custom: sell half at +this %, then a stop at the entry
    size: float | None = None       # custom: USD per trade
    max_hold_s: float | None = None  # custom: sell whatever is left after this long
    enabled: bool = True            # custom: off = no new buys (open ones are still managed)
    title: str = ""
    max_secs: float | None = None   # skip a token that took longer than this (from its launch) to reach the level
    skip_backslide: bool = False    # skip if the curve slid back below the decision level before our buy
    grad_trail: float | None = None  # custom: at graduation keep riding, sell this % below the high since then
    # momentum filters (2026-11-23, from 149 curve_fast34 trades: coins still being bought in the seconds after the
    # signal won, the ones that stalled at the level or had a big dev bag lost). Checked at the fill; a rule with
    # either one sends its live signal only once they pass, never at the decision.
    min_fill: float | None = None   # buy only if the curve is at least this % sold when our buy would land
    max_dev: float | None = None    # skip if the dev holds this % of the supply or more
    # copycats (2026-11-23, BOBCAT): a coin named like one launched shortly before it (CURVE_COPYCAT_WINDOW_S) is
    # usually a copy riding the original's name, pumped by one wallet; on the day's curve_fast34 trades 1 of 26 won
    skip_copycat: bool = False

    def waits(self) -> bool:
        return self.min_fill is not None or self.max_dev is not None or self.skip_copycat


CURVE_RULES: dict[str, CurveRule] = {
    "curve_70": CurveRule(70.0, "grad"),
    "curve_85": CurveRule(85.0, "grad"),
    # user's rule (2026-11-23): $10, no stop loss, at 2x sell half and put a stop at the entry; exit variants
    "curve_70_half": CurveRule(70.0, "half"),
    "curve_70_trail": CurveRule(70.0, "trail"),
    # from the first 25 curve_70 / 6 curve_85 trades (2026-11-23): every buy whose price fell 10%+ in the 2 s
    # before the fill lost (8 of 8, avg -41%); most trades were up 20-40% before they turned. Half out early.
    "curve_70_filter": CurveRule(70.0, "half", half_at=20.0, skip_drop=10.0),
    "curve_85_half40": CurveRule(85.0, "half", half_at=40.0),
    # user's idea (2026-11-23): buy new tokens at 30% sold, sell at 70% (~2.6x on a classic curve); with / without
    # a stop, side by side
    "curve_30_70": CurveRule(30.0, "range", exit_at=70.0),
    "curve_30_70_sl": CurveRule(30.0, "range", exit_at=70.0, sl=-30.0),
    # from the research (2026-11-23). arXiv 2610.39458: SOL in the curve 60 s after launch is the best single
    # predictor of a token going on. arXiv 2602.14860: reaching a level fast (few, big trades) predicts
    # graduation best, buying on the level alone loses, and selling just before graduation beats selling after.
    # Levels on a classic curve: 5 real SOL = 19.3 % sold, 10 SOL = 33.8 %, vSol 95 = 92.6 %, vSol 105 = 96.6 %.
    "curve_fast19": CurveRule(19.3, "custom", exit_at=96.6, sl=-30.0, size=10.0, max_hold_s=900.0, max_secs=30.0),
    "curve_fast34": CurveRule(33.8, "custom", exit_at=96.6, sl=-25.0, size=10.0, max_hold_s=900.0, max_secs=60.0),
    "curve_93_fast": CurveRule(92.6, "custom", exit_at=99.0, sl=-20.0, size=10.0, max_hold_s=600.0, max_secs=240.0),
    # user's choice (2026-11-23): curve_fast34, buying only coins still being bought after the signal (40 %+ sold at
    # the fill) whose dev holds under 25 %. On the day's trades: 48 of 149 passed, +$18.5, against -$198 for the rest.
    "curve_fast34_mom": CurveRule(33.8, "custom", exit_at=96.6, sl=-25.0, size=10.0, max_hold_s=900.0, max_secs=60.0,
                                  min_fill=40.0, max_dev=25.0, skip_copycat=True,
                                  title="curve_fast34 with momentum: still bought after the signal (40%+ at the "
                                        "fill), dev under 25%, no copycat names"),
    # user's choice (2026-11-23): curve_70 with real money at $10. curve_70 sells at graduation, which a real
    # account can't (it needs an exit level and a stop), so this is curve_70 with the exit just before
    # graduation (99 % sold, ~+225 % on a classic curve), the same -25 % stop and 60 min max hold.
    "curve_70_live": CurveRule(70.0, "custom", exit_at=99.0, sl=-25.0, size=10.0, max_hold_s=3600.0,
                               title="curve_70 for real: buy at 70% sold, sell at 99% sold, stop -25%, 60 min max"),
}
CURVE_STRATEGIES: dict[str, float] = {k: r.level for k, r in CURVE_RULES.items()}
RANGE_LATE_PTS = 10.0   # range: buy only while the curve is at most this many points past the entry level
CUSTOM_PREFIX = "cc"    # names of the owner's own curve strategies (/paper builder): cc<id>


def is_custom(name: str) -> bool:
    return name.startswith(CUSTOM_PREFIX) and name[len(CUSTOM_PREFIX):].isdigit()


def custom_rule(row: Any) -> CurveRule:
    """A curve_custom row -> the rule the engine runs."""
    return CurveRule(float(row.level), "custom", half_at=float(row.half_at or 0) or 100.0,
                     skip_drop=row.skip_drop, exit_at=row.exit_at, sl=row.sl_pct, tp=row.tp_pct,
                     half=row.half_at, size=float(row.size_usd or 10.0), max_hold_s=float(row.max_hold_min or 60) * 60,
                     enabled=bool(row.enabled), title=custom_title(row), max_secs=row.max_secs,
                     skip_backslide=bool(row.skip_backslide), grad_trail=getattr(row, "grad_trail", None))


def custom_title(row: Any) -> str:
    parts = [f"${float(row.size_usd or 10):g}, buy at {float(row.level):g}% sold"]
    if row.exit_at:
        parts.append(f"sell all at {float(row.exit_at):g}% sold")
    if row.tp_pct:
        parts.append(f"sell all at +{float(row.tp_pct):g}%")
    if row.half_at:
        parts.append(f"half at +{float(row.half_at):g}% then stop at entry")
    parts.append(f"stop {float(row.sl_pct):g}%" if row.sl_pct else "no stop")
    if row.skip_drop:
        parts.append(f"skip if it fell {float(row.skip_drop):g}% first")
    if getattr(row, "max_secs", None):
        parts.append(f"skip if slower than {float(row.max_secs):g}s to the level")
    if getattr(row, "skip_backslide", None):
        parts.append("skip if the curve slid back")
    trail = getattr(row, "grad_trail", None)
    parts.append(f"at graduation keep riding, sell {float(trail):g}% off the high" if trail else "graduation sells")
    parts.append(f"{float(row.max_hold_min or 60):g} min max")
    return ", ".join(parts)


def _shown_tp(rule: CurveRule, default_tp: float) -> float:
    """The take-profit % /paper shows for a rule."""
    if rule.mode == "grad":
        return default_tp
    if rule.mode == "custom":
        return rule.tp or rule.half or (_range_gain(rule) if rule.exit_at else 0.0)
    if rule.mode == "range":
        return _range_gain(rule)
    return rule.half_at


def _range_gain(rule: CurveRule) -> float:
    """Price gain (%) from the entry level to the exit level on a classic curve (price ~ 1 / virtual tokens^2)."""
    v = lambda pct: V_TOKENS_START - REAL_TOKENS_START * pct / 100
    return round(((v(rule.level) / v(rule.exit_at or rule.level)) ** 2 - 1) * 100, 1)


def _now() -> datetime:
    return datetime.now(timezone.utc)


_LOOKALIKE = str.maketrans({"а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p",
                            "с": "c", "т": "t", "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d"})


def name_key(symbol: str) -> str:
    """A coin's name for spotting copies: case, accents, spacing and look-alike letters (Cyrillic "Е" in "BЕAR")
    don't matter."""
    import unicodedata

    s = unicodedata.normalize("NFKD", symbol or "").casefold().translate(_LOOKALIKE)
    return "".join(ch for ch in s if ch.isalnum() and ch.isascii())


def parse_curve(data_b64: str) -> dict[str, Any] | None:
    """pump.fun bonding-curve account -> {price, progress, complete} (8-byte discriminator, then u64 virtual
    token, virtual SOL, real token, real SOL reserves, total supply, then bool complete)."""
    try:
        raw = base64.b64decode(data_b64)
        v_tok, v_sol, r_tok, r_sol, _supply = struct.unpack_from("<5Q", raw, 8)
        complete = bool(raw[48]) if len(raw) > 48 else False
    except Exception:
        return None
    v_sol_f, v_tok_f, r_tok_f = v_sol / 1e9, v_tok / 1e6, r_tok / 1e6
    price = v_sol_f / v_tok_f if v_tok_f else None
    if complete:
        return {"complete": True, "price": price, "progress": 100.0}
    # same sanity window as app/sniper.py: anything else is another layout or a misread
    if not price or not 25 <= v_sol_f <= 130 or not 2e8 <= v_tok_f <= 1.1e9 or r_tok_f > REAL_TOKENS_START * 1.01:
        return None
    progress = max(0.0, min(100.0, (1 - r_tok_f / REAL_TOKENS_START) * 100))
    return {"complete": False, "price": price, "progress": progress, "real_sol": r_sol / 1e9}


def parse_trade(msg: dict[str, Any]) -> dict[str, Any] | None:
    """A PumpPortal bonding-curve trade -> {price, progress} from its virtual reserves after the trade."""
    try:
        v_sol, v_tok = float(msg.get("vSolInBondingCurve") or 0), float(msg.get("vTokensInBondingCurve") or 0)
    except (TypeError, ValueError):
        return None
    if not 25 <= v_sol <= 130 or not 2e8 <= v_tok <= 1.1e9:
        return None
    r_tok = v_tok - (V_TOKENS_START - REAL_TOKENS_START)
    progress = max(0.0, min(100.0, (1 - r_tok / REAL_TOKENS_START) * 100))
    try:
        sol = float(msg.get("solAmount") or 0)
    except (TypeError, ValueError):
        sol = 0.0
    return {"complete": False, "price": v_sol / v_tok, "progress": progress,
            "trade": (msg.get("txType"), msg.get("traderPublicKey"), sol)}


@dataclass
class Watch:
    mint: str
    symbol: str
    created: float                    # monotonic time we saw it
    curve: str
    copycat: bool = False             # named like another coin launched shortly before it
    price: float | None = None
    progress: float = 0.0
    graduated: bool = False
    pending: dict[str, tuple[float, float, str]] = field(default_factory=dict)   # strat -> (fill at, px, why)
    held: dict[str, dict[str, Any]] = field(default_factory=dict)
    decided: set[str] = field(default_factory=set)
    creator: str | None = None
    dev_pct: float | None = None          # share of the curve the creator bought at launch
    uri: str | None = None
    dev_sold: bool = False                # the creator sold (seen on PumpPortal's trade stream)
    real_sol: float | None = None         # last SOL in the curve (Helius): its change is a trade's size
    trades: deque = field(default_factory=lambda: deque(maxlen=600))   # (t, side +1/-1, sol, trader|None)
    path: deque = field(default_factory=lambda: deque(maxlen=600))     # (t, progress)
    pre: dict[str, dict[str, Any]] = field(default_factory=dict)       # strat -> snapshot at the decision


class PaperCurve:
    def __init__(self, settings: Settings | None = None, sol_usd: float = 0.0) -> None:
        self.s = settings or get_settings()
        self.sol_usd = sol_usd
        self.watch: dict[str, Watch] = {}
        self.live_signals: set[str] = set()   # rules some live account trades (app/curve_live.py): publish them
        self._names: dict[str, tuple[float, str]] = {}   # name -> (first seen, its mint): for spotting copycats
        self.opened: list[dict[str, Any]] = []
        self.closed: list[dict[str, Any]] = []
        self.feed = self._pick_feed()
        self.pp_subbed: set[str] = set()
        self.pp_events = 0
        self.rules: dict[str, CurveRule] = dict(CURVE_RULES)
        self._social_todo: set[str] = set()
        self._lost_at: float | None = None      # when the sockets dropped (None: connected)

    def set_custom(self, rules: dict[str, CurveRule]) -> None:
        """The owner's strategies from the database. One that was deleted keeps running for its open trades,
        switched off (no new buys), until they close."""
        held = {s for w in self.watch.values() for s in w.held}
        merged = dict(CURVE_RULES)
        merged.update(rules)
        for name, rule in self.rules.items():
            if is_custom(name) and name not in rules and name in held:
                merged[name] = CurveRule(**{**rule.__dict__, "enabled": False})
        self.rules = merged

    def _pick_feed(self) -> str | None:
        """helius / hybrid / pumpportal, or None when what CURVE_FEED needs is missing."""
        has_h, has_pp = bool(self._helius_ws()), bool(self.s.pumpportal_api_key)
        feed = (self.s.curve_feed or "auto").lower()
        if feed == "auto":
            feed = "hybrid" if has_h and has_pp else "helius" if has_h else "pumpportal" if has_pp else ""
        ok = {"helius": has_h, "hybrid": has_h and has_pp, "pumpportal": has_pp}.get(feed, False)
        return feed if ok else None

    # ---- events -------------------------------------------------------------
    def on_create(self, msg: dict[str, Any], now: float) -> str | None:
        """A new token from PumpPortal. Returns the mint if we should watch its curve."""
        mint, curve = msg.get("mint"), str(msg.get("bondingCurveKey") or "")
        if msg.get("txType") != "create" or not mint or not curve or mint in self.watch:
            return None
        copycat = self._seen_name(str(msg.get("symbol") or ""), mint, now)
        if self.s.curve_skip_mayhem and msg.get("is_mayhem_mode"):
            return None
        if len(self.watch) >= self.s.curve_max_watch:
            return None
        w = Watch(mint, str(msg.get("symbol") or "")[:60], now, curve, copycat=copycat)
        w.creator, w.uri = msg.get("traderPublicKey"), (str(msg.get("uri") or "")[:300] or None)
        try:
            w.dev_pct = round(float(msg.get("initialBuy") or 0) / REAL_TOKENS_START * 100, 2)
        except (TypeError, ValueError):
            w.dev_pct = None
        self.watch[mint] = w
        return mint

    def _seen_name(self, symbol: str, mint: str, now: float) -> bool:
        """Remember every launch's name (watched or not); True if another coin had it within the window."""
        key = name_key(symbol)
        if not key:
            return False
        names = self._names
        window = self.s.curve_copycat_window_s
        if len(names) > 20_000:
            for k in [k for k, (t, _) in names.items() if now - t > window]:
                del names[k]
        prev = names.get(key)
        if prev is not None and prev[1] != mint and now - prev[0] <= window:
            return True
        names[key] = (now, mint)
        return False

    def on_curve(self, mint: str, data_b64: str, now: float) -> None:
        """A change of the token's bonding-curve account (from Helius)."""
        st = parse_curve(data_b64)
        w = self.watch.get(mint)
        if w is not None and st and not st["complete"] and st.get("real_sol") is not None:
            if w.real_sol is not None and mint not in self.pp_subbed:     # PumpPortal has the details when streamed
                d = st["real_sol"] - w.real_sol
                if abs(d) > 1e-6:
                    w.trades.append((now, 1 if d > 0 else -1, abs(d), None))
            w.real_sol = st["real_sol"]
        self._update(mint, st, now)

    def on_pp(self, msg: dict[str, Any], now: float) -> None:
        """A PumpPortal trade or migration on a token we stream."""
        mint, tx = msg.get("mint"), msg.get("txType")
        if tx == "migrate":
            self._update(mint, {"complete": True, "price": None, "progress": 100.0}, now)
        elif tx in ("buy", "sell") and mint in self.pp_subbed:
            self.pp_events += 1
            st = parse_trade(msg)
            w = self.watch.get(mint)
            if w is not None and st:
                side, trader, sol = st["trade"]
                w.trades.append((now, 1 if side == "buy" else -1, sol, trader))
                if side == "sell" and trader and trader == w.creator:
                    w.dev_sold = True
            self._update(mint, st, now)

    def _update(self, mint: str | None, st: dict[str, Any] | None, now: float) -> None:
        w = self.watch.get(mint) if mint else None
        if w is None or st is None or w.graduated:
            return
        if st["complete"]:
            w.graduated = True
            w.pending.clear()                         # too late to buy on the curve
            final = st["price"]
            if final and (not w.price or w.price / 20 <= final <= w.price * 20):
                w.price = final
            w.progress = 100.0
            self._exits(w, now)
            if mint in curve_live.EXITS:
                curve_live.on_price(mint, None, graduated=True)
            return
        if w.price and not w.price / 20 <= st["price"] <= w.price * 20:
            return                                    # a 20x jump in one update is a bad reading
        w.price, w.progress = st["price"], st["progress"]
        if mint in curve_live.EXITS:                  # a live account holds it: its stop / exit, at once
            curve_live.on_price(mint, w.price)
        w.path.append((now, w.progress))
        for strat, rule in self.rules.items():
            level = rule.level
            if strat not in w.decided and w.progress >= level:
                w.decided.add(strat)
                if not rule.enabled:
                    continue
                if rule.mode in ("range", "custom") and w.progress > level + RANGE_LATE_PTS:
                    continue                          # first seen far past its entry level: not this trade
                if rule.max_secs is not None and now - w.created > rule.max_secs:
                    continue                          # a slow climb: the buying is weak
                w.pre[strat] = self.snapshot(w, now)
                w.pending[strat] = (now + self.s.curve_latency_s, w.price,
                                    f"curve {w.progress:.0f}% at {now - w.created:.0f}s")
                if self.live_signals and strat in self.live_signals and not rule.waits():   # traded for real
                    curve_live.publish(strat, rule, w.mint, w.symbol, w.price, w.progress,
                                       f"curve {w.progress:.0f}% at {now - w.created:.0f}s",
                                       rule.max_hold_s or self.s.curve_max_hold_s)
        self._fills(w, now)
        self._exits(w, now)

    # ---- PumpPortal trade stream: which tokens to pay for ------------------------
    def pp_from_pct(self) -> float | None:
        """Progress from which a token is streamed from PumpPortal (None: not at all)."""
        return {"hybrid": self.s.curve_pp_from_pct, "pumpportal": 0.0}.get(self.feed)

    def pp_sync(self) -> list[str]:
        """Subscribe / unsubscribe messages that bring the PumpPortal stream in line with the watch list."""
        frm = self.pp_from_pct()
        want = set() if frm is None else {m for m, w in self.watch.items()
                                          if not w.graduated and (w.progress >= frm or w.held)}
        add, drop = sorted(want - self.pp_subbed), sorted(self.pp_subbed - want)
        out = []
        if add:
            out.append(json.dumps({"method": "subscribeTokenTrade", "keys": add}))
        if drop:
            out.append(json.dumps({"method": "unsubscribeTokenTrade", "keys": drop}))
        self.pp_subbed = want
        return out

    def tick(self, now: float) -> list[str]:
        """Every second: delayed fills, max-hold exits, dropping tokens that stalled. Returns mints to unwatch."""
        drop = []
        for mint, w in list(self.watch.items()):
            self._fills(w, now)
            self._exits(w, now)
            if w.held or w.pending:
                continue
            age = now - w.created
            stalled = ((age > self.s.curve_prune1_s and w.progress < self.s.curve_prune1_pct)
                       or (age > self.s.curve_prune2_s and w.progress < self.s.curve_prune2_pct)
                       or age > self.s.curve_max_watch_s)
            done = w.graduated or all(k in w.decided for k, r in self.rules.items() if r.enabled)
            if stalled or done:
                drop.append(mint)
                del self.watch[mint]
        return drop

    # ---- simulated fills ----------------------------------------------------
    def _cost(self) -> float:
        return (self.s.curve_fee_pct + self.s.curve_slippage_pct) / 100

    def _open_count(self, strat: str | None = None) -> int:
        return sum(len(x.held) if strat is None else int(strat in x.held) for x in self.watch.values())

    def snapshot(self, w: Watch, now: float) -> dict[str, Any]:
        """What the token looks like at our decision: kept on the paper trade to find what winners share."""
        last = [t for t in w.trades if now - t[0] <= 60]
        buys = [t for t in last if t[1] > 0]
        sells = [t for t in last if t[1] < 0]
        known = [t for t in last if t[3]]
        back = [p for (t, p) in w.path if now - t <= 60]
        return {
            "secs_to_level": round(now - w.created, 1), "level_at": round(w.progress, 1),
            "dev_pct": w.dev_pct, "dev_sold": w.dev_sold if w.creator else None, "copycat": w.copycat,
            "buys_60s": len(buys), "sells_60s": len(sells),
            "sol_in_60s": round(sum(t[2] for t in buys), 3), "sol_out_60s": round(sum(t[2] for t in sells), 3),
            "buyers_60s": len({t[3] for t in known if t[1] > 0}) if known else None,
            "progress_60s": round(w.progress - back[0], 1) if back else None,
            "uri": w.uri,
        }

    def _fills(self, w: Watch, now: float) -> None:
        for strat, (at, decision_px, why) in list(w.pending.items()):
            if now < at or not w.price:
                continue
            del w.pending[strat]
            if strat in w.held or self._open_count(strat) >= self.s.curve_max_open:
                continue
            rule = self.rules[strat]
            if rule.mode in ("range", "custom") and rule.exit_at is not None and w.progress >= rule.exit_at:
                continue                                  # already at the exit level: nothing left to make
            drop = rule.skip_drop
            if drop is not None and decision_px and w.price < decision_px * (1 - drop / 100):
                continue                                  # being sold into: the price fell before our buy landed
            pre = w.pre.get(strat) or {}
            if rule.skip_backslide and pre.get("level_at") is not None and w.progress < pre["level_at"]:
                continue                                  # the curve went back down before our buy landed
            if rule.min_fill is not None and w.progress < rule.min_fill:
                continue                                  # stalled at the level: nobody kept buying
            if rule.max_dev is not None and (pre.get("dev_pct") or 0) >= rule.max_dev:
                continue                                  # the dev holds too much of it
            if rule.skip_copycat and w.copycat:
                continue                                  # a copy of a coin launched just before it
            pos = {"strategy": strat, "mint": w.mint, "symbol": w.symbol, "market": w.price,
                   "fill": w.price * (1 + self._cost()), "at": now, "wall": _now(), "peak": 0.0, "trough": 0.0,
                   "size": self._size(strat), "rem": 1.0, "proceeds": 0.0, "half": False, "gpeak": None,
                   "features": {**pre, "paid_pct": round((w.price / decision_px - 1) * 100, 1) if decision_px else None,
                                "fill_level": round(w.progress, 1)},
                   "why": f"{why} +{self.s.curve_latency_s:g}s, filled at {w.progress:.0f}%, "
                          f"paid {(w.price / decision_px - 1) * 100 if decision_px else 0:+.0f}% vs decision"}
            w.held[strat] = pos
            self.opened.append(pos)
            if rule.waits() and self.live_signals and strat in self.live_signals:
                # its filters passed: the live accounts buy now, at this price
                curve_live.publish(strat, rule, w.mint, w.symbol, w.price, w.progress,
                                   f"{why}, {w.progress:.0f}% at the fill", rule.max_hold_s or self.s.curve_max_hold_s)

    def _size(self, strat: str) -> float:
        rule = self.rules[strat]
        if rule.size:
            return rule.size
        return self.s.curve_half_size_usd if rule.mode in ("half", "trail", "range") else self.s.curve_size_usd

    @staticmethod
    def worth(pos: dict[str, Any], price: float, cost: float) -> float:
        """What the position brought in so far plus what is left would fetch at `price` (after cost)."""
        return pos["proceeds"] + pos["size"] * pos["rem"] / pos["fill"] * price * (1 - cost)

    def _close(self, w: Watch, strat: str, pos: dict[str, Any], reason: str, cost: float) -> None:
        pnl = self.worth(pos, w.price, cost) - pos["size"]
        pos.update(exit=w.price, reason=reason, pnl=pnl, pnl_pct=pnl / pos["size"] * 100, exit_wall=_now(),
                   proceeds=pnl + pos["size"], rem=0.0)
        self.closed.append(pos)
        del w.held[strat]

    def _exits(self, w: Watch, now: float) -> None:
        if not w.price:
            return
        for strat, pos in list(w.held.items()):
            pct = (w.price / pos["market"] - 1) * 100
            pos["peak"], pos["trough"] = max(pos["peak"], pct), min(pos["trough"], pct)
            cost = self._cost()
            rule = self.rules.get(strat)
            if rule is None:
                continue
            mode = rule.mode
            reason = None
            if mode == "grad":
                if w.graduated:
                    reason, cost = "graduated", cost + self.s.curve_grad_exit_extra_pct / 100
                elif self.s.curve_tp_pct > 0 and pct >= self.s.curve_tp_pct:
                    reason = "take_profit"
                elif pct <= self.s.curve_sl_pct:
                    reason = "stop_loss"
                elif now - pos["at"] >= self.s.curve_max_hold_s:
                    reason = "time"
                if reason:
                    self._close(w, strat, pos, reason, cost)
                continue
            if mode == "custom":
                sale_cost = cost + (self.s.curve_grad_exit_extra_pct / 100 if w.graduated else 0)
                if rule.half and not pos["half"] and pct >= rule.half:
                    pos["proceeds"] += pos["size"] * 0.5 / pos["fill"] * w.price * (1 - sale_cost)
                    pos["rem"], pos["half"] = 0.5, True
                    pos["why"] = (pos.get("why") or "")[:120] + f" | half sold at +{rule.half:g}%"
                riding = w.graduated and rule.grad_trail is not None
                if riding:                                       # the rest rides on PumpSwap from the graduation
                    pos["gpeak"] = max(pos["gpeak"] or w.price, w.price)
                    cost = sale_cost
                if w.graduated and not riding:
                    reason, cost = "graduated", sale_cost
                elif riding and w.price <= pos["gpeak"] * (1 - rule.grad_trail / 100):
                    reason = "trailing_stop"
                elif rule.exit_at is not None and w.progress >= rule.exit_at and not riding:
                    reason = "curve_target"
                elif rule.tp is not None and pct >= rule.tp:
                    reason = "take_profit"
                elif pos["half"] and w.price <= pos["market"]:
                    reason = "breakeven_stop"
                elif not pos["half"] and rule.sl is not None and pct <= rule.sl:
                    reason = "stop_loss"
                elif now - pos["at"] >= (rule.max_hold_s or self.s.curve_max_hold_s):
                    reason = "time"
                if reason:
                    self._close(w, strat, pos, reason, cost)
                continue
            if mode == "range":
                if w.graduated:
                    reason, cost = "graduated", cost + self.s.curve_grad_exit_extra_pct / 100
                elif rule.exit_at is not None and w.progress >= rule.exit_at:
                    reason = "curve_target"                  # reached the exit level: everything out
                elif rule.sl is not None and pct <= rule.sl:
                    reason = "stop_loss"
                elif now - pos["at"] >= self.s.curve_max_hold_s:
                    reason = "time"
                if reason:
                    self._close(w, strat, pos, reason, cost)
                continue
            # half / trail: no stop loss; at +half_at sell half, then a stop at the entry
            sale_cost = cost + (self.s.curve_grad_exit_extra_pct / 100 if w.graduated and pos["gpeak"] is None else 0)
            half_at = rule.half_at
            if not pos["half"] and pct >= half_at:
                pos["proceeds"] += pos["size"] * 0.5 / pos["fill"] * w.price * (1 - sale_cost)
                pos["rem"], pos["half"] = 0.5, True
                pos["why"] = (pos.get("why") or "")[:120] + f" | half sold at +{half_at:g}%"
            if pos["half"] and w.price <= pos["market"]:
                reason = "breakeven_stop"                    # gave back the move: the rest out at the entry
            elif w.graduated and mode == "half":
                reason, cost = "graduated", sale_cost
            elif w.graduated:                                # trail: the rest rides on PumpSwap
                if pos["gpeak"] is None:
                    pos["gpeak"] = w.price                   # graduation price: the trail starts here
                pos["gpeak"] = max(pos["gpeak"], w.price)
                if w.price <= pos["gpeak"] * (1 - self.s.curve_trail_pct / 100):
                    reason = "trailing_stop"
                elif now - pos["at"] >= self.s.curve_trail_max_hold_s:
                    reason = "time"
            elif now - pos["at"] >= self.s.curve_max_hold_s:
                reason = "time"
            if reason:
                self._close(w, strat, pos, reason, cost)

    def riding(self) -> list[str]:
        """Graduated tokens a trail position still holds: priced from Jupiter (the curve is gone)."""
        return [m for m, w in self.watch.items() if w.graduated and w.held]

    def on_riding_price(self, mint: str, price_sol: float, now: float) -> None:
        w = self.watch.get(mint)
        if w is None or not w.graduated or not price_sol or (w.price and not w.price / 20 <= price_sol <= w.price * 20):
            return
        w.price = price_sol
        self._exits(w, now)

    # ---- persistence ----------------------------------------------------------
    def _usd(self, sol_px: float) -> float:
        return sol_px * self.sol_usd if self.sol_usd else sol_px

    async def flush(self, session) -> int:
        """Write opened / closed paper positions (strategy curve_*) so /paper shows them."""
        n = 0
        rows: list[tuple[dict[str, Any], PaperPosition]] = []
        opened, self.opened = self.opened, []          # buys that land while this awaits wait for the next flush
        for pos in opened:
            rule = self.rules[pos["strategy"]]
            grad = rule.mode == "grad"
            hold_s = (rule.max_hold_s or self.s.curve_max_hold_s) if rule.mode != "trail" else self.s.curve_trail_max_hold_s
            row = PaperPosition(strategy=pos["strategy"], chain=Chain.solana, token_address=pos["mint"],
                                symbol=pos["symbol"], trigger=pos["why"][:160], status="open",
                                size_usd=pos["size"], entry_time=pos["wall"],
                                entry_price=self._usd(pos["market"]), fill_price=self._usd(pos["fill"]),
                                tp_pct=_shown_tp(rule, self.s.curve_tp_pct),
                                sl_pct=self.s.curve_sl_pct if grad else (rule.sl or 0.0),
                                max_hold_minutes=max(int(hold_s // 60), 1),
                                remaining_frac=1.0, proceeds_usd=0.0, features=pos.get("features"))
            session.add(row)
            rows.append((pos, row))
            n += 1
        await self._socials(rows)
        await session.flush()
        for pos, row in rows:             # keep the id: each flush has its own session
            pos["row"] = row.id
        batch = list(self.closed)                     # closes that land while this awaits wait for the next flush
        for c in batch:
            row = await session.get(PaperPosition, c["row"]) if c.get("row") else None
            if row is None:
                continue
            row.status, row.exit_time, row.exit_reason = "closed", c["exit_wall"], c["reason"]
            row.exit_price = row.last_price = self._usd(c["exit"])
            row.peak_pct, row.trough_pct = c["peak"], c["trough"]
            row.pnl_usd, row.pnl_pct = c["pnl"], c["pnl_pct"]
            row.proceeds_usd, row.remaining_frac = c["pnl"] + row.size_usd, 0.0
            row.trigger = (c.get("why") or row.trigger or "")[:160]
            n += 1
        done = {id(c) for c in batch if c.get("row") is not None}
        self.closed = [c for c in self.closed if id(c) not in done]
        n += await self._mark_open(session)
        await session.commit()
        return n

    _social_cache: dict[str, dict[str, bool]] = {}

    async def _socials(self, rows: list[tuple[dict[str, Any], PaperPosition]]) -> None:
        """has_twitter / has_website / has_telegram: from the cache now; unknown tokens are read in the background
        (never inside a flush: a slow metadata host must not stall the bot)."""
        for _, row in rows:
            f = dict(row.features or {})
            if f.get("uri") in self._social_cache:
                f.update({f"has_{k}": v for k, v in self._social_cache[f["uri"]].items()})
                row.features = f
        self._social_todo.update((row.features or {}).get("uri") for _, row in rows)
        self._social_todo -= {None, ""} | set(self._social_cache)

    async def fetch_socials(self, SessionLocal) -> None:
        """Background: read queued token metadata and add the socials to their paper rows."""
        try:
            await self._fetch_socials(SessionLocal)
        except Exception:
            log.warning("curve (paper): socials lookup failed", exc_info=True)

    async def _fetch_socials(self, SessionLocal) -> None:
        import httpx

        todo = list(self._social_todo)[:5]
        self._social_todo -= set(todo)
        if not todo:
            return
        async with httpx.AsyncClient(timeout=4, follow_redirects=True) as c:
            for uri in todo:
                try:
                    meta = (await c.get(uri)).json()
                    meta = meta if isinstance(meta, dict) else {}
                except Exception:
                    meta = {}
                self._social_cache[uri] = {k: bool(meta.get(k)) for k in ("twitter", "website", "telegram")}
        async with SessionLocal() as session:
            rows = (await session.execute(select(PaperPosition).where(
                PaperPosition.entry_time >= _now() - timedelta(hours=3),
                PaperPosition.features.is_not(None)))).scalars().all()
            for row in rows:
                f = dict(row.features or {})
                if f.get("uri") in todo and "has_twitter" not in f:
                    f.update({f"has_{k}": v for k, v in self._social_cache[f["uri"]].items()})
                    row.features = f
            await session.commit()

    async def _mark_open(self, session) -> int:
        n, stamp = 0, _now()
        # copies: the feed readers add and drop tokens while this awaits the database (a live view of the dicts
        # raised "dictionary changed size during iteration" and closed every open position as feed_lost)
        for w in list(self.watch.values()):
            if not w.price:
                continue
            for pos in list(w.held.values()):
                row = await session.get(PaperPosition, pos["row"]) if pos.get("row") else None
                if row is None:
                    continue
                pnl = self.worth(pos, w.price, self._cost()) - pos["size"]
                row.last_price, row.last_priced_at = self._usd(w.price), stamp
                row.peak_pct, row.trough_pct = pos["peak"], pos["trough"]
                row.pnl_usd, row.pnl_pct = pnl, pnl / pos["size"] * 100
                row.remaining_frac, row.proceeds_usd = pos["rem"], pos["proceeds"]
                row.trigger = (pos.get("why") or row.trigger or "")[:160]
                n += 1
        return n

    # ---- loop -------------------------------------------------------------------
    def _helius_ws(self) -> str | None:
        url = self.s.live_rpc_url or ""
        return url.replace("https://", "wss://", 1) if url.startswith("https://") and "helius" in url else None

    async def run_forever(self) -> None:
        if not self.s.curve_paper_enabled:
            return
        if not self.feed:
            log.warning("curve (paper): CURVE_FEED=%s needs a Helius LIVE_RPC_URL and/or PUMPPORTAL_API_KEY; "
                        "not started", self.s.curve_feed)
            return
        import websockets

        from app.db import SessionLocal

        async with SessionLocal() as session:          # a restart left these with no feed: close them flat
            stale = (await session.execute(select(PaperPosition).where(
                PaperPosition.strategy.in_(tuple(CURVE_STRATEGIES)) | PaperPosition.strategy.like(CUSTOM_PREFIX + "%"),
                PaperPosition.status == "open"))).scalars().all()
            for p in stale:
                p.status, p.exit_reason, p.exit_time = "closed", "restart", _now()
                p.exit_price = p.last_price or p.entry_price
                p.pnl_usd = p.pnl_usd if p.pnl_usd is not None else 0.0
                p.pnl_pct = p.pnl_pct if p.pnl_pct is not None else 0.0
            await session.commit()
        # one PumpPortal connection for new tokens, trades and migrations (keyed when we have a key)
        pp_url = FEED_URL + (f"?api-key={self.s.pumpportal_api_key}" if self.s.pumpportal_api_key else "")
        hws = self._helius_ws() if self.feed in ("helius", "hybrid") else None
        backoff = 2.0
        while True:
            try:
                await self._refresh_sol_usd()
                async with contextlib.AsyncExitStack() as stack:
                    pp = await stack.enter_async_context(
                        websockets.connect(pp_url, ping_interval=20, max_size=2 ** 22))
                    hl = (await stack.enter_async_context(websockets.connect(hws, ping_interval=20, max_size=2 ** 22))
                          if hws else None)
                    await pp.send(json.dumps({"method": "subscribeNewToken"}))
                    if self.feed == "pumpportal":
                        await pp.send(json.dumps({"method": "subscribeMigration"}))
                    log.info("curve (paper): connected (feed %s)", self.feed)
                    backoff = 2.0
                    self.pp_subbed = set()             # a new connection starts with no subscriptions
                    self._req: dict[int, str] = {}
                    self._sub: dict[int, str] = {}
                    self._mint_sub: dict[str, int] = {}
                    for mint in self.after_reconnect(time.monotonic()) if hl is not None else ():
                        await self._subscribe(hl, mint)    # the coins we still hold: watch them again
                    jobs = [self._read_pp(pp, hl), self._housekeep(pp, hl, SessionLocal)]
                    if hl is not None:
                        jobs.append(self._read_curves(hl))
                    await asyncio.gather(*jobs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("curve (paper): feed error %s; reconnecting in %.0fs", exc, backoff)
                self.on_feed_lost(time.monotonic())     # open trades wait for the reconnect (up to a limit)
                try:
                    async with SessionLocal() as session:
                        await self.flush(session)
                except Exception:
                    log.exception("curve (paper): flush after feed loss failed")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    FEED_GRACE_S = 300.0      # a reconnect within this keeps the open trades; a longer outage closes them

    def on_feed_lost(self, now: float) -> None:
        """The sockets dropped: forget tokens we only watch, keep the ones we hold (no buys pending; they are
        watched again after the reconnect). Short outages (a 502, a dropped frame) cost nothing."""
        if self._lost_at is None:
            self._lost_at = now
        for mint, w in list(self.watch.items()):
            w.pending.clear()
            if not w.held:
                del self.watch[mint]
        self.pp_subbed = set()

    def after_reconnect(self, now: float) -> list[str]:
        """Back online: held coins to watch again, or, after an outage longer than FEED_GRACE_S, their trades
        closed at the last price we had (feed_lost)."""
        lost, self._lost_at = self._lost_at, None
        if lost is not None and now - lost > self.FEED_GRACE_S:
            for w in self.watch.values():
                for pos in list(w.held.values()):
                    px = w.price or pos["market"]
                    pnl = self.worth(pos, px, self._cost()) - pos["size"]
                    pos.update(exit=px, reason="feed_lost", exit_wall=_now(), pnl=pnl, pnl_pct=pnl / pos["size"] * 100)
                    self.closed.append(pos)
                w.held.clear()
            self.watch.clear()
            return []
        return [m for m, w in self.watch.items() if w.held and not w.graduated]

    _ids = itertools.count(1)

    async def _subscribe(self, hl, mint: str) -> None:
        w = self.watch.get(mint)
        if not w:
            return
        rid = next(self._ids)
        self._req[rid] = mint
        await hl.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": "accountSubscribe",
                                  "params": [w.curve, {"encoding": "base64", "commitment": "processed"}]}))

    async def _read_pp(self, pp, hl) -> None:
        async for raw in pp:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("errors"):
                log.warning("curve (paper): PumpPortal %s", str(msg)[:200])
                continue
            now = time.monotonic()
            if msg.get("txType") == "create":
                mint = self.on_create(msg, now)
                if mint and hl is not None:
                    await self._subscribe(hl, mint)
            else:
                self.on_pp(msg, now)

    async def _read_curves(self, hl) -> None:
        async for raw in hl:
            msg = json.loads(raw)
            if "id" in msg and "result" in msg:              # subscription confirmed
                mint = self._req.pop(msg["id"], None)
                if mint and isinstance(msg["result"], int):
                    self._sub[msg["result"]] = mint
                    self._mint_sub[mint] = msg["result"]
                continue
            p = msg.get("params") or {}
            mint = self._sub.get(p.get("subscription"))
            data = (((p.get("result") or {}).get("value") or {}).get("data") or [None])[0]
            if mint and data:
                self.on_curve(mint, data, time.monotonic())

    async def _housekeep(self, pp, hl, SessionLocal) -> None:
        last_flush = last_px = last_cost = last_ride = time.monotonic()
        last_rules = 0.0
        self._social_task: asyncio.Task | None = None
        events_at = self.pp_events
        while True:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            if now - last_rules >= 30.0:
                last_rules = now
                await self._load_custom(SessionLocal)
            dropped = self.tick(now)
            for m in self.pp_sync():
                await pp.send(m)
            for mint in dropped if hl is not None else ():
                sid = self._mint_sub.pop(mint, None)
                if sid is not None:
                    self._sub.pop(sid, None)
                    await hl.send(json.dumps({"jsonrpc": "2.0", "id": next(self._ids),
                                              "method": "accountUnsubscribe", "params": [sid]}))
            if now - last_flush >= 5.0 and (self.opened or self.closed or any(w.held for w in self.watch.values())):
                last_flush = now
                async with SessionLocal() as session:
                    await self.flush(session)
            if now - last_px >= 60.0:
                last_px = now
                await self._refresh_sol_usd()
            if self._social_todo and not (self._social_task and not self._social_task.done()):
                self._social_task = asyncio.create_task(self.fetch_socials(SessionLocal))
            if self.riding() and now - last_ride >= self.s.curve_trail_price_seconds:
                last_ride = now
                await self._price_riding(now)
            if self.pp_from_pct() is not None and now - last_cost >= 600.0:
                n, events_at, last_cost = self.pp_events - events_at, self.pp_events, now
                # 0.01 SOL per 10,000 events; 144 ten-minute windows a day
                log.info("curve (paper): %d PumpPortal events in 10 min, %d tokens streamed (~%.3f SOL/day)",
                         n, len(self.pp_subbed), n * 144 * 0.01 / 10_000)

    async def _load_custom(self, SessionLocal) -> None:
        from app.models import CurveCustom

        try:
            async with SessionLocal() as session:
                rows = (await session.execute(select(CurveCustom))).scalars().all()
            self.set_custom({r.name: custom_rule(r) for r in rows if r.name})
        except Exception:
            log.warning("curve (paper): could not load custom strategies", exc_info=True)
        try:                                       # which rules live accounts trade for real (app/curve_live.py)
            from app.models import TradingAccount

            from app.accounts import bots_of

            async with SessionLocal() as session:
                accts = (await session.execute(select(TradingAccount).where(
                    TradingAccount.enabled.is_(True)))).scalars().all()
            self.live_signals = {k for r in accts for k, v in bots_of(r).items() if v.get("enabled")}
        except Exception:
            log.warning("curve (paper): could not load the live curve accounts", exc_info=True)

    async def _price_riding(self, now: float) -> None:
        """Graduated coins a trail position still holds: Jupiter's price (USD -> SOL per token)."""
        import httpx

        from app.live import JUPITER

        mints = self.riding()[:50]
        if not mints or not self.sol_usd:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await JUPITER.request(c, "GET", f"{self.s.jupiter_api_url}/price/v3", background=True,
                                          params={"ids": ",".join(mints)})
            data = (r.json() or {}) if r is not None and r.status_code == 200 else {}
        except Exception:
            log.debug("curve (paper): riding prices failed", exc_info=True)
            return
        for m in mints:
            usd = (data.get(m) or {}).get("usdPrice")
            if usd:
                self.on_riding_price(m, float(usd) / self.sol_usd, now)

    async def _refresh_sol_usd(self) -> None:
        import httpx

        from app.live import SOL_MINT

        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{self.s.jupiter_api_url}/price/v3", params={"ids": SOL_MINT})
                self.sol_usd = float(r.json()[SOL_MINT]["usdPrice"])
        except Exception:
            log.warning("curve (paper): SOL price unavailable")
