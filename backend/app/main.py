"""FastAPI application: REST API, `/ws/live-signals`, AI analyst, Telegram alerts.

Run:
  uvicorn app.main:app --host 0.0.0.0 --port 8000
  # worker in a second process:  python -m app.scanner_worker
  # or single-process MVP:       RUN_SCANNER_IN_API=true uvicorn app.main:app
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from sqlalchemy import text

from app.ai_analyst import AIAnalyst
from app.api import ai as ai_routes
from app.api import live as live_routes
from app.api import me as me_routes
from app.api import tokens as token_routes
from app.cache import Cache
from app.config import get_settings
from app.db import SessionLocal, engine, init_db
from app.telegram_alerts import TelegramAlerts
from app.ws import SignalHub

settings = get_settings()
logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await init_db()
    app.state.cache = Cache(settings.redis_url)
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(settings.http_timeout_seconds),
                                       headers={"User-Agent": "alpha-scanner/0.1"})
    app.state.analyst = AIAnalyst(app.state.cache, settings)
    app.state.hub = SignalHub(settings)
    app.state.telegram = None
    await app.state.hub.start()

    background: list[asyncio.Task] = []
    telegram: TelegramAlerts | None = None
    if settings.telegram_bot_token:
        telegram = TelegramAlerts(settings)
        app.state.telegram = telegram
        background.append(asyncio.create_task(telegram.run(), name="telegram"))
    scanner = None
    if settings.run_scanner_in_api:
        from app.scanner_worker import Scanner

        scanner = Scanner(settings)
        background.append(asyncio.create_task(scanner.run_forever(), name="scanner"))

    log.info("API ready (cache=%s, ai=%s, telegram=%s, embedded_scanner=%s)", app.state.cache.backend,
             "llm" if app.state.analyst.enabled else "rules", bool(telegram), bool(scanner))
    try:
        yield
    finally:
        for t in background:
            t.cancel()
        for t in background:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        if telegram:
            await telegram.close()
        if scanner:
            await scanner.close()
        await app.state.hub.stop()
        await app.state.http.aclose()
        await app.state.cache.close()
        await engine.dispose()


app = FastAPI(title="Alpha Scanner API", version="0.2.0", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1_024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["*"],
)
app.include_router(token_routes.router)
app.include_router(ai_routes.router)
app.include_router(live_routes.router)
app.include_router(me_routes.router)


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict[str, object]:
    async with SessionLocal() as s:
        await s.execute(text("SELECT 1"))
    return {"ok": True, "ws_clients": len(app.state.hub.clients), "cache": app.state.cache.backend}


@app.websocket("/ws/live-signals")
async def live_signals(ws: WebSocket) -> None:
    await app.state.hub.handle(ws)
