"""Complete live prices of pump.fun / PumpSwap coins, read from the chain over Helius's WebSocket.

PumpPortal's trade stream turned out to miss most trades (2026-11-23: 1 of 7 on a busy PumpSwap pool), so its
price could be minutes stale. This feed watches the accounts that ARE the price instead:

  * a coin still on pump.fun: its bonding-curve account (PDA "bonding-curve" + mint). SOL per token =
    virtual SOL / virtual tokens, the exact formula pump.fun trades at (decoded by app.sniper.curve_state).
  * a coin that moved to PumpSwap: its canonical pool (derived from the mint, the pool a pump.fun migration
    creates) and the pool's two vaults. SOL per token = (SOL vault + the pool's virtual_quote_reserves) / token
    vault. The virtual reserve (i128 at offset 245 of the pool account, ~17.6 SOL on a migrated coin) is part of
    what the pool prices with (Chainstack's pump.fun bot, docs/pumpfun-protocol.md); without it the vault ratio
    read 49-93 % below Jupiter's price on real pools, with it +0.9..1.8 % (about the pool fee), 2026-11-23.

Helius pushes every change of those accounts (accountSubscribe), so the price is current after every trade and
needs no "age" check: a quiet coin simply has not moved. Demand-driven like the old PumpPortal feed: the copy
engine asks for the coins it is checking (take-capital, quick exit, owner targets) through
JupiterExecutor.token_prices*, and coins nobody asked for in LIVE_POOL_PRICES_KEEP_SECONDS are dropped.
Anything not watched here falls back to Jupiter / DexScreener.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import Settings, get_settings

log = logging.getLogger("pool_prices")

POOL: "PoolPrices | None" = None      # set by the worker when LIVE_POOL_PRICES is on
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
# PumpSwap Pool account, after the 8-byte discriminator: pool_bump u8, index u16, creator, base_mint, quote_mint,
# lp_mint, base vault, quote vault, lp_supply u64, coin_creator, is_mayhem_mode, is_cashback_coin,
# virtual_quote_reserves i128
POOL_BASE_VAULT, POOL_QUOTE_VAULT, POOL_VIRTUAL_QUOTE = 139, 171, 245
WSOL = "So11111111111111111111111111111111111111112"
RETRY_UNRESOLVED_SECONDS = 300.0


def curve_address(mint: str) -> str:
    from solders.pubkey import Pubkey

    pda, _ = Pubkey.find_program_address([b"bonding-curve", bytes(Pubkey.from_string(mint))],
                                         Pubkey.from_string(PUMP_PROGRAM))
    return str(pda)


def pool_address(mint: str) -> str:
    """The canonical PumpSwap pool of a pump.fun coin paired with SOL: index 0, owned by the curve's authority."""
    import struct

    from solders.pubkey import Pubkey

    m = Pubkey.from_string(mint)
    auth = Pubkey.find_program_address([b"pool-authority", bytes(m)], Pubkey.from_string(PUMP_PROGRAM))[0]
    return str(Pubkey.find_program_address(
        [b"pool", struct.pack("<H", 0), bytes(auth), bytes(m), bytes(Pubkey.from_string(WSOL))],
        Pubkey.from_string(AMM_PROGRAM))[0])


def parse_pool(data_b64: str) -> tuple[str, str, float] | None:
    """PumpSwap pool account -> (token vault, SOL vault, virtual quote reserves in SOL)."""
    import base64

    from solders.pubkey import Pubkey

    try:
        raw = base64.b64decode(data_b64)
        if len(raw) < POOL_VIRTUAL_QUOTE + 16:
            return None
        base = str(Pubkey.from_bytes(raw[POOL_BASE_VAULT:POOL_BASE_VAULT + 32]))
        quote = str(Pubkey.from_bytes(raw[POOL_QUOTE_VAULT:POOL_QUOTE_VAULT + 32]))
        vq = int.from_bytes(raw[POOL_VIRTUAL_QUOTE:POOL_VIRTUAL_QUOTE + 16], "little", signed=True)
    except Exception:
        return None
    return base, quote, vq / 1e9


def _ui(acc_value: dict[str, Any] | None) -> float | None:
    """Token amount of a jsonParsed token account (vault)."""
    try:
        ta = acc_value["data"]["parsed"]["info"]["tokenAmount"]
        return float(ta.get("uiAmountString") or ta.get("uiAmount") or 0)
    except Exception:
        return None


@dataclass
class Book:
    mint: str
    kind: str                                   # "curve" | "amm"
    accounts: list[str]                         # curve: [curve]; amm: [token vault, SOL vault, pool]
    values: dict[str, float] = field(default_factory=dict)
    subs: dict[int, str] = field(default_factory=dict)    # Helius subscription id -> account
    price: float | None = None

    def reprice(self) -> None:
        v = self.values
        if self.kind == "curve" and v.get("vtok"):
            self.price = v["vsol"] / v["vtok"]
        elif self.kind == "amm" and v.get("base"):
            quote = v.get("quote", 0.0) + v.get("vq", 0.0)
            self.price = quote / v["base"] if quote > 0 else None


class PoolPrices:
    def __init__(self, settings: Settings | None = None) -> None:
        self.s = settings or get_settings()
        self.rpc = self.s.live_rpc_url or self.s.solana_rpc_url
        self._asked: dict[str, float] = {}
        self.books: dict[str, Book] = {}
        self._unresolved: dict[str, float] = {}
        self._pending: dict[int, tuple[str, str]] = {}   # request id -> (mint, account)
        self._ids = 0
        self.connected = False
        self.notifications = 0
        self._noted, self._log_at = 0, time.monotonic()

    # ---- used by the copy engine (through JupiterExecutor) -------------------------------------------
    def request(self, mints) -> None:
        now = time.monotonic()
        for m in mints:
            if m.endswith("pump"):
                self._asked[m] = now

    def price_sol(self, mint: str) -> float | None:
        """SOL per whole token, only while the account subscription is live (else the caller uses Jupiter)."""
        b = self.books.get(mint)
        if not self.connected or b is None or not b.subs or len(b.subs) < len(b.accounts):
            return None
        return b.price

    # ---- resolving a coin to the accounts that hold its price -----------------------------------------
    async def _call(self, http: httpx.AsyncClient, method: str, params: list[Any]) -> Any:
        r = await http.post(self.rpc, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        r.raise_for_status()
        return r.json().get("result")

    async def resolve(self, http: httpx.AsyncClient, mint: str) -> Book | None:
        from app.sniper import curve_state

        curve = curve_address(mint)
        res = await self._call(http, "getAccountInfo", [curve, {"encoding": "base64", "commitment": "processed"}])
        data = ((res or {}).get("value") or {}).get("data")
        st = curve_state(data[0]) if data else None
        if st is not None:                                   # still on the bonding curve
            b = Book(mint, "curve", [curve], {"vsol": st["vSolInBondingCurve"], "vtok": st["vTokensInBondingCurve"]})
            b.reprice()
            return b
        # moved to PumpSwap: its canonical pool (no search), the vaults it names and its virtual quote reserve
        if not self.s.live_pool_prices_amm:
            return None
        pool = pool_address(mint)
        res = await self._call(http, "getAccountInfo", [pool, {"encoding": "base64", "commitment": "processed"}])
        val = (res or {}).get("value") or {}
        parsed = parse_pool(val["data"][0]) if val.get("owner") == AMM_PROGRAM and val.get("data") else None
        if parsed is None:
            return None
        base_vault, quote_vault, vq = parsed
        res = await self._call(http, "getMultipleAccounts", [[base_vault, quote_vault],
                                                            {"encoding": "jsonParsed", "commitment": "processed"}])
        vals = (res or {}).get("value") or []
        if len(vals) != 2 or not all(vals):
            return None
        b = Book(mint, "amm", [base_vault, quote_vault, pool],
                 {"base": _ui(vals[0]) or 0.0, "quote": _ui(vals[1]) or 0.0, "vq": vq})
        b.reprice()
        return b

    # ---- notifications --------------------------------------------------------------------------------
    def on_message(self, raw: str | bytes) -> str | None:
        """Apply one Helius message. Returns a mint that must be resolved again (a curve that just completed)."""
        from app.sniper import curve_state

        try:
            msg = json.loads(raw)
        except ValueError:
            return None
        if "id" in msg and msg.get("id") in self._pending:            # subscription confirmed
            mint, acct = self._pending.pop(msg["id"])
            b = self.books.get(mint)
            if b is not None and isinstance(msg.get("result"), int):
                b.subs[msg["result"]] = acct
            return None
        if msg.get("method") != "accountNotification":
            return None
        params = msg.get("params") or {}
        sub, value = params.get("subscription"), ((params.get("result") or {}).get("value")) or {}
        for b in self.books.values():
            acct = b.subs.get(sub)
            if acct is None:
                continue
            self.notifications += 1
            if b.kind == "curve":
                data = value.get("data")
                st = curve_state(data[0]) if isinstance(data, list) and data else None
                if st is None:                                    # complete: the coin is moving to PumpSwap
                    return b.mint
                b.values.update(vsol=st["vSolInBondingCurve"], vtok=st["vTokensInBondingCurve"])
            elif len(b.accounts) > 2 and acct == b.accounts[2]:          # the pool: its virtual reserve
                data = value.get("data")
                parsed = parse_pool(data[0]) if isinstance(data, list) and data else None
                if parsed is not None:
                    b.values["vq"] = parsed[2]
            else:
                amt = _ui(value)
                if amt is not None:
                    b.values["base" if acct == b.accounts[0] else "quote"] = amt
            b.reprice()
            return None
        return None

    def _log(self) -> None:
        if time.monotonic() - self._log_at < 600:
            return
        n = self.notifications - self._noted
        self._noted, self._log_at = self.notifications, time.monotonic()
        log.info("pool prices: %d updates in 10 min, %d coins watched", n, len(self.books))

    # ---- loop -----------------------------------------------------------------------------------------
    def _wanted(self) -> set[str]:
        now = time.monotonic()
        self._asked = {m: t for m, t in self._asked.items() if now - t <= self.s.live_pool_prices_keep_seconds}
        return set(sorted(self._asked, key=self._asked.get, reverse=True)[: self.s.live_pool_prices_max_coins])

    async def _sync(self, ws, http: httpx.AsyncClient) -> None:
        want = self._wanted()
        for mint in [m for m in self.books if m not in want]:
            b = self.books.pop(mint)
            for sub in b.subs:
                self._ids += 1
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": self._ids, "method": "accountUnsubscribe",
                                          "params": [sub]}))
        now = time.monotonic()
        for mint in sorted(want - set(self.books)):
            if now - self._unresolved.get(mint, -1e9) < RETRY_UNRESOLVED_SECONDS:
                continue
            try:
                b = await asyncio.wait_for(self.resolve(http, mint), timeout=8)
            except Exception as exc:
                log.info("pool prices: can't resolve %s (%s)", mint[:8], str(exc)[:80])
                b = None
            if b is None:
                self._unresolved[mint] = now
                continue
            self.books[mint] = b
            log.info("pool prices: watching %s (%s, %.3e SOL)", mint[:8], b.kind, b.price or 0)
            for acct in b.accounts:
                self._ids += 1
                self._pending[self._ids] = (mint, acct)
                enc = "base64" if b.kind == "curve" or (b.kind == "amm" and acct == b.accounts[-1]) else "jsonParsed"
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": self._ids, "method": "accountSubscribe",
                                          "params": [acct, {"encoding": enc, "commitment": "processed"}]}))

    async def run_forever(self) -> None:
        import websockets

        from app.fast_watch import ws_url

        url, delay = ws_url(self.rpc), 1.0
        async with httpx.AsyncClient(timeout=10) as http:
            while True:
                try:
                    async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2**22,
                                                  open_timeout=15) as ws:
                        self.books.clear()               # a new connection has no subscriptions
                        self._pending.clear()
                        self.connected = True
                        log.info("pool prices connected (Helius)")
                        delay = 1.0
                        while True:
                            await self._sync(ws, http)
                            self._log()
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                            except asyncio.TimeoutError:
                                continue
                            redo = self.on_message(raw)
                            if redo:                     # curve completed: drop it, the next sync resolves it again
                                b = self.books.pop(redo, None)
                                for sub in (b.subs if b else {}):
                                    self._ids += 1
                                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": self._ids,
                                                              "method": "accountUnsubscribe", "params": [sub]}))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("pool prices dropped (%s: %s); reconnecting in %.0fs",
                                type(exc).__name__, str(exc)[:120], delay)
                finally:
                    self.connected = False
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
