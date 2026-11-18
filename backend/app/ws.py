"""Live signal fan-out for `/ws/live-signals`.

Signal source:
  * Redis configured -> subscribe to `REDIS_SIGNAL_CHANNEL` (the worker publishes there, ~0 latency).
  * No Redis         -> poll the `signals` table for ids above the last one seen (works across processes).
The `scanner_runs` table is always polled so clients get a `scan_complete` event after every cycle and can
refetch the table.

Server -> client messages
  {"type": "hello", "recent": [LiveSignal...], "filters": {...}}
  {"type": "signal", ...LiveSignal}
  {"type": "scan_complete", "run_id", "scored", "signals", "finished_at"}
  {"type": "heartbeat", "ts"}
  {"type": "pong"} | {"type": "subscribed", "filters": {...}} | {"type": "error", "message"}

Client -> server messages
  {"type": "subscribe", "chains": ["solana", "base"], "min_score": 80}   (both optional)
  {"type": "ping"}
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy import func, select

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models import Chain, ScannerRun, Signal
from app.repository import recent_signals, utc

log = logging.getLogger(__name__)

SEND_TIMEOUT = 5.0
MAX_CLIENTS = 1_000


@dataclass
class ClientFilter:
    chains: set[str] | None = None
    min_score: int = 75

    def matches(self, msg: dict[str, Any]) -> bool:
        if msg.get("type") != "signal":
            return True
        if self.chains and msg.get("chain") not in self.chains:
            return False
        return int(msg.get("score") or 0) >= self.min_score

    def as_dict(self) -> dict[str, Any]:
        return {"chains": sorted(self.chains) if self.chains else None, "min_score": self.min_score}


@dataclass
class _Client:
    ws: WebSocket
    filter: ClientFilter
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SignalHub:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.clients: dict[int, _Client] = {}
        self._tasks: list[asyncio.Task] = []
        self._last_signal_id = 0
        self._last_run_id = 0
        self._drain_lock = asyncio.Lock()

    # ---- lifecycle ------------------------------------------------------
    async def start(self) -> None:
        async with SessionLocal() as s:
            self._last_signal_id = (await s.execute(select(func.max(Signal.id)))).scalar() or 0
            self._last_run_id = (await s.execute(select(func.max(ScannerRun.id)))).scalar() or 0
        source = self._redis_loop() if self.settings.redis_url else self._poll_signals_loop()
        self._tasks = [
            asyncio.create_task(source, name="ws-signal-source"),
            asyncio.create_task(self._poll_runs_loop(), name="ws-run-poll"),
            asyncio.create_task(self._heartbeat_loop(), name="ws-heartbeat"),
        ]
        log.info("SignalHub started (source=%s)", "redis" if self.settings.redis_url else "db-poll")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        for c in list(self.clients.values()):
            with contextlib.suppress(Exception):
                await c.ws.close(code=1001)
        self.clients.clear()

    # ---- connection handling --------------------------------------------
    async def handle(self, ws: WebSocket) -> None:
        if len(self.clients) >= MAX_CLIENTS:
            await ws.close(code=1013)  # try again later
            return
        await ws.accept()
        client = _Client(ws=ws, filter=ClientFilter(min_score=self.settings.live_signal_min_score))
        self.clients[id(ws)] = client
        try:
            await self._send_hello(client)
            while True:
                raw = await ws.receive_text()
                await self._on_message(client, raw)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.debug("WebSocket client error", exc_info=True)
        finally:
            self.clients.pop(id(ws), None)

    async def _send_hello(self, c: _Client) -> None:
        async with SessionLocal() as s:
            recent = await recent_signals(s, limit=20, min_score=c.filter.min_score)
        await self._send(c, {"type": "hello", "filters": c.filter.as_dict(),
                             "recent": [r.payload | {"signal_id": r.id} for r in recent
                                        if c.filter.matches({"type": "signal", **r.payload})]})

    async def _on_message(self, c: _Client, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            await self._send(c, {"type": "error", "message": "invalid JSON"})
            return
        kind = msg.get("type") if isinstance(msg, dict) else None
        if kind == "ping":
            await self._send(c, {"type": "pong"})
        elif kind == "subscribe":
            chains = msg.get("chains")
            valid = {ch.value for ch in Chain}
            if chains is not None:
                if not isinstance(chains, list) or any(ch not in valid for ch in chains):
                    await self._send(c, {"type": "error", "message": f"chains must be a subset of {sorted(valid)}"})
                    return
                c.filter.chains = set(chains) or None
            if "min_score" in msg:
                try:
                    c.filter.min_score = min(max(int(msg["min_score"]), 0), 100)
                except (TypeError, ValueError):
                    await self._send(c, {"type": "error", "message": "min_score must be an integer 0-100"})
                    return
            await self._send(c, {"type": "subscribed", "filters": c.filter.as_dict()})
        else:
            await self._send(c, {"type": "error", "message": "unknown message type"})

    # ---- broadcasting ---------------------------------------------------
    async def _send(self, c: _Client, msg: dict[str, Any]) -> bool:
        try:
            async with c.lock:
                await asyncio.wait_for(c.ws.send_text(json.dumps(msg, default=str)), SEND_TIMEOUT)
            return True
        except Exception:
            self.clients.pop(id(c.ws), None)
            with contextlib.suppress(Exception):
                await c.ws.close(code=1011)
            return False

    async def broadcast(self, msg: dict[str, Any]) -> int:
        targets = [c for c in list(self.clients.values()) if c.filter.matches(msg)]
        if not targets:
            return 0
        results = await asyncio.gather(*(self._send(c, msg) for c in targets))
        return sum(results)

    # ---- sources --------------------------------------------------------
    async def _drain_signals(self) -> None:
        """Broadcast every committed signal above the last id seen (DB-poll mode)."""
        async with self._drain_lock:
            async with SessionLocal() as s:
                new = await recent_signals(s, since_id=self._last_signal_id, limit=100)
            for sig in new:
                self._last_signal_id = max(self._last_signal_id, sig.id)
                await self.broadcast({**sig.payload, "type": "signal", "signal_id": sig.id})

    async def _poll_signals_loop(self) -> None:
        while True:
            try:
                await self._drain_signals()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("signal poll failed", exc_info=True)
            await asyncio.sleep(self.settings.ws_poll_seconds)

    async def _redis_loop(self) -> None:
        import redis.asyncio as aioredis

        while True:
            client = aioredis.from_url(self.settings.redis_url, decode_responses=True)
            pubsub = client.pubsub()
            try:
                await pubsub.subscribe(self.settings.redis_signal_channel)
                async for item in pubsub.listen():
                    if item.get("type") != "message":
                        continue
                    try:
                        payload = json.loads(item["data"])
                    except (ValueError, TypeError):
                        continue
                    self._last_signal_id = max(self._last_signal_id, int(payload.get("signal_id") or 0))
                    await self.broadcast({**payload, "type": "signal"})
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("Redis subscription dropped; reconnecting in 3s", exc_info=True)
                await asyncio.sleep(3)
            finally:
                with contextlib.suppress(Exception):
                    await pubsub.aclose()
                    await client.aclose()

    async def _poll_runs_loop(self) -> None:
        while True:
            try:
                async with SessionLocal() as s:
                    runs = (await s.execute(
                        select(ScannerRun).where(ScannerRun.id > self._last_run_id,
                                                 ScannerRun.finished_at.is_not(None))
                        .order_by(ScannerRun.id.asc())
                    )).scalars().all()
                if runs and not self.settings.redis_url:
                    await self._drain_signals()  # guarantee a cycle's signals precede its scan_complete
                for r in runs:
                    self._last_run_id = r.id
                    await self.broadcast({"type": "scan_complete", "run_id": r.id, "scored": r.scored,
                                          "signals": r.signals_emitted, "ok": r.ok,
                                          "finished_at": utc(r.finished_at)})
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("run poll failed", exc_info=True)
            await asyncio.sleep(max(self.settings.ws_poll_seconds, 2.0))

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.ws_heartbeat_seconds)
            await self.broadcast({"type": "heartbeat", "ts": datetime.now(timezone.utc)})
