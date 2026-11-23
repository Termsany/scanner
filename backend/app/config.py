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

    gecko_discovery_every_cycles: int = 3        # GeckoTerminal trending/new lists: 8 calls -> every 3rd cycle

    # Paper trading (simulation only, no real money)
    paper_enabled: bool = True
    paper_interval_seconds: int = 60            # signals and anything not handled faster below
    # Closer to live (2026-11-21): wallet buys are entered a few seconds after the trade at the market price then
    # (Jupiter, never below the wallet's own price), and open positions are re-priced every few seconds.
    paper_entry_seconds: float = 3.0
    paper_exit_seconds: float = 10.0
    paper_live_prices: bool = True

    # Wallet scout (app/wallet_scout.py): find -> strict filter -> 3-day realistic paper trial -> suggest.
    # Off until turned on: it spends Helius credits (one run a day, capped).
    scout_enabled: bool = False
    scout_hour_utc: int = 6
    scout_max_calls: int = 400                 # Helius calls per daily run
    scout_history_pages: int = 8               # up to 800 swaps per wallet, stopping once SCOUT_MIN_DAYS is covered
    scout_lookback_days: int = 3               # winners from this many days back
    scout_min_hits: int = 2                    # bought early in at least this many winners
    scout_max_trials: int = 4                  # wallets on paper trial at once (each is polled by the tracker)
    scout_trial_days: float = 3.0
    scout_min_days: float = 3.0                # history the strict filter needs
    scout_max_swaps_per_day: float = 150.0
    scout_min_closed: int = 15
    scout_min_profit_factor: float = 1.3
    scout_min_win_rate: float = 40.0
    scout_min_hold_minutes: float = 5.0
    # from arXiv 2601.08641 (smart-money copy trading): the average return per trade must be statistically above
    # zero and not driven by a few lottery wins, and the wallet must still be trading
    scout_min_tstat: float = 1.645
    scout_max_ret_std: float = 1.0               # std of per-trade return (1.0 = 100 %)
    scout_max_idle_hours: float = 72.0
    scout_trial_min_trades: int = 5
    paper_size_usd: float = 100.0
    paper_slippage_pct: float = 1.5             # charged on entry AND exit (pump.fun fee + slippage)
    paper_no_price_minutes: int = 60            # no market price this long: close at last known price
    # wallet_mirror copies these tracked wallets (buy with them, sell the same share when they sell).
    # Empty = every tracked wallet.
    paper_mirror_wallets: list[str] = Field(default_factory=list)
    paper_mirror_max_hold_hours: int = 24 * 7

    # Paper sniper (app/sniper.py): new pump.fun tokens, simulated only. Off until turned on.
    sniper_paper_enabled: bool = False
    sniper_size_usd: float = 30.0
    sniper_latency_s: float = 2.0                # our assumed delay from decision to a landed buy
    sniper_fee_pct: float = 1.0                  # pump.fun fee, each way
    sniper_slippage_pct: float = 2.0             # each way
    sniper_tp_pct: float = 50.0
    sniper_sl_pct: float = -30.0
    sniper_max_hold_s: float = 300.0
    sniper_sample_every: int = 10                # snipe_all: every Nth new token
    sniper_max_dev_buy_sol: float = 3.0          # creator bought more than this at launch: skip
    sniper_skip_mayhem: bool = True              # pump.fun "mayhem mode" tokens (agent-driven price): skip
    sniper_window_s: float = 20.0                # snipe_momentum: decide within this many seconds of launch
    sniper_min_buyers: int = 5
    sniper_min_net_sol: float = 3.0
    sniper_max_open: int = 60
    sniper_momentum_enabled: bool = False        # snipe_momentum (counts buys): off since 2026-11-23, it lost and
                                                 # many small trades predict graduation worse (arXiv 2602.14860)
    sniper_fast_tp_pct: float = 25.0             # snipe_fast_tp: take profit here...
    sniper_fast_trail_pct: float = 15.0          # ...or sell once the price is this % below its high

    # Paper graduation trades (app/grad.py): pump.fun tokens right after they move to PumpSwap. Simulated only.
    grad_paper_enabled: bool = False
    grad_size_usd: float = 30.0
    grad_latency_s: float = 2.0                  # our assumed delay from decision to a landed buy
    grad_fee_pct: float = 1.0                    # PumpSwap fee + priority/tip, each way
    grad_slippage_pct: float = 1.5               # each way
    grad_sl_pct: float = -20.0
    grad_tp1_pct: float = 20.0                   # sell GRAD_TP1_FRAC here...
    grad_tp1_frac: float = 0.5
    grad_tp2_pct: float = 50.0                   # ...and the rest here
    grad_max_hold_s: float = 1800.0
    grad_confirm_s: float = 60.0                 # grad_confirm: look this long after the migration...
    grad_confirm_min_pct: float = 5.0            # ...and buy only if the price is up at least this much
    grad_max_open: int = 40

    # Paper pre-graduation trades (app/curve.py): buy late on the pump.fun bonding curve, sell at graduation.
    curve_paper_enabled: bool = False
    curve_feed: str = "auto"                     # auto / helius / hybrid / pumpportal (see app/curve.py)
    curve_pp_from_pct: float = 50.0              # hybrid: stream a token from PumpPortal once this far along
    curve_size_usd: float = 30.0
    curve_latency_s: float = 2.0                 # our assumed delay from decision to a landed buy
    curve_fee_pct: float = 1.0                   # pump.fun fee + priority/tip, each way
    curve_slippage_pct: float = 2.0              # each way
    curve_grad_exit_extra_pct: float = 3.0       # graduation exit: the real sale is in the fresh PumpSwap pool
    curve_tp_pct: float = 0.0                    # 0 = no take profit: hold for graduation
    curve_sl_pct: float = -25.0
    curve_max_hold_s: float = 3600.0
    curve_max_open: int = 40
    curve_max_watch: int = 800                   # curves subscribed on Helius at once
    curve_prune1_s: float = 120.0                # unwatch a token still under CURVE_PRUNE1_PCT after this long...
    curve_prune1_pct: float = 10.0
    curve_prune2_s: float = 900.0                # ...or under CURVE_PRUNE2_PCT after this long
    curve_prune2_pct: float = 40.0
    curve_max_watch_s: float = 7200.0
    curve_skip_mayhem: bool = True
    # curve_70_half / curve_70_trail (user's rule 2026-11-23): $10, no stop loss, half at 2x, then a stop at entry
    curve_half_size_usd: float = 10.0
    curve_trail_pct: float = 30.0                # trail: after graduation, sell the rest 30% below its high
    curve_trail_max_hold_s: float = 7200.0       # trail: sell whatever is left after this long
    curve_trail_price_seconds: float = 3.0       # trail: Jupiter price of graduated coins this often

    # Live copy trading (REAL money, Solana via Jupiter). Off unless LIVE_MODE=live AND a key is set.
    #   off     nothing runs
    #   dry     quotes real Jupiter routes and logs what it would do; never signs or sends
    #   live    signs and sends swaps from LIVE_PRIVATE_KEY's wallet
    live_mode: str = "off"
    live_private_key: str | None = None          # base58 secret key or JSON byte array; set by the owner only
    live_wallets: list[str] = Field(default_factory=list)   # Solana wallets to copy
    live_size_usd: float = 30.0                  # per buy
    live_max_open_usd: float = 100.0             # total cost of open positions never exceeds this
    live_max_daily_loss_usd: float = 50.0        # realized loss in 24h above this: pause new buys
    # The owner's price orders (/live, Orders tab; app/orders.py): buy a coin once it reaches a price
    live_orders_enabled: bool = True
    live_orders_check_seconds: float = 2.0
    live_order_max_over_pct: float = 5.0         # default: never pay more than the trigger + this %
    live_slippage_bps: int = 1_500               # 15%: memecoins move fast; the quote's minimum out still applies
    live_slippage_bps_unconditional: int = 2_500 # 25% first try for LIVE_UNCONDITIONAL_WALLETS (fresh launches)
    live_max_slippage_bps: int = 5_000           # retries after a slippage failure widen up to this (50%)
    live_priority_fee_lamports: int = 1_000_000  # cap on priority fee per swap (0.001 SOL)
    # Buy pump.fun / PumpSwap tokens through PumpPortal's transaction API instead of Jupiter (user's choice
    # 2026-11-21: ~0.5% PumpPortal fee for skipping the shared Jupiter queue). Sells stay on Jupiter.
    live_direct_pump_buys: bool = False
    live_direct_priority_fee_sol: float = 0.0005
    live_direct_slippage_cap_pct: float = 15.0      # direct buys with a chase limit: slippage at most this
    live_direct_retry_slippage_pct: float = 40.0    # one retry when PumpPortal's own slippage check fails (6004)
    live_direct_max_overspend_pct: float = 3.0      # simulated cost above the entry size: rebuild smaller / use Jupiter
    # Best route (user's request 2026-11-23: buy where it's cheapest, sell where it pays most, fees included):
    # pump coins are priced on PumpPortal (simulated: exact, its 0.5% fee and priority fee included) and on
    # Jupiter (every pool, split routes) at the same time; the better one after fees is sent.
    live_best_route: bool = True
    live_best_route_sells: bool = True
    live_best_route_wait_seconds: float = 1.5       # a ready PumpPortal buy waits at most this for Jupiter's quote
    live_best_route_margin_pct: float = 0.3         # switch only when the other route is better by more than this
    live_jupiter_fee_estimate_sol: float = 0.0005   # network + priority fee a Jupiter swap adds (not in its quote)
    live_min_liquidity_usd: float = 10_000.0
    # Skip a copy buy when the live quote's price per token is more than this % above the price recorded with
    # the leader's buy (we would be buying the leader's pump). None = no limit.
    live_max_chase_pct: float | None = 10.0
    # Tokens the bot never buys, on any account (comma-separated mints). User's list, 2026-11-21:
    # PUMP (pump.fun's own token) and TTWO: leaders trade them all day, we only lose on fees.
    live_blocked_mints: str = ("pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn,"   # PUMP
                               "TTWofwAge91oFhZs7kpQdyrVRkmevgM88xijGvQFbKo,"   # TTWO
                               "SQQQAa3gUxgnqcEsdjPA4RQNQBgB97TnG2hJhk2WaGE,"   # SQQQ (added 2026-11-21)
                               "ANTBTZQ1EUuTxUs3tfeampxdqTWPHmwyJmcWj4xoAyNe")   # ANT (added 2026-11-21)
    live_max_chase_pct_no_checks: float | None = 300.0   # same guard for "no checks" leaders: never buy
                                                 # more than this % above the leader's price
    # When the leader sells, also sell the same share of tokens the owner bought by hand in the trading wallet
    live_sell_manual_with_leader: bool = True
    # Watch the copied wallets over LIVE_RPC_URL's WebSocket (~1 s) instead of waiting for the tracker's poll
    live_fast_detection: bool = True
    # Copy these leaders' buys only when they open a token they did not hold before (no add-ons / DCA)
    live_new_tokens_only_wallets: list[str] = Field(default_factory=list)
    # Copy these leaders with no per-trade conditions: no liquidity minimum, chase limit or scanner check, and the
    # tracker records all their trades (no $ minimum). Account limits (budget, daily loss, SOL, pause) still apply.
    live_unconditional_wallets: list[str] = Field(default_factory=list)
    # Scanner check before every copy buy (RugCheck + DexScreener, scored like the scanner); see app/screen.py
    live_screen_enabled: bool = True
    live_min_alpha_score: int | None = 70
    live_max_top10_pct: float = 50.0
    live_screen_fail_closed: bool = True          # check unavailable -> skip the buy
    live_sol_reserve: float = 0.02               # SOL kept back for fees
    live_interval_seconds: float = 3.0
    live_fast_commitment: str = "processed"      # WebSocket notifications: "processed" arrives ~0.5-1 s before "confirmed"
    live_fast_fetch_seconds: float = 3.0          # wait at most this long for a notified transaction to confirm
    pumpportal_api_key: str | None = None         # PumpPortal data API key (account-trade stream); never logged
    live_fast_pumpportal: bool = True             # use PumpPortal's stream next to Helius when a key is set
    # Prices of pump.fun / PumpSwap positions from PumpPortal's trade stream (app/pump_prices.py), before
    # Jupiter / DexScreener. Billed per event (0.01 SOL / 10,000), so only coins being checked are streamed.
    live_pump_prices: bool = False
    live_pump_prices_max_coins: int = 15          # at most this many coins subscribed at once
    live_pump_prices_keep_seconds: float = 60.0   # unsubscribe a coin nobody asked a price for this long
    live_pump_prices_max_age_seconds: float = 90.0  # older last trade: use Jupiter / DexScreener instead
    # Complete prices from the chain (app/pool_prices.py): Helius pushes every change of a coin's bonding curve
    # or PumpSwap pool vaults. Replaces the PumpPortal feed above, which misses most trades (1 of 7 seen).
    live_pool_prices: bool = False
    live_pool_prices_max_coins: int = 20
    live_pool_prices_keep_seconds: float = 60.0
    # PumpSwap coins too (pool vault balances). Off: on a live pool the vault ratio read 6.6% below Jupiter's
    # sell price (2026-11-23), so it is not the price the pool trades at. Bonding-curve coins are exact.
    live_pool_prices_amm: bool = False
    loop_freeze_log_seconds: float = 1.0          # worker event loop blocked this long: logged (app/loop_watch.py)
    loop_debug: bool = True                       # asyncio debug: names the step that blocked (small overhead)
    # Compare open positions with the wallet's real token balance this often; tokens sold by hand (Phantom)
    # are found in the wallet's on-chain history and booked with what they really sold for
    live_reconcile_seconds: float = 120.0
    # Once a position is up this much (100 = 2x), sell just enough to get its cost back; the rest (pure profit)
    # stays until the leader sells. None = off
    live_take_capital_at_pct: float | None = 100.0
    live_trim_cap_pct: float | None = 50.0       # at the take-capital target, sell up to this % of the coins
                                                 # (the user's rule: 50% at 2x); None = sell what repays the cost
    live_copy_trims: bool = False                # copy the leader's partial sells (up to live_trim_cap_pct)?
                                                 # off: only its full exit sells the rest
    live_follow_after_trim: bool = True          # the user's rule: a partial sell by the leader before 2x is
                                                 # copied and the position then follows all its sells (no 50% at
                                                 # 2x); with no partial sell first: 50% at 2x, rest on full exit
    live_bag_waits_for_full_exit: bool = True    # once the capital is back, leader trims are ignored: the rest
                                                 # is sold only when the leader sells everything
    live_book_hand_buys: bool = True             # buys made by hand of a token the bot holds join its position
    live_adopt_hand_buys: bool = True            # tokens bought by hand that the bot doesn't hold: tracked as a
                                                 # "manual" position (capital out at the take-capital target)
    live_adopt_lookback_hours: float = 24.0      # how far back a hand buy can be to be adopted
    live_take_capital_check_seconds: float = 2.0    # how often open positions are priced for take-capital
    live_take_capital_fast_from: float = 0.5       # within this share of the target (1.5x for +100%):
                                                   # price from Jupiter too (faster than DexScreener)
    live_take_capital_strong: float = 1.15         # this far past the target, one reading is enough
    # Hold-only tokens (by mint, never by name: fake "SOL"/"ETH" tokens are everywhere). The bot copies their buys
    # but never sells them by itself: not when the leader sells, not to take the capital, not at max hold.
    # Selling by hand (page / Telegram) still works.
    live_hold_mints: list[str] = Field(default_factory=lambda: [
        "So11111111111111111111111111111111111111112",    # SOL (wrapped)
        "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",   # ETH on Solana (Wormhole / Portal)
        "TTWofwAge91oFhZs7kpQdyrVRkmevgM88xijGvQFbKo",    # TTWO
    ])
    # Per-wallet "quick exit" switch (off by default; the user's 2x rule otherwise): sell everything at
    # +TP, at -SL, after the time limit, or on the wallet's first sell (like the paper wallet_follow)
    live_quick_tp_pct: float = 50.0
    live_quick_sl_pct: float = -25.0
    live_quick_max_hold_minutes: float = 360.0
    live_quick_check_seconds: float = 3.0        # quick-exit price check (one Jupiter call for all of them)
    live_max_hold_hours: int = 24 * 7            # sell everything after this even if the leader still holds
    live_time_stop_minutes: float | None = None  # off (user's choice). Set N to sell positions whose capital
                                                 # is not back N minutes after the buy
    live_rpc_url: str | None = None              # defaults to SOLANA_RPC_URL
    jupiter_api_url: str = "https://lite-api.jup.ag"
    # Extra trading wallets copying the same leaders with their own budget, e.g.
    # LIVE_ACCOUNTS=[{"name": "big", "size_usd": 50, "max_open_usd": 500, "max_daily_loss_usd": 250}]
    # Each one's key goes in LIVE_PRIVATE_KEY_<NAME> (LIVE_PRIVATE_KEY_BIG); without it the account stays idle.
    live_accounts: list[dict[str, Any]] = Field(default_factory=list)
    # Fernet key that encrypts private keys of accounts made on the /accounts page (generate one with
    # python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"). Keep a copy:
    # without it those wallets can't be used. Unset = new accounts can't be created from the page
    live_key_encryption_key: str | None = None
    live_accounts_reload_seconds: float = 30.0   # how often the worker picks up account edits
    live_dashboard_password: str | None = None
    # Registered users (/login, /me). Off = no sign-ups and no user logins
    users_enabled: bool = True
    users_registration_open: bool = True          # new sign-ups (they still wait for the operator's approval)
    user_session_days: int = 14
    site_url: str = "https://scanner.da3m.io"     # for links in reset messages
    # Password-reset email through Resend (https://resend.com). Unset = the link goes to the operator on Telegram
    resend_api_key: str | None = None
    email_from: str = "Sirb <no-reply@da3m.io>"
    password_reset_minutes: int = 60
    withdraw_lock_hours: int = 24                 # after a password reset or a 2FA reset/removal
    auth_cookie_secure: bool = True               # HTTPS only; set false for local http development
    # Defaults for a newly approved user's account
    user_default_size_usd: float = 10.0
    user_default_open_usd: float = 100.0
    user_default_loss_usd: float = 50.0   # unlocks /live on the dashboard; unset = page disabled
    # Billing of users' accounts (first values only: the admin edits them on /admin, stored in the database).
    # Off until the admin turns it on and sets the wallet that receives the payments.
    billing_fee_usd: float = 59.0                 # fixed fee per month
    billing_profit_pct: float = 15.0              # share of the month's net profit above the high-water mark
    billing_trial_days: int = 7                   # free days after approval
    billing_grace_days: int = 3                   # unpaid this long after the period ends: new buys pause
    billing_interval_seconds: float = 600.0       # how often the worker creates and collects bills

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
    telegram_signal_alerts: bool = True       # false = Telegram sends only tracked-wallet buy/sell alerts
    telegram_trade_group_seconds: int = 45    # hold wallet alerts this long to merge same-token trades (0 = off)
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

    def blocked_mints(self) -> set[str]:
        return {m.strip() for m in (self.live_blocked_mints or "").split(",") if m.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
