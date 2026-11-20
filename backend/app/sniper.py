"""Paper sniper for new pump.fun tokens: simulated only (no keys, no transactions, no money).

Question it answers: if we bought new pump.fun tokens within seconds of their launch and sold within minutes,
would it make money after fees, slippage and our real delay?

Feeds: PumpPortal's free WebSocket (wss://pumpportal.fun/api/data) sends every new token as it is created,
with its bonding-curve account. Trades come from Helius instead (PumpPortal's trade stream needs a paid key):
an accountSubscribe on that bonding curve pushes every change, and its virtual reserves give the price
(SOL per token = vSol / vTokens) after every trade; a rise in its real SOL is a buy, a fall a sell. Buyers'
addresses are not in the account, so the momentum rule counts buys.

Strategies (side by side, results on /paper):
  snipe_all       every SNIPER_SAMPLE_EVERY-th new token, bought SNIPER_LATENCY_S after we see it
  snipe_momentum  a new token that, within SNIPER_WINDOW_S of launch, gets >= SNIPER_MIN_BUYERS different
                  buyers and >= SNIPER_MIN_NET_SOL net SOL in; bought SNIPER_LATENCY_S after that

Fill: the price of the first trade at or after (decision time + latency), plus SNIPER_FEE_PCT (pump.fun fee)
and SNIPER_SLIPPAGE_PCT, on entry and again on exit. Exit at take profit, stop loss, or max hold (checked on
every trade and every second). "ahead" in the trigger = buys that landed between launch and our fill.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import logging
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from app.config import Settings, get_settings
from app.models import Chain, PaperPosition

log = logging.getLogger("sniper")

FEED_URL = "wss://pumpportal.fun/api/data"
SNIPE_STRATEGIES = ("snipe_all", "snipe_momentum")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def curve_state(data_b64: str) -> dict[str, float] | None:
    """pump.fun bonding-curve account -> reserves (8-byte discriminator, then u64 virtual token, virtual SOL,
    real token, real SOL reserves, total supply, then bool complete). Tokens have 6 decimals."""
    try:
        raw = base64.b64decode(data_b64)
        v_tok, v_sol, r_tok, r_sol, _supply = struct.unpack_from("<5Q", raw, 8)
        complete = bool(raw[48]) if len(raw) > 48 else False
    except Exception:
        return None
    if not v_tok:
        return None
    return {"vSolInBondingCurve": v_sol / 1e9, "vTokensInBondingCurve": v_tok / 1e6,
            "real_sol": r_sol / 1e9, "complete": complete}


def price_of(ev: dict[str, Any]) -> float | None:
    """SOL per token from the bonding curve's virtual reserves in a PumpPortal event."""
    v_sol, v_tok = ev.get("vSolInBondingCurve"), ev.get("vTokensInBondingCurve")
    try:
        return float(v_sol) / float(v_tok) if v_sol and v_tok else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


@dataclass
class Watch:
    """One new token we are watching (and maybe holding, on paper)."""
    mint: str
    symbol: str
    created: float                    # monotonic time we saw it
    price: float | None               # last SOL/token
    curve: str = ""                   # bonding-curve account (watched on Helius)
    real_sol: float | None = None     # last real SOL in the curve: up = buy, down = sell
    buyers: set[str] = field(default_factory=set)
    net_sol: float = 0.0
    buys: int = 0
    # pending entries: strategy -> (fill at monotonic time, decision price, why)
    pending: dict[str, tuple[float, float, str]] = field(default_factory=dict)
    # open paper positions: strategy -> dict
    held: dict[str, dict[str, Any]] = field(default_factory=dict)
    decided: set[str] = field(default_factory=set)


class PaperSniper:
    def __init__(self, settings: Settings | None = None, sol_usd: float = 0.0) -> None:
        self.s = settings or get_settings()
        self.sol_usd = sol_usd
        self.watch: dict[str, Watch] = {}
        self.seen = 0
        self.closed: list[dict[str, Any]] = []      # finished trades, for the writer
        self.opened: list[dict[str, Any]] = []

    # ---- events -------------------------------------------------------------
    def on_message(self, msg: dict[str, Any], now: float) -> list[str]:
        """Feed one PumpPortal message. Returns mints to subscribe to (new tokens)."""
        tx = msg.get("txType")
        mint = msg.get("mint")
        if not mint or not tx:
            return []
        if tx == "create":
            self.seen += 1
            w = Watch(mint, str(msg.get("symbol") or "")[:60], now, price_of(msg), curve=str(msg.get("bondingCurveKey") or ""))
            dev_sol = float(msg.get("solAmount") or 0)
            if dev_sol > self.s.sniper_max_dev_buy_sol:
                return []          # the creator bought big at launch: a dump waiting to happen
            if self.s.sniper_skip_mayhem and msg.get("is_mayhem_mode"):
                return []          # pump.fun's own agent moves this curve's price: not a fair test
            if self.seen % max(self.s.sniper_sample_every, 1) == 0:
                w.pending["snipe_all"] = (now + self.s.sniper_latency_s, w.price or 0.0, "new token")
                w.decided.add("snipe_all")
            self.watch[mint] = w
            return [mint]
        w = self.watch.get(mint)
        if w is None:
            return []
        px = price_of(msg)
        if px:
            w.price = px
        sol = float(msg.get("solAmount") or 0)
        trader = str(msg.get("traderPublicKey") or "")
        if tx == "buy":
            w.buys += 1
            w.net_sol += sol
            if trader:
                w.buyers.add(trader)
        elif tx == "sell":
            w.net_sol -= sol
        # tx == "update": price only
        # momentum decision inside the window
        buyers = len(w.buyers) or w.buys          # the curve feed has no addresses: count buys
        if ("snipe_momentum" not in w.decided and now - w.created <= self.s.sniper_window_s
                and buyers >= self.s.sniper_min_buyers and w.net_sol >= self.s.sniper_min_net_sol):
            w.decided.add("snipe_momentum")
            w.pending["snipe_momentum"] = (now + self.s.sniper_latency_s, w.price or 0.0,
                                           f"{buyers} buys {w.net_sol:.1f} SOL in {now - w.created:.0f}s")
        self._fills(w, now)
        self._exits(w, now)
        return []

    def tick(self, now: float) -> list[str]:
        """Every second: fills whose delay passed with no trade, max-hold exits, forgetting stale tokens.
        Returns mints to unsubscribe from."""
        drop = []
        for mint, w in list(self.watch.items()):
            self._fills(w, now)
            self._exits(w, now)
            idle = not w.pending and not w.held
            if idle and now - w.created > max(self.s.sniper_window_s, self.s.sniper_latency_s) + 2:
                drop.append(mint)
                del self.watch[mint]
        return drop

    # ---- simulated fills ----------------------------------------------------
    def _cost(self) -> float:
        return (self.s.sniper_fee_pct + self.s.sniper_slippage_pct) / 100

    def _fills(self, w: Watch, now: float) -> None:
        for strat, (at, decision_px, why) in list(w.pending.items()):
            if now < at or not w.price:
                continue
            if len(w.held) >= 1 and strat in w.held:
                del w.pending[strat]
                continue
            if sum(len(x.held) for x in self.watch.values()) >= self.s.sniper_max_open:
                del w.pending[strat]
                continue
            fill = w.price * (1 + self._cost())
            pos = {"strategy": strat, "mint": w.mint, "symbol": w.symbol, "market": w.price, "fill": fill,
                   "decision": decision_px, "at": now, "wall": _now(), "peak": 0.0, "trough": 0.0,
                   "why": f"{why} +{self.s.sniper_latency_s:g}s, ahead {w.buys}, "
                          f"paid {((w.price / decision_px) - 1) * 100 if decision_px else 0:+.0f}% vs decision"}
            w.held[strat] = pos
            del w.pending[strat]
            self.opened.append(pos)

    def _exits(self, w: Watch, now: float) -> None:
        if not w.price:
            return
        for strat, pos in list(w.held.items()):
            pct = (w.price / pos["market"] - 1) * 100
            pos["peak"], pos["trough"] = max(pos["peak"], pct), min(pos["trough"], pct)
            reason = None
            if pct >= self.s.sniper_tp_pct:
                reason = "take_profit"
            elif pct <= self.s.sniper_sl_pct:
                reason = "stop_loss"
            elif now - pos["at"] >= self.s.sniper_max_hold_s:
                reason = "time"
            if reason:
                out = w.price * (1 - self._cost())
                size = self.s.sniper_size_usd
                pnl = size / pos["fill"] * out - size
                # same dict as in `opened`: flush() writes the row first, then closes it
                pos.update(exit=w.price, reason=reason, pnl=pnl, pnl_pct=pnl / size * 100, exit_wall=_now())
                self.closed.append(pos)
                del w.held[strat]

    def on_curve(self, mint: str, data_b64: str, now: float) -> None:
        """A change of the token's bonding-curve account (from Helius) -> a buy / sell event."""
        w = self.watch.get(mint)
        st = curve_state(data_b64)
        if w is None or st is None:
            return
        prev, w.real_sol = w.real_sol, st["real_sol"]
        delta = 0.0 if prev is None else st["real_sol"] - prev
        side = "buy" if delta > 0 else "sell" if delta < 0 else "update"
        self.on_message({"txType": side, "mint": mint, "solAmount": abs(delta), "traderPublicKey": "",
                         "vSolInBondingCurve": st["vSolInBondingCurve"],
                         "vTokensInBondingCurve": st["vTokensInBondingCurve"]}, now)

    # ---- persistence ----------------------------------------------------------
    async def flush(self, session) -> int:
        """Write opened / closed paper positions (strategy snipe_*) so /paper shows them."""
        n = 0
        rows: dict[tuple[str, str], PaperPosition] = {}
        for pos in self.opened:
            usd = self.sol_usd or 0.0
            row = PaperPosition(strategy=pos["strategy"], chain=Chain.solana, token_address=pos["mint"],
                                symbol=pos["symbol"], trigger=pos["why"][:160], status="open",
                                size_usd=self.s.sniper_size_usd, entry_time=pos["wall"],
                                entry_price=pos["market"] * usd if usd else pos["market"],
                                fill_price=pos["fill"] * usd if usd else pos["fill"],
                                tp_pct=self.s.sniper_tp_pct, sl_pct=self.s.sniper_sl_pct,
                                max_hold_minutes=max(int(self.s.sniper_max_hold_s // 60), 1),
                                remaining_frac=1.0, proceeds_usd=0.0)
            session.add(row)
            rows[(pos["strategy"], pos["mint"])] = row
            pos["row"] = row
            n += 1
        self.opened.clear()
        await session.flush()
        for c in self.closed:
            row = c.get("row")
            if row is None:
                continue
            usd = self.sol_usd or 0.0
            row.status, row.exit_time, row.exit_reason = "closed", c["exit_wall"], c["reason"]
            row.exit_price = c["exit"] * usd if usd else c["exit"]
            row.last_price = row.exit_price
            row.peak_pct, row.trough_pct = c["peak"], c["trough"]
            row.pnl_usd, row.pnl_pct = c["pnl"], c["pnl_pct"]
            row.proceeds_usd, row.remaining_frac = c["pnl"] + row.size_usd, 0.0
            n += 1
        self.closed = [c for c in self.closed if c.get("row") is None]
        await session.commit()
        return n

    # ---- loop -------------------------------------------------------------------
    def _helius_ws(self) -> str | None:
        url = self.s.live_rpc_url or ""
        return url.replace("https://", "wss://", 1) if url.startswith("https://") and "helius" in url else None

    async def run_forever(self) -> None:
        if not self.s.sniper_paper_enabled:
            return
        hws = self._helius_ws()
        if not hws:
            log.warning("sniper (paper): needs a Helius LIVE_RPC_URL for prices; not started")
            return
        import websockets

        from app.db import SessionLocal

        async with SessionLocal() as session:          # a restart left these with no feed: close them flat
            stale = (await session.execute(select(PaperPosition).where(
                PaperPosition.strategy.in_(SNIPE_STRATEGIES), PaperPosition.status == "open"))).scalars().all()
            for p in stale:
                p.status, p.exit_reason, p.exit_time = "closed", "restart", _now()
                p.exit_price = p.last_price or p.entry_price
                p.pnl_usd = p.pnl_usd if p.pnl_usd is not None else 0.0
                p.pnl_pct = p.pnl_pct if p.pnl_pct is not None else 0.0
            await session.commit()
        backoff = 2.0
        while True:
            try:
                await self._refresh_sol_usd()
                async with websockets.connect(FEED_URL, ping_interval=20, max_size=2 ** 20) as pp, \
                        websockets.connect(hws, ping_interval=20, max_size=2 ** 22) as hl:
                    await pp.send(json.dumps({"method": "subscribeNewToken"}))
                    log.info("sniper (paper): connected (new tokens: PumpPortal, prices: Helius)")
                    backoff = 2.0
                    await asyncio.gather(self._read_new(pp, hl), self._read_prices(hl), self._housekeep(hl, SessionLocal))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("sniper (paper): feed error %s; reconnecting in %.0fs", exc, backoff)
                for w in self.watch.values():       # the feed is gone: nothing can be priced
                    w.pending.clear()
                self.watch = {m: w for m, w in self.watch.items() if w.held}
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    _ids = itertools.count(1)

    async def _read_new(self, pp, hl) -> None:
        self._req: dict[int, str] = {}       # request id -> mint (until Helius answers with a subscription id)
        self._sub: dict[int, str] = {}       # subscription id -> mint
        self._mint_sub: dict[str, int] = {}
        async for raw in pp:
            msg = json.loads(raw)
            if not isinstance(msg, dict):
                continue
            for mint in self.on_message(msg, time.monotonic()):
                w = self.watch.get(mint)
                if w and w.curve:
                    rid = next(self._ids)
                    self._req[rid] = mint
                    await hl.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": "accountSubscribe",
                                              "params": [w.curve, {"encoding": "base64", "commitment": "processed"}]}))

    async def _read_prices(self, hl) -> None:
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

    async def _housekeep(self, hl, SessionLocal) -> None:
        last_flush = last_px = time.monotonic()
        while True:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            for mint in self.tick(now):
                sid = self._mint_sub.pop(mint, None)
                if sid is not None:
                    self._sub.pop(sid, None)
                    await hl.send(json.dumps({"jsonrpc": "2.0", "id": next(self._ids), "method": "accountUnsubscribe",
                                              "params": [sid]}))
            if now - last_flush >= 5.0 and (self.opened or self.closed):
                last_flush = now
                async with SessionLocal() as session:
                    await self.flush(session)
            if now - last_px >= 60.0:
                last_px = now
                await self._refresh_sol_usd()

    async def _refresh_sol_usd(self) -> None:
        import httpx

        from app.live import SOL_MINT

        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{self.s.jupiter_api_url}/price/v3", params={"ids": SOL_MINT})
                self.sol_usd = float(r.json()[SOL_MINT]["usdPrice"])
        except Exception:
            log.warning("sniper (paper): SOL price unavailable")
