"""Live prices of pump.fun / PumpSwap coins from PumpPortal's trade stream.

Every trade on a subscribed coin arrives within about a second of its block, with the bonding curve's reserves
(SOL per token = vSolInBondingCurve / vTokensInBondingCurve) or, on PumpSwap, the trade's own SOL and token
amounts. That is fresher than Jupiter's price API and costs no Jupiter call (the Jupiter queue is shared with
swaps).

Demand-driven, because PumpPortal bills every event (0.01 SOL per 10,000): the copy engine asks for prices of
the positions it is checking (take-capital, quick exit, owner targets) through JupiterExecutor.token_prices*,
and only coins asked for in the last LIVE_PUMP_PRICES_KEEP_SECONDS stay subscribed, at most
LIVE_PUMP_PRICES_MAX_COINS. Anything without a fresh PumpPortal price falls back to Jupiter / DexScreener.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from app.config import Settings, get_settings

log = logging.getLogger("pump_prices")

PUMP: "PumpPrices | None" = None     # set by the worker when LIVE_PUMP_PRICES is on and a key exists


def is_pump_coin(mint: str) -> bool:
    """pump.fun mints end in "pump" (vanity address); only those are streamed and priced here."""
    return mint.endswith("pump")


def price_from(msg: dict[str, Any]) -> float | None:
    """SOL per whole token from one PumpPortal trade message."""
    try:
        v_sol, v_tok = float(msg.get("vSolInBondingCurve") or 0), float(msg.get("vTokensInBondingCurve") or 0)
        if v_sol > 0 and v_tok > 0:
            return v_sol / v_tok                      # bonding curve: the price after this trade
        sol, tok = float(msg.get("solAmount") or 0), float(msg.get("tokenAmount") or 0)
        if sol > 0 and tok > 0:
            return sol / tok                          # PumpSwap: this trade's own price
        mc = float(msg.get("marketCapSol") or 0)
        if mc > 0:
            return mc / 1_000_000_000                 # pump.fun supply is 1 billion tokens
    except (TypeError, ValueError):
        return None
    return None


class PumpPrices:
    def __init__(self, settings: Settings | None = None) -> None:
        self.s = settings or get_settings()
        self._px: dict[str, tuple[float, float]] = {}      # mint -> (SOL per token, monotonic time)
        self._asked: dict[str, float] = {}                 # mint -> last time the engine asked for it
        self._subbed: set[str] = set()
        self.events = 0                                    # billed events received (for the cost log)
        self._events_logged = 0
        self._log_at = time.monotonic()

    # ---- used by the copy engine (through JupiterExecutor) -----------------------------------------------
    def request(self, mints) -> None:
        now = time.monotonic()
        for m in mints:
            if is_pump_coin(m):
                self._asked[m] = now

    def price_sol(self, mint: str) -> float | None:
        hit = self._px.get(mint)
        if hit and time.monotonic() - hit[1] <= self.s.live_pump_prices_max_age_seconds:
            return hit[0]
        return None

    # ---- stream ------------------------------------------------------------------------------------------
    def wanted(self) -> set[str]:
        now = time.monotonic()
        keep = self.s.live_pump_prices_keep_seconds
        self._asked = {m: t for m, t in self._asked.items() if now - t <= keep}
        newest = sorted(self._asked, key=self._asked.get, reverse=True)[: self.s.live_pump_prices_max_coins]
        return set(newest)

    def sync_messages(self) -> list[str]:
        """Subscribe / unsubscribe messages that bring the stream in line with what is wanted now."""
        want = self.wanted()
        add, drop = sorted(want - self._subbed), sorted(self._subbed - want)
        out = []
        if add:
            out.append(json.dumps({"method": "subscribeTokenTrade", "keys": add}))
        if drop:
            out.append(json.dumps({"method": "unsubscribeTokenTrade", "keys": drop}))
            for m in drop:
                self._px.pop(m, None)
        self._subbed = want
        return out

    def on_message(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        if msg.get("errors") or (msg.get("message") and "subscribed" not in str(msg.get("message")).lower()):
            log.warning("PumpPortal prices: %s", str(msg)[:200])
            return
        mint = msg.get("mint")
        if not mint or mint not in self._subbed or msg.get("txType") not in ("buy", "sell"):
            return
        self.events += 1
        px = price_from(msg)
        if px:
            self._px[mint] = (px, time.monotonic())

    def _log_cost(self) -> None:
        if time.monotonic() - self._log_at < 600:
            return
        n = self.events - self._events_logged
        self._events_logged, self._log_at = self.events, time.monotonic()
        # 0.01 SOL per 10,000 events; 144 ten-minute windows a day
        log.info("PumpPortal prices: %d events in 10 min on %d coins (~%.3f SOL/day at this rate)",
                 n, len(self._subbed), n * 144 * 0.01 / 10_000)

    async def run_forever(self) -> None:
        import websockets

        url, delay = f"wss://pumpportal.fun/api/data?api-key={self.s.pumpportal_api_key}", 1.0
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2**22,
                                              open_timeout=15) as ws:
                    self._subbed = set()                 # a new connection starts with no subscriptions
                    log.info("PumpPortal prices connected")
                    delay = 1.0
                    while True:
                        for m in self.sync_messages():
                            await ws.send(m)
                        self._log_cost()
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue
                        self.on_message(raw)
                        # drain whatever else is queued before the next sync
                        while True:
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=0.001)
                            except asyncio.TimeoutError:
                                break
                            self.on_message(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("PumpPortal prices dropped (%s: %s); reconnecting in %.0fs",
                            type(exc).__name__, str(exc)[:120], delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)
