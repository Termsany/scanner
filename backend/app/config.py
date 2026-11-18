"""Central configuration, loaded from environment variables / `.env`."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Persistence -------------------------------------------------------
    database_url: str = f"sqlite+aiosqlite:///{(BACKEND_DIR / 'alpha_scanner.db').as_posix()}"
    redis_url: str | None = None  # e.g. redis://localhost:6379/0 ; None -> in-memory fallback

    # --- Scanner loop ------------------------------------------------------
    scan_interval_seconds: int = 60
    chains: list[str] = Field(default_factory=lambda: ["solana", "ethereum", "base", "bsc"])
    min_liquidity_usd: float = 10_000.0
    min_volume_1h_usd: float = 5_000.0
    max_tokens_per_cycle: int = 120          # hard cap on tokens sent to security APIs per cycle
    security_cache_ttl_seconds: int = 1_800  # security data changes slowly; re-check every 30 min
    http_timeout_seconds: float = 15.0

    # --- Signals -----------------------------------------------------------
    live_signal_min_score: int = 75
    signal_cooldown_seconds: int = 3_600     # don't re-emit the same token more than once per hour
    redis_signal_channel: str = "alpha:signals"

    # --- Smart money -------------------------------------------------------
    smart_wallets_file: Path = BACKEND_DIR / "data" / "smart_wallets.json"

    # --- Optional API keys (all endpoints used work keyless at low volume) --
    goplus_access_token: str | None = None
    rugcheck_api_key: str | None = None

    # --- API server ----------------------------------------------------------
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"])
    run_scanner_in_api: bool = False         # single-process MVP: run the worker loop inside the API
    active_window_minutes: int = 1_440       # tokens not rescanned within this window drop off the table
    ws_heartbeat_seconds: int = 25
    ws_poll_seconds: float = 1.5             # DB poll cadence when Redis is not configured

    # --- AI analyst (any OpenAI-compatible endpoint) -------------------------
    # OpenAI:  https://api.openai.com/v1
    # Gemini:  https://generativelanguage.googleapis.com/v1beta/openai/
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str | None = None
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 20.0
    ai_cache_ttl_seconds: int = 600
    ai_requests_per_minute: int = 10         # per client IP

    # --- Telegram --------------------------------------------------------------
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None      # channel / group / user that receives signals
    telegram_min_score: int = 80
    telegram_commands_enabled: bool = True   # /top, /token, /status via long polling
    public_dashboard_url: str | None = None  # deep links in alerts, e.g. https://scanner.example.com

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
