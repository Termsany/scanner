"""Real-time detection of the copied wallets' trades over a Solana WebSocket (Helius).

The wallet tracker polls every TRACKER_INTERVAL_SECONDS (public RPC), so a copied buy was seen 15-30 s late.
Here each copied wallet gets a `logsSubscribe` ("mentions" the wallet) on LIVE_RPC_URL's WebSocket: the node
pushes every confirmed transaction that touches the wallet within about a second. The transaction is fetched,
parsed and recorded through the tracker's normal pipeline (same pricing, filters and dedupe), and the copy
engines are woken at once instead of on their next tick.

The poller keeps running as the backup: anything missed while the socket is down is still picked up there, and
a trade recorded by both is stored once (unique chain + tx + token).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.config import Settings, get_settings
from app.models import Chain, SmartWallet, WalletTrade
from app.wallet_tracker import WalletTracker, solana_trades_from_tx

log = logging.getLogger("fast_watch")

# Copy engines register an Event here; a new copied trade sets them all (see CopyEngine loops in app.live).
WAKE: list[asyncio.Event] = []


def wake_engines() -> None:
    for ev in WAKE:
        ev.set()


def ws_url(http_url: str) -> str:
    if http_url.startswith("https://"):
        return "wss://" + http_url[len("https://"):]
    if http_url.startswith("http://"):
        return "ws://" + http_url[len("http://"):]
    return http_url


class FastWatcher:
    def __init__(self, wallets: list[str], settings: Settings | None = None,
                 fetch_tx: Callable[[str], Awaitable[dict[str, Any] | None]] | None = None,
                 tracker: WalletTracker | None = None, session_factory=None) -> None:
        self.s = settings or get_settings()
        self.wallets = sorted(set(wallets))
        self.rpc = self.s.live_rpc_url
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(15.0), headers={"User-Agent": "alpha-scanner/0.1"})
        self.tracker = tracker or WalletTracker(self.s)
        self._fetch_tx = fetch_tx or self._get_transaction
        self._session_factory = session_factory
        self._subs: dict[int, str] = {}      # subscription id -> wallet
        self._pending: dict[int, str] = {}   # request id -> wallet
        self._seen: dict[str, float] = {}    # signature -> time handled (drop duplicates from overlapping subs)
        # Notifications are handled in parallel across wallets (a busy wallet must not delay another's buy),
        # one at a time per wallet (its buy is recorded before its sell, as the engine expects).
        self._wallet_locks: dict[str, asyncio.Lock] = {}
        self._slots = asyncio.Semaphore(8)
        self._tasks: set[asyncio.Task] = set()
        self.handled = 0

    async def close(self) -> None:
        await self.http.aclose()
        await self.tracker.close()

    async def _get_transaction(self, sig: str) -> dict[str, Any] | None:
        """Notifications come at "processed"; getTransaction answers once confirmed (usually < 1 s later). Wait at
        most LIVE_FAST_FETCH_SECONDS: a transaction that never confirms must not hold up the wallet's next ones
        (2026-11-22: 33 s queued); the poller still picks up anything real that is missed here."""
        deadline = time.monotonic() + self.s.live_fast_fetch_seconds
        for attempt in range(40):
            if time.monotonic() > deadline:
                log.info("fast: %s not confirmed in %.0fs; leaving it to the poller", sig[:12], self.s.live_fast_fetch_seconds)
                return None
            r = await self.http.post(self.rpc, json={"jsonrpc": "2.0", "id": 1, "method": "getTransaction", "params": [
                sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}]})
            if r.status_code == 429:
                await asyncio.sleep(0.5)
                continue
            r.raise_for_status()
            body = r.json()
            if body.get("result"):
                return body["result"]
            if body.get("error"):
                log.warning("getTransaction %s: %s", sig[:12], body["error"])
                return None
            await asyncio.sleep(0.25)
        return None

    # ---- messages -------------------------------------------------------------
    def subscribe_requests(self) -> list[str]:
        reqs = []
        for i, w in enumerate(self.wallets, start=1):
            self._pending[i] = w
            reqs.append(json.dumps({"jsonrpc": "2.0", "id": i, "method": "logsSubscribe",
                                    "params": [{"mentions": [w]}, {"commitment": self.s.live_fast_commitment}]}))
        return reqs

    async def handle_message(self, raw: str | bytes) -> int:
        """Returns the number of trades recorded from this message."""
        msg = json.loads(raw)
        if "id" in msg and "result" in msg and msg["id"] in self._pending:   # subscription confirmed
            self._subs[int(msg["result"])] = self._pending.pop(msg["id"])
            return 0
        if msg.get("method") != "logsNotification":
            return 0
        params = msg.get("params") or {}
        wallet = self._subs.get(params.get("subscription"))
        value = ((params.get("result") or {}).get("value")) or {}
        sig = value.get("signature")
        if not wallet or not sig or value.get("err") is not None:
            return 0                                    # failed transactions move nothing
        return await self.handle_signature(wallet, sig, "helius")

    async def handle_signature(self, wallet: str, sig: str, source: str) -> int:
        """A copied wallet's transaction announced by either source; the first one to announce it records it."""
        now = time.monotonic()
        if sig in self._seen:
            return 0
        self._seen[sig] = now
        if len(self._seen) > 5_000:                     # keep the dedupe window small
            cutoff = now - 600
            self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
        lock = self._wallet_locks.setdefault(wallet, asyncio.Lock())
        async with lock, self._slots:
            return await self.process(wallet, sig, now, source)   # `now`: when the notification arrived

    def dispatch(self, raw: str | bytes) -> None:
        """Handle a notification in the background so the next one is read at once."""
        async def run() -> None:
            try:
                await self.handle_message(raw)
            except Exception:
                log.exception("fast: handling a notification failed")
        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def process(self, wallet: str, sig: str, notified: float | None = None, source: str = "helius") -> int:
        t0 = time.monotonic()
        queued = t0 - notified if notified else 0.0
        tx = await self._fetch_tx(sig)
        if not tx:
            return 0
        trades = solana_trades_from_tx(tx, wallet, sig)
        if not trades:
            return 0                                    # airdrop / transfer of SOL only / not signed by wallet
        factory = self._session_factory
        if factory is None:
            from app.db import SessionLocal as factory  # noqa: N813
        async with factory() as session:
            rows = (await session.execute(select(SmartWallet.address, SmartWallet.label).where(
                SmartWallet.chain == Chain.solana, SmartWallet.address == wallet))).all()
            labels = {(Chain.solana, a): (lbl or a[:6]) for a, lbl in rows} or {(Chain.solana, wallet): wallet[:6]}
            try:
                await self.tracker.record(session, trades, labels)
                await session.commit()
            except IntegrityError:
                await session.rollback()                # the poller stored it first: nothing to do
                return 0
            stored = (await session.execute(select(func.count(WalletTrade.id)).where(
                WalletTrade.chain == Chain.solana, WalletTrade.tx_hash == sig))).scalar() or 0
        bt = trades[0].block_time
        lag = (datetime.now(timezone.utc) - bt).total_seconds() if bt else -1
        sides = "/".join(t.side for t in trades)
        if not stored:   # under TRACKER_MIN_BUY_USD, no DEX pair, too little liquidity...: same filters as the poller
            log.info("fast: %s %s %s filtered out (%.1fs after the block)", wallet[:6], sides, sig[:12], lag)
            return 0
        self.handled += 1
        wake_engines()
        took = time.monotonic() - t0
        log.info("fast: %s %s %s recorded %.1fs after the block (notified at %.1fs, %.1fs queued, %.1fs to fetch "
                 "+ record) via %s", wallet[:6], sides, sig[:12], lag, max(lag - took - queued, 0.0), queued, took, source)
        return stored

    # ---- connection -----------------------------------------------------------
    async def run_forever(self) -> None:
        import websockets

        if not self.rpc or not self.wallets:
            log.info("Fast detection off (needs LIVE_RPC_URL and copied wallets)")
            return
        loops = [self._helius_loop()]
        if self.s.pumpportal_api_key and self.s.live_fast_pumpportal:
            loops.append(self._pumpportal_loop())
        await asyncio.gather(*loops)

    async def _pumpportal_loop(self) -> None:
        """PumpPortal's account-trade stream (paid, 0.01 SOL per 10,000 trades): pump.fun and PumpSwap trades of the
        copied wallets, usually under a second after the block, where Helius's notifications often lag 10-50 s
        (measured 2026-11-22). Helius stays on for everything else; whichever source is first records the trade."""
        import websockets

        url, delay = f"wss://pumpportal.fun/api/data?api-key={self.s.pumpportal_api_key}", 1.0
        wallets = set(self.wallets)
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2**22,
                                              open_timeout=15) as ws:
                    await ws.send(json.dumps({"method": "subscribeAccountTrade", "keys": sorted(wallets)}))
                    log.info("Fast detection: PumpPortal stream connected for %d wallets", len(wallets))
                    delay = 1.0
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue
                        if msg.get("errors") or (msg.get("message") and "subscribed" not in str(msg["message"])):
                            log.warning("fast: PumpPortal says %s", str(msg)[:200])
                            continue
                        w, sig = msg.get("traderPublicKey"), msg.get("signature")
                        if w in wallets and sig:
                            self._spawn(self.handle_signature(w, sig, "pumpportal"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("fast: PumpPortal stream dropped (%s: %s); reconnecting in %.0fs",
                            type(exc).__name__, str(exc)[:120], delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)

    def _spawn(self, coro) -> None:
        async def run() -> None:
            try:
                await coro
            except Exception:
                log.exception("fast: handling a trade failed")
        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _helius_loop(self) -> None:
        import websockets

        url, delay = ws_url(self.rpc), 1.0
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2**22,
                                              open_timeout=15) as ws:
                    self._subs.clear()
                    self._pending.clear()
                    for req in self.subscribe_requests():
                        await ws.send(req)
                    log.info("Fast detection connected: watching %d wallets over WebSocket", len(self.wallets))
                    delay = 1.0
                    async for raw in ws:
                        self.dispatch(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("fast: WebSocket dropped (%s: %s); reconnecting in %.0fs",
                            type(exc).__name__, str(exc)[:120], delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)
