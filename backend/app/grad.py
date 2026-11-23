"""Paper "graduation" trades: pump.fun tokens bought in the first minutes after they move to PumpSwap.
Simulated only (no keys, no transactions, no money).

Why: the speed wallets (5hqZDJ / 5Evb9R, analysed 2026-11-21) made most of their profit on tokens bought
within ~5 minutes of graduation, cutting losers fast and selling winners in steps around +20% / +50%.

Feeds: PumpPortal's free WebSocket `subscribeMigration` sends {signature, mint, txType "migrate"} when a
token leaves the bonding curve. The new PumpSwap pool is found in that transaction (an account of program
pAMMBay6... whose base mint is the token and quote mint is wrapped SOL). Prices come from Helius: an
accountSubscribe on the pool's two vaults; price = SOL in the quote vault / tokens in the base vault.

Strategies (side by side on /paper):
  grad_all      every migrated token, bought GRAD_LATENCY_S after the migration event
  grad_confirm  only if, GRAD_CONFIRM_S after the migration, the price is >= GRAD_CONFIRM_MIN_PCT above the
                first price we saw; bought GRAD_LATENCY_S after that check
Exits: sell GRAD_TP1_FRAC at +GRAD_TP1_PCT, the rest at +GRAD_TP2_PCT; stop loss GRAD_SL_PCT on what is
left; anything still open after GRAD_MAX_HOLD_S is sold. Fee + slippage on every fill.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.config import Settings, get_settings
from app.models import Chain, PaperPosition

log = logging.getLogger("grad")

FEED_URL = "wss://pumpportal.fun/api/data"
PUMPSWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
WSOL = "So11111111111111111111111111111111111111112"
GRAD_STRATEGIES = ("grad_all", "grad_confirm")
POOL_LEN = 203                     # bytes up to the end of the quote vault


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _key(raw: bytes, off: int) -> str:
    from solders.pubkey import Pubkey

    return str(Pubkey.from_bytes(raw[off:off + 32]))


def decode_pool(data_b64: str) -> dict[str, str] | None:
    """PumpSwap pool account: 8 discriminator, u8 bump, u16 index, creator, then base mint @43, quote mint @75,
    LP mint @107, base vault @139, quote vault @171 (checked on a live pool 2026-11-21)."""
    try:
        raw = base64.b64decode(data_b64)
    except Exception:
        return None
    if len(raw) < POOL_LEN:
        return None
    return {"base_mint": _key(raw, 43), "quote_mint": _key(raw, 75),
            "base_vault": _key(raw, 139), "quote_vault": _key(raw, 171)}


def find_pool(accounts: list[tuple[str, dict[str, Any] | None]], mint: str) -> dict[str, str] | None:
    """Among a migration transaction's accounts ((address, getMultipleAccounts value)), the SOL pool of `mint`."""
    for addr, v in accounts:
        if not v or v.get("owner") != PUMPSWAP:
            continue
        p = decode_pool((v.get("data") or [""])[0])
        if p and p["base_mint"] == mint and p["quote_mint"] == WSOL:
            return {"pool": addr, **p}
    return None


@dataclass
class Watch:
    mint: str
    created: float                         # monotonic time of the migration event
    symbol: str = ""
    base: float | None = None              # tokens in the base vault
    quote: float | None = None             # SOL in the quote vault
    base_slot: int | None = None
    quote_slot: int | None = None
    price: float | None = None
    first_price: float | None = None
    pending: dict[str, tuple[float, str]] = field(default_factory=dict)   # strategy -> (fill at, why)
    held: dict[str, dict[str, Any]] = field(default_factory=dict)
    decided: set[str] = field(default_factory=set)


class PaperGrad:
    def __init__(self, settings: Settings | None = None, sol_usd: float = 0.0) -> None:
        self.s = settings or get_settings()
        self.sol_usd = sol_usd
        self.watch: dict[str, Watch] = {}
        self.opened: list[dict[str, Any]] = []
        self.closed: list[dict[str, Any]] = []
        self.partial: list[dict[str, Any]] = []      # TP1 fills to write

    # ---- events -------------------------------------------------------------
    def on_migration(self, mint: str, now: float) -> bool:
        if not mint or mint in self.watch:
            return False
        w = Watch(mint, now)
        w.pending["grad_all"] = (now + self.s.grad_latency_s, "migrated")
        w.decided.add("grad_all")
        self.watch[mint] = w
        return True

    def on_vault(self, mint: str, side: str, amount: float | None, now: float, slot: int | None = None) -> None:
        """A vault balance. Every swap moves both vaults, and their notifications come one by one: the price is
        only taken when both balances are from the same slot (else a half-updated pair fakes a jump)."""
        w = self.watch.get(mint)
        if w is None or amount is None:
            return
        if side == "base":
            w.base, w.base_slot = float(amount), slot
        else:
            w.quote, w.quote_slot = float(amount), slot
        same = w.base_slot is None or w.quote_slot is None or w.base_slot == w.quote_slot
        if w.base and w.quote and same:
            w.price = w.quote / w.base
            if w.first_price is None:
                w.first_price = w.price
        self._step(w, now)

    def tick(self, now: float) -> list[str]:
        """Every second: decisions, fills, exits; returns mints we stopped watching (to unsubscribe)."""
        drop = []
        for mint, w in list(self.watch.items()):
            self._step(w, now)
            age = now - w.created
            done = not w.pending and not w.held and "grad_confirm" in w.decided
            no_price = w.price is None and age > self.s.grad_confirm_s + 60
            if (done and age > self.s.grad_confirm_s + self.s.grad_latency_s + 5) or (no_price and not w.held):
                drop.append(mint)
                del self.watch[mint]
        return drop

    def _step(self, w: Watch, now: float) -> None:
        self._confirm(w, now)
        self._fills(w, now)
        self._exits(w, now)

    # ---- rules ----------------------------------------------------------------
    def _cost(self) -> float:
        return (self.s.grad_fee_pct + self.s.grad_slippage_pct) / 100

    def _confirm(self, w: Watch, now: float) -> None:
        if "grad_confirm" in w.decided or now - w.created < self.s.grad_confirm_s:
            return
        w.decided.add("grad_confirm")
        if not w.price or not w.first_price:
            return
        up = (w.price / w.first_price - 1) * 100
        if up >= self.s.grad_confirm_min_pct:
            w.pending["grad_confirm"] = (now + self.s.grad_latency_s,
                                         f"up {up:+.0f}% {self.s.grad_confirm_s:g}s after migration")

    def _fills(self, w: Watch, now: float) -> None:
        for strat, (at, why) in list(w.pending.items()):
            if now < at or not w.price:
                continue
            del w.pending[strat]
            if strat in w.held or sum(len(x.held) for x in self.watch.values()) >= self.s.grad_max_open:
                continue
            from app import paper_switches

            if paper_switches.paused(strat):              # paused on /paper
                continue
            fill = w.price * (1 + self._cost())
            pos = {"strategy": strat, "mint": w.mint, "symbol": w.symbol, "market": w.price, "fill": fill,
                   "at": now, "wall": _now(), "peak": 0.0, "trough": 0.0, "frac": 1.0, "proceeds": 0.0,
                   "tp1": False,
                   "why": f"{why} +{self.s.grad_latency_s:g}s, {(now - w.created):.0f}s after migration, "
                          f"{((w.price / w.first_price) - 1) * 100 if w.first_price else 0:+.0f}% vs first price"}
            w.held[strat] = pos
            self.opened.append(pos)

    def _sell(self, pos: dict[str, Any], frac: float, price: float) -> None:
        size = self.s.grad_size_usd
        pos["proceeds"] += size * frac / pos["fill"] * price * (1 - self._cost())
        pos["frac"] = max(pos["frac"] - frac, 0.0)

    def _exits(self, w: Watch, now: float) -> None:
        if not w.price:
            return
        for strat, pos in list(w.held.items()):
            pct = (w.price / pos["market"] - 1) * 100
            pos["peak"], pos["trough"] = max(pos["peak"], pct), min(pos["trough"], pct)
            if not pos["tp1"] and pct >= self.s.grad_tp1_pct:
                self._sell(pos, min(self.s.grad_tp1_frac, pos["frac"]), w.price)
                pos["tp1"] = True
                self.partial.append(pos)
            reason = None
            if pct >= self.s.grad_tp2_pct:
                reason = "take_profit"
            elif pct <= self.s.grad_sl_pct:
                reason = "stop_loss"
            elif now - pos["at"] >= self.s.grad_max_hold_s:
                reason = "time"
            if reason or pos["frac"] <= 1e-9:
                if pos["frac"] > 0:
                    self._sell(pos, pos["frac"], w.price)
                size = self.s.grad_size_usd
                pnl = pos["proceeds"] - size
                pos.update(exit=w.price, reason=reason or "take_profit", pnl=pnl, pnl_pct=pnl / size * 100,
                           exit_wall=_now())
                self.closed.append(pos)
                del w.held[strat]

    # ---- persistence ----------------------------------------------------------
    def _usd(self, sol_px: float) -> float:
        return sol_px * self.sol_usd if self.sol_usd else sol_px

    async def flush(self, session) -> int:
        from app import paper_switches

        await paper_switches.refresh(session)
        """Write opened / partly sold / closed rows (strategy grad_*) so /paper shows them. Each flush may use a
        new session: rows are found again by id."""
        n = 0
        rows: list[tuple[dict[str, Any], PaperPosition]] = []
        for pos in self.opened:
            row = PaperPosition(strategy=pos["strategy"], chain=Chain.solana, token_address=pos["mint"],
                                symbol=pos["symbol"] or pos["mint"][:6], trigger=pos["why"][:160], status="open",
                                size_usd=self.s.grad_size_usd, entry_time=pos["wall"],
                                entry_price=self._usd(pos["market"]), fill_price=self._usd(pos["fill"]),
                                tp_pct=self.s.grad_tp2_pct, sl_pct=self.s.grad_sl_pct,
                                max_hold_minutes=max(int(self.s.grad_max_hold_s // 60), 1),
                                remaining_frac=1.0, proceeds_usd=0.0)
            session.add(row)
            rows.append((pos, row))
            n += 1
        await session.flush()
        for pos, row in rows:
            pos["row"] = row.id
        self.opened.clear()
        for c in self.closed:
            row = await session.get(PaperPosition, c["row"]) if c.get("row") else None
            if row is None:
                continue
            row.status, row.exit_time, row.exit_reason = "closed", c["exit_wall"], c["reason"]
            row.exit_price = row.last_price = self._usd(c["exit"])
            row.peak_pct, row.trough_pct = c["peak"], c["trough"]
            row.pnl_usd, row.pnl_pct = c["pnl"], c["pnl_pct"]
            row.proceeds_usd, row.remaining_frac = c["proceeds"], 0.0
            n += 1
        self.closed = [c for c in self.closed if c.get("row") is None]
        self.partial = [p for p in self.partial if p.get("row") is None]
        n += await self._mark_open(session)
        await session.commit()
        return n

    async def _mark_open(self, session) -> int:
        """Open rows: current price, what is sold so far, and net PnL if the rest were sold now."""
        size, n, stamp = self.s.grad_size_usd, 0, _now()
        for w in self.watch.values():
            if not w.price:
                continue
            for pos in w.held.values():
                row = await session.get(PaperPosition, pos["row"]) if pos.get("row") else None
                if row is None:
                    continue
                value = pos["proceeds"] + size * pos["frac"] / pos["fill"] * w.price * (1 - self._cost())
                row.last_price, row.last_priced_at = self._usd(w.price), stamp
                row.peak_pct, row.trough_pct = pos["peak"], pos["trough"]
                row.remaining_frac, row.proceeds_usd = pos["frac"], pos["proceeds"]
                row.pnl_usd = value - size
                row.pnl_pct = row.pnl_usd / size * 100
                n += 1
        return n

    # ---- loop -------------------------------------------------------------------
    def _helius_ws(self) -> str | None:
        url = self.s.live_rpc_url or ""
        return url.replace("https://", "wss://", 1) if url.startswith("https://") and "helius" in url else None

    async def run_forever(self) -> None:
        if not self.s.grad_paper_enabled:
            return
        hws = self._helius_ws()
        if not hws:
            log.warning("grad (paper): needs a Helius LIVE_RPC_URL for prices; not started")
            return
        import websockets

        from app.db import SessionLocal

        async with SessionLocal() as session:          # a restart left these with no feed: close them as they are
            stale = (await session.execute(select(PaperPosition).where(
                PaperPosition.strategy.in_(GRAD_STRATEGIES), PaperPosition.status == "open"))).scalars().all()
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
                    await pp.send(json.dumps({"method": "subscribeMigration"}))
                    log.info("grad (paper): connected (migrations: PumpPortal, prices: Helius)")
                    backoff = 2.0
                    self._req: dict[int, tuple[str, str]] = {}     # request id -> (mint, side)
                    self._sub: dict[int, tuple[str, str]] = {}     # subscription id -> (mint, side)
                    self._mint_subs: dict[str, list[int]] = {}
                    await asyncio.gather(self._read_migrations(pp, hl), self._read_prices(hl),
                                         self._housekeep(hl, SessionLocal))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("grad (paper): feed error %s; reconnecting in %.0fs", exc, backoff)
                for w in self.watch.values():
                    w.pending.clear()
                self.watch = {m: w for m, w in self.watch.items() if w.held}
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    _ids = itertools.count(1)

    async def _read_migrations(self, pp, hl) -> None:
        async for raw in pp:
            msg = json.loads(raw)
            if not isinstance(msg, dict) or msg.get("txType") != "migrate" or msg.get("pool") not in (None, "pump-amm"):
                continue
            mint, sig = str(msg.get("mint") or ""), str(msg.get("signature") or "")
            if mint and sig and self.on_migration(mint, time.monotonic()):
                asyncio.create_task(self._attach_pool(mint, sig, hl))

    async def _rpc(self, c, method: str, params: list) -> Any:
        for _ in range(4):
            try:
                r = await c.post(self.s.live_rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                                            "params": params})
                if r.status_code == 200 and r.content:
                    return r.json().get("result")
            except Exception:
                pass
            await asyncio.sleep(1.5)
        return None

    async def _attach_pool(self, mint: str, sig: str, hl) -> None:
        """Find the new pool in the migration transaction, then watch its two vaults."""
        import httpx

        try:
            async with httpx.AsyncClient(timeout=20) as c:
                tx = None
                for _ in range(8):                     # the tx can take a moment to be served
                    tx = await self._rpc(c, "getTransaction", [sig, {"maxSupportedTransactionVersion": 0,
                                                                    "encoding": "jsonParsed",
                                                                    "commitment": "confirmed"}])
                    if tx:
                        break
                    await asyncio.sleep(1.0)
                if not tx:
                    return
                keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
                accts: list[tuple[str, Any]] = []
                for i in range(0, len(keys), 100):
                    res = await self._rpc(c, "getMultipleAccounts", [keys[i:i + 100], {"encoding": "base64"}])
                    accts += list(zip(keys[i:i + 100], (res or {}).get("value") or []))
                pool = find_pool(accts, mint)
        except Exception as exc:
            log.debug("grad (paper): pool lookup %s failed: %s", mint[:8], exc)
            return
        if not pool or mint not in self.watch:
            return
        for side, vault in (("base", pool["base_vault"]), ("quote", pool["quote_vault"])):
            rid = next(self._ids)
            self._req[rid] = (mint, side)
            await hl.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": "accountSubscribe",
                                      "params": [vault, {"encoding": "jsonParsed", "commitment": "processed"}]}))
        async with httpx.AsyncClient(timeout=20) as c:       # starting balances (the vaults may stay quiet)
            for side, vault in (("base", pool["base_vault"]), ("quote", pool["quote_vault"])):
                b = await self._rpc(c, "getTokenAccountBalance", [vault])
                w = self.watch.get(mint)
                if w is not None and (w.base_slot if side == "base" else w.quote_slot) is not None:
                    continue                           # a live update already came in: it is newer
                self.on_vault(mint, side, ((b or {}).get("value") or {}).get("uiAmount"), time.monotonic())

    async def _read_prices(self, hl) -> None:
        async for raw in hl:
            msg = json.loads(raw)
            if "id" in msg and "result" in msg:
                key = self._req.pop(msg["id"], None)
                if key and isinstance(msg["result"], int):
                    self._sub[msg["result"]] = key
                    self._mint_subs.setdefault(key[0], []).append(msg["result"])
                continue
            p = msg.get("params") or {}
            key = self._sub.get(p.get("subscription"))
            if not key:
                continue
            data = ((p.get("result") or {}).get("value") or {}).get("data") or {}
            info = ((data.get("parsed") or {}).get("info") or {}) if isinstance(data, dict) else {}
            amount = (info.get("tokenAmount") or {}).get("uiAmount")
            slot = (((p.get("result") or {}).get("context")) or {}).get("slot")
            self.on_vault(key[0], key[1], amount, time.monotonic(), slot)

    async def _housekeep(self, hl, SessionLocal) -> None:
        last_flush = last_px = time.monotonic()
        while True:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            for mint in self.tick(now):
                for sid in self._mint_subs.pop(mint, []):
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

    async def _refresh_sol_usd(self) -> None:
        import httpx

        from app.live import SOL_MINT

        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{self.s.jupiter_api_url}/price/v3", params={"ids": SOL_MINT})
                self.sol_usd = float(r.json()[SOL_MINT]["usdPrice"])
        except Exception:
            log.warning("grad (paper): SOL price unavailable")
