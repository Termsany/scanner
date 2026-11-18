"""Central configuration, loaded from environment variables / `.env`."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from typing import Any

from pydantic import Field, field_validator
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
    # Auto-discovery: a wallet becomes "smart" when it was a top holder at signal time of at least
    # MIN_WINS tokens that later rallied >= WIN_GAIN_PCT, with a win rate >= MIN_WIN_RATE across all
    # signalled tokens it held. Wallets seen in too many tokens are infrastructure, not traders.
    smart_auto_enabled: bool = True
    smart_auto_interval_minutes: int = 30
    smart_auto_lookback_days: int = 14
    smart_auto_win_gain_pct: float = 50.0
    smart_auto_min_wins: int = 2
    smart_auto_min_win_rate: float = 0.5
    smart_auto_max_appearances: int = 40
    smart_auto_max_holder_pct: float = 20.0   # bigger holders are usually dev / team wallets
    smart_activity_check_enabled: bool = True # reject bot / program / contract addresses before adding them

    # Direct on-chain holdings check of tracked wallets (free public RPCs; override with your own)
    holdings_check_enabled: bool = True
    holdings_min_usd: float = 100.0
    holdings_max_accounts: int = 4_000          # per chain per cycle
    holdings_timeout_seconds: float = 20.0
    holdings_min_interval_seconds: float = 0.25  # between RPC requests
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    evm_rpc_urls: dict[str, str] = Field(default_factory=lambda: {
        "ethereum": "https://ethereum-rpc.publicnode.com",
        "base": "https://base-rpc.publicnode.com",
        "bsc": "https://bsc-rpc.publicnode.com",
    })

    # Signal performance (GeckoTerminal candles; free API: 30 calls/min shared with the scanner)
    perf_interval_seconds: int = 300
    perf_max_calls_per_run: int = 15             # = 3 calls/min on average
    telegram_daily_report_hour: int | None = 8   # UTC hour for the daily performance summary; None = off

    # Buy alerts for wallets the operator tracks (/track in Telegram)
    tracker_enabled: bool = True
    tracker_interval_seconds: int = 30
    tracker_min_buy_usd: float = 100.0
    tracker_min_liquidity_usd: float = 5_000.0
    tracker_regroup_minutes: int = 60          # repeat buys of the same token inside this window: one alert
    tracker_max_evm_block_range: int = 1_000   # cap catch-up after downtime
    # getLogs without a contract address (any token sent to the wallet): publicnode refuses it; dRPC's free
    # tier allows it for ranges <= 100 blocks, so queries are split into TRACKER_EVM_LOG_SPAN chunks.
    # BSC: dRPC's public endpoint rate-limits 30s polling (429); 1rpc.io allows it with <= 50-block ranges.
    tracker_evm_rpc_urls: dict[str, str] = Field(default_factory=lambda: {
        "ethereum": "https://eth.drpc.org",
        "base": "https://base.drpc.org",
        "bsc": "https://1rpc.io/bnb",
    })
    tracker_evm_log_span: int = 100
    tracker_evm_log_spans: dict[str, int] = Field(default_factory=lambda: {"bsc": 50})  # per-chain override
    tracker_backoff_max_seconds: int = 600       # after a 429, pause that chain (60s, doubling, up to this)
    tracker_max_tx_per_poll: int = 25          # per Solana wallet
    tracker_solana_min_interval: float = 0.5   # seconds between Solana RPC calls (public RPC 429s faster)

    # Birdeye trader discovery (free "Standard" key: 30k CU/month, 30 CU per call, 1 req/s)
    birdeye_api_key: str | None = None
    birdeye_interval_minutes: int = 120
    birdeye_daily_call_budget: int = 30          # 30/day = 900/month, under the free 1,000
    birdeye_seed_limit: int = 50                 # chain-wide weekly PnL leaders per chain per day
    birdeye_seed_max_trades_week: int = 1_500    # more than this is a bot / market maker
    birdeye_seed_min_trades_week: int = 10       # fewer is a one-off flip (often an insider)
    birdeye_seed_min_realized_usd: float = 2_500.0
    birdeye_min_token_pnl_usd: float = 1_000.0   # min PnL on a winner token to become a candidate
    birdeye_min_win_rate: float = 0.55           # 30-day win rate to qualify
    birdeye_min_realized_usd_30d: float = 5_000.0
    birdeye_min_trades_30d: int = 20
    birdeye_max_trades_30d: int = 5_000
    birdeye_min_closed_positions: int = 5
    birdeye_min_unique_tokens: int = 5
    birdeye_reverify_days: int = 7

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
    llm_max_tokens: int = 600
    # Provider-specific request fields, JSON. E.g. NVIDIA reasoning models answer far faster with
    # LLM_EXTRA_BODY={"chat_template_kwargs": {"enable_thinking": false}}
    llm_extra_body: dict[str, Any] = Field(default_factory=dict)
    ai_cache_ttl_seconds: int = 600
    ai_requests_per_minute: int = 10         # per client IP

    # --- Telegram --------------------------------------------------------------
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None      # channel / group / user that receives signals
    telegram_min_score: int = 80
    telegram_commands_enabled: bool = True   # /top, /token, /status via long polling
    public_dashboard_url: str | None = None  # deep links in alerts, e.g. https://scanner.example.com

    log_level: str = "INFO"

    @field_validator("llm_base_url")
    @classmethod
    def _strip_endpoint_path(cls, v: str) -> str:
        """The OpenAI client appends /chat/completions itself; accept a pasted full endpoint URL too."""
        v = v.strip().rstrip("/")
        for suffix in ("/chat/completions", "/completions"):
            if v.endswith(suffix):
                v = v[: -len(suffix)]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
