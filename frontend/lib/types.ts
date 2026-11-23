// Mirrors backend/app/schemas.py (Phase 1-2 API contracts).

export type Chain = "solana" | "ethereum" | "base" | "bsc";
export type Badge = "STRONG_ALPHA" | "MODERATE" | "HIGH_RISK";
export type SortField = "score" | "volume" | "volume_1h" | "liquidity" | "age" | "change_1h";
export type SortOrder = "asc" | "desc";

export const CHAINS: Chain[] = ["solana", "ethereum", "base", "bsc"];

export interface SecurityBadges {
  lp_locked: boolean | null;
  no_mint: boolean | null;
  no_freeze: boolean | null;
  honeypot: boolean | null;
  buy_tax: number | null;
  sell_tax: number | null;
}

export interface TokenSummary {
  id: number;
  chain: Chain;
  address: string;
  symbol: string;
  name: string;
  image_url: string | null;
  pair_url: string | null;
  dex_id: string | null;
  pair_created_at: string | null;
  price_usd: number | null;
  price_change_m5: number | null;
  price_change_h1: number | null;
  price_change_h24: number | null;
  liquidity_usd: number | null;
  volume_h1: number | null;
  volume_h24: number | null;
  market_cap: number | null;
  fdv: number | null;
  alpha_score: number;
  badge: Badge;
  smart_money_hits: number;
  badges: SecurityBadges;
  last_scanned_at: string;
}

export interface PaginatedTokens {
  items: TokenSummary[];
  page: number;
  page_size: number;
  total: number;
}

export interface MarketMetrics {
  chain: Chain;
  address: string;
  symbol: string;
  name: string;
  pair_address: string | null;
  dex_id: string | null;
  pair_url: string | null;
  quote_symbol: string | null;
  pair_created_at: string | null;
  age_minutes: number | null;
  price_usd: number | null;
  price_change_m5: number | null;
  price_change_h1: number | null;
  price_change_h6: number | null;
  price_change_h24: number | null;
  liquidity_usd: number | null;
  volume_m5: number | null;
  volume_h1: number | null;
  volume_h6: number | null;
  volume_h24: number | null;
  buys_h1: number | null;
  sells_h1: number | null;
  buys_h24: number | null;
  sells_h24: number | null;
  fdv: number | null;
  market_cap: number | null;
  boosts_active: number;
  discovery_sources: string[];
}

export interface HolderEntry {
  address: string;
  pct: number;
  tag: string | null;
  is_contract: boolean | null;
  is_locked: boolean | null;
}

export interface RiskItem {
  name: string;
  level: "info" | "warn" | "danger";
  description: string;
  source: string;
}

export interface SecurityReport {
  chain: Chain;
  address: string;
  providers: string[];
  is_honeypot: boolean | null;
  is_mintable: boolean | null;
  is_freezable: boolean | null;
  is_open_source: boolean | null;
  is_proxy: boolean | null;
  owner_can_change_balance: boolean | null;
  hidden_owner: boolean | null;
  cannot_sell_all: boolean | null;
  transfer_pausable: boolean | null;
  buy_tax: number | null;
  sell_tax: number | null;
  lp_locked_pct: number | null;
  top10_holders_pct: number | null;
  holder_count: number | null;
  creator_pct: number | null;
  rugcheck_score: number | null;
  risks: RiskItem[];
  top_holders: HolderEntry[];
  fetched_at: string;
}

export interface ScoreComponent {
  key: string;
  label: string;
  points: number;
  max_points: number;
  passed: boolean;
  value: number | string | boolean | null;
  detail: string;
}

export interface CategoryScore {
  name: string;
  points: number;
  max_points: number;
  components: ScoreComponent[];
}

export interface ScoreBreakdown {
  version: string;
  total: number;
  badge: Badge;
  badge_color: "green" | "amber" | "red";
  security: CategoryScore;
  momentum: CategoryScore;
  smart_money: CategoryScore;
  hard_fail: string | null;
  data_gaps: string[];
  scored_at: string;
}

export interface TokenDetail {
  token: TokenSummary;
  metrics: MarketMetrics;
  security: SecurityReport | null;
  score: ScoreBreakdown | null;
  score_history: { total: number; scored_at: string }[];
}

export interface AIAnalysis {
  verdict: "BULLISH" | "NEUTRAL" | "AVOID";
  bullets: string[];
  confidence: number;
  model: string;
  generated_at: string;
  disclaimer: string;
}

export interface LiveSignal {
  type: "signal";
  signal_id: number | null;
  chain: Chain;
  address: string;
  symbol: string;
  name: string;
  score: number;
  badge: Badge;
  price_usd: number | null;
  liquidity_usd: number | null;
  volume_h1: number | null;
  price_change_h1: number | null;
  market_cap: number | null;
  pair_url: string | null;
  top_reasons: string[];
  created_at: string;
}

export interface TopGainer {
  chain: Chain;
  address: string;
  symbol: string;
  signal_price_usd: number;
  current_price_usd: number;
  gain_pct: number;
  signalled_at: string;
}

export interface ScannerStatus {
  live: boolean;
  last_run_at: string | null;
  last_run_ok: boolean | null;
  last_run_scored: number | null;
  last_run_candidates: number | null;
  last_run_errors: number | null;
  last_cycle_seconds: number | null;
  security_coverage_pct: number | null;
  scan_interval_seconds: number;
  active_tokens: number;
  high_alpha_24h: number;
  signals_24h: number;
  top_gainer: TopGainer | null;
}

export interface Candle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface ChartData {
  source: "geckoterminal" | "snapshots";
  timeframe: string;
  candles: Candle[];
}

export type ChartInterval = "1m" | "5m" | "15m" | "1h" | "4h" | "1d";

export interface TokenQuery {
  chain?: Chain;
  min_score?: number;
  min_liquidity?: number;
  badge?: Badge;
  q?: string;
  sort?: SortField;
  order?: SortOrder;
  page?: number;
  page_size?: number;
}

export interface TokenRef {
  chain: Chain;
  address: string;
}

export interface SmartWallet {
  chain: Chain;
  address: string;
  label: string;
  source: "manual" | "auto" | "birdeye";
  enabled: boolean;
  wins: number;
  appearances: number;
  win_tokens: { symbol: string; address: string; gain_pct: number; pnl_usd?: number }[];
  stats: { win_rate?: number; realized_usd_30d?: number; trades_30d?: number } | null;
  track_buys: boolean;
  updated_at: string;
}

export interface SmartWalletList {
  items: SmartWallet[];
  enabled_manual: number;
  enabled_auto: number;
  enabled_birdeye: number;
  disabled: number;
  telegram_bot: string | null;
}

export interface SignalOut {
  id: number;
  chain: Chain;
  address: string;
  symbol: string;
  score: number;
  badge: Badge;
  price_usd: number | null;
  current_price_usd: number | null;
  gain_pct: number | null;
  created_at: string;
  payload: Record<string, unknown>;
}

/** Feed item: a live signal, plus the gain since it fired when loaded from GET /api/signals. */
export type FeedSignal = LiveSignal & { gain_pct?: number | null };

export interface PerfStats {
  n: number;
  n_24h: number;
  median_1h: number | null;
  median_6h: number | null;
  median_24h: number | null;
  positive_6h_pct: number | null;
  positive_24h_pct: number | null;
  hit_50_pct: number | null;
  median_peak: number | null;
  median_trough: number | null;
}

export interface PerfRow {
  symbol: string;
  chain: Chain;
  token_address: string;
  score: number;
  signal_time: string;
  entry_price: number | null;
  r_1h: number | null;
  r_6h: number | null;
  r_24h: number | null;
  peak_pct: number | null;
  trough_pct: number | null;
  peak_minutes: number | null;
}

export interface Performance {
  window_hours: number;
  signals_in_window: number;
  measured: number;
  overall: PerfStats;
  by_score: (PerfStats & { label: string })[];
  by_chain: (PerfStats & { chain: Chain })[];
  best: { symbol: string; chain: Chain; score: number; peak_pct: number; peak_minutes: number; token_address: string }[];
  recent: PerfRow[];
}

export interface PaperStrategy {
  name: string;
  title: string;
  tp_pct: number;
  sl_pct: number;
  mirror: boolean;
  max_hold_minutes: number;
  trades: number;
  open: number;
  closed: number;
  wins: number;
  win_rate: number | null;
  realized_usd: number;
  unrealized_usd: number;
  invested_usd: number;
  return_pct: number | null;
  avg_pnl_pct: number | null;
  best_pct: number | null;
  worst_pct: number | null;
  exit_reasons: Record<string, number>;
  reset_at?: string | null;       // the owner reset its numbers: only trades opened since count
  custom?: { id: number; enabled: boolean } | null;   // one of the owner's own curve strategies
}

/** One of the owner's own curve paper strategies (built on /paper). */
export interface CurveRuleOut {
  id: number;
  name: string;
  title: string;
  enabled: boolean;
  level: number;
  exit_at: number | null;
  tp_pct: number | null;
  half_at: number | null;
  sl_pct: number | null;
  skip_drop: number | null;
  max_secs: number | null;
  skip_backslide: boolean;
  size_usd: number;
  max_hold_min: number;
  curve_gain_pct: number | null;
  created_at: string | null;
}

export interface CurveRuleIn {
  level: number;
  exit_at: number | null;
  tp_pct: number | null;
  half_at: number | null;
  sl_pct: number | null;
  skip_drop: number | null;
  max_secs: number | null;
  skip_backslide: boolean;
  size_usd: number;
  max_hold_min: number;
}

export interface PaperPositionOut {
  id: number;
  strategy: string;
  chain: Chain;
  token_address: string;
  symbol: string;
  trigger: string;
  status: "open" | "closed";
  size_usd: number;
  entry_time: string | null;
  entry_price: number;
  last_price: number | null;
  peak_pct: number | null;
  trough_pct: number | null;
  exit_time: string | null;
  exit_price: number | null;
  exit_reason: string | null;
  pnl_usd: number | null;
  pnl_pct: number | null;
  remaining_pct: number;
}

export interface PaperSummary {
  hours: number;
  size_usd: number;
  slippage_pct: number;
  generated_at: string;
  mirror_wallets: string[];
  strategies: PaperStrategy[];
  positions: PaperPositionOut[];
}

export interface LiveFill {
  side: "buy" | "buy_hand" | "held" | "sell" | "sell_failed";
  sig?: string;
  sol?: number;
  usd?: number;
  reason?: string;
  error?: string;
  share?: number;
  at: string;
}

/** The owner's target for one position: in % over the entry, or by the coin's price (USD per token). */
export interface TargetBody {
  target_pct: number | null;
  share: number;
  stop_pct: number | null;
  target_price?: number | null;
  stop_price?: number | null;
  hands_off?: boolean;
}

export interface LivePositionOut {
  id: number;
  mode: "dry" | "live";
  account: string;
  token_address: string;
  symbol: string;
  leader_wallet: string;
  status: "open" | "closed" | "failed";
  cost_usd: number;
  sol_spent: number;
  proceeds_usd: number;
  proceeds_sol: number;
  pnl_usd: number | null;
  tokens_left: string;
  tokens_bought: string;
  buy_signature: string | null;
  manual_sell_frac: number | null;
  target_pct?: number | null;     // owner's target: sell target_share at +target_pct
  target_share?: number | null;
  stop_pct?: number | null;       // owner's stop: sell all at this (negative) %
  target_price?: number | null;   // or by the coin's price (USD per token)
  stop_price?: number | null;
  hands_off?: boolean;            // no bot rule sells it, only the owner's target / stop
  fills: LiveFill[];
  exit_reason: string | null;
  error: string | null;
  opened_at: string | null;
  closed_at: string | null;
  price_usd?: number | null;
  value_usd?: number | null;
  unrealized_usd?: number | null;
  unrealized_pct?: number | null;
}

export interface LeaderTrade {
  id: number;
  wallet: string;
  label: string;
  side: "buy" | "sell";
  kind: string;
  symbol: string;
  token_address: string;
  usd: number | null;
  pct_sold: number | null;
  tx: string;
  at: string;
}

/** A coin in an account's wallet (/live, Wallet coins tab). */
export interface HoldingCoin {
  mint: string;
  symbol: string;
  raw: string;
  amount: number;
  price_usd: number | null;
  value_usd: number | null;
  stable: boolean;
  position: LivePositionOut | null;   // the bot's open position of this coin, if any
}

export interface Holdings {
  mode: string;
  updated_at: string;
  accounts: { name: string; pubkey: string; coins: HoldingCoin[] }[];
}

/** The owner's price order (/live, Orders tab): buy once the coin reaches a price. */
export interface PriceOrderOut {
  id: number;
  account: string;
  mint: string;
  symbol: string;
  condition: "below" | "above";
  trigger_price: number;
  amount_usd: number;
  max_over_pct: number;
  then_target_price: number | null;
  then_target_share: number | null;
  then_stop_price: number | null;
  status: "open" | "filled" | "cancelled" | "expired" | "failed";
  last_price: number | null;
  position_id: number | null;
  note: string | null;
  expires_at: string | null;
  created_at: string | null;
  done_at: string | null;
}

export interface PriceOrderIn {
  account: string;
  mint: string;
  condition: "below" | "above";
  trigger_price: number;
  amount_usd: number;
  max_over_pct?: number | null;
  target_price?: number | null;
  target_share?: number | null;
  stop_price?: number | null;
  expires_hours?: number | null;
}

export interface LiveStatus {
  mode: "off" | "dry" | "live";
  paused: boolean;
  sol_usd: number | null;
  updated_at: string;
  accounts: LiveAccount[];
  leader_activity: LeaderTrade[];
}

export interface LiveAccount {
  name: string;
  active: boolean;
  paused?: boolean;
  enabled?: boolean;
  pubkey: string | null;
  sol_balance: number | null;
  sol_balance_usd: number | null;
  open_value_usd: number | null;
  unrealized_usd: number | null;
  wallets: string[];
  size_usd: number;
  max_open_usd: number;
  max_daily_loss_usd: number;
  realized_usd: number;
  realized_24h: number;
  open_cost_usd: number;
  open: LivePositionOut[];
  closed: LivePositionOut[];
  failed: LivePositionOut[];
}

export interface ScoutCandidate {
  id: number;
  address: string;
  source: string;
  hits: number;
  status: "new" | "rejected" | "trial" | "passed" | "failed" | "added";
  reason: string;
  metrics: { days: number; per_day: number; closed: number; win_rate: number; net_usd: number; net_wo_best: number;
    profit_factor: number; hold_min: number; buy_usd: number } | null;
  trial: { trades: number; closed: number; wins: number; net_usd: number; open_usd: number; size_usd: number } | null;
  found_at: string | null;
  trial_end: string | null;
}

export interface ScoutList {
  enabled: boolean;
  hour_utc: number;
  candidates: ScoutCandidate[];
}

export interface TelegramKind {
  key: string;
  title: string;
  help: string;
  on: boolean;
}

export interface TelegramPrefs {
  configured: boolean;     // bot token and chat set on the server
  kinds: TelegramKind[];
}

export interface LeaderCfg {
  wallet: string;
  label: string;
  unconditional: boolean;
  new_tokens_only: boolean;
  size_usd: number | null;
  max_chase_pct?: number | null;   // most paid above its price, %; null = account default
  quick_exit?: boolean;   // sell all at +50% / -25% / 6 h / its first sell instead of the 2x rule
  ignore_sells?: boolean; // never sell because it sells (fast wallets): exits only from quick exit / max hold
  quick_tp_pct?: number | null;   // quick exit take profit +% (null = default 50)
  quick_sl_pct?: number | null;   // quick exit stop loss, negative % (null = default -25)
  quick_hours?: number | null;    // quick exit time limit in hours (null = default 6)
  enabled?: boolean;   // false: no new buys from it (its open positions still follow its sells)
}

export interface TradingAccountCfg {
  name: string;
  enabled: boolean;
  paused: boolean;
  billing_hold?: boolean;
  open_by_leader?: Record<string, number>;
  key_source: "env" | "db";
  pubkey: string | null;
  size_usd: number;
  max_open_usd: number;
  max_daily_loss_usd: number;
  take_capital_at_pct: number | null;
  max_hold_hours: number | null;
  leaders: LeaderCfg[];
  open_positions: number;
  updated_at: string | null;
  user_id: number | null;
  cap_size_usd: number | null;
  cap_open_usd: number | null;
  cap_loss_usd: number | null;
  key_revealed: boolean;
}

export interface AccountsList {
  accounts: TradingAccountCfg[];
  can_store_keys: boolean;
  defaults: { max_hold_hours: number; reload_seconds: number };
}

export interface MyPosition {
  id: number;
  token_address: string;
  symbol: string;
  status: "open" | "closed" | "failed";
  cost_usd: number;
  proceeds_usd: number;
  pnl_usd: number | null;
  price_usd?: number | null;
  value_usd?: number | null;
  unrealized_usd?: number | null;
  unrealized_pct?: number | null;
  exit_reason: string | null;
  manual_sell_frac: number | null;
  buy_signature: string | null;
  opened_at: string | null;
  closed_at: string | null;
}

export interface MySettings {
  size_usd: number;
  max_open_usd: number;
  max_daily_loss_usd: number;
  take_capital_at_pct: number | null;
  paused: boolean;
  caps: { size_usd: number | null; max_open_usd: number | null; max_daily_loss_usd: number | null };
}

export interface MyAccount {
  name: string;
  enabled: boolean;
  pubkey: string | null;
  mode: "off" | "dry" | "live";
  key_revealed: boolean;
  sol_balance: number | null;
  sol_usd: number | null;
  sol_balance_usd: number | null;
  settings: MySettings;
  realized_usd: number;
  realized_24h: number;
  unrealized_usd: number | null;
  open_cost_usd: number;
  open: MyPosition[];
  closed: MyPosition[];
  stats?: MyStats;
  billing?: MyBilling | null;
}

export interface Bill {
  id: number;
  account?: string;
  email?: string | null;
  period_start: string;
  period_end: string;
  fee_usd: number;
  discount_usd: number;
  coupon_code: string | null;
  profit_pct: number;
  profit_usd: number;
  share_usd: number;
  total_usd: number;
  status: "due" | "sending" | "paid" | "check" | "waived";
  sol_amount: number | null;
  signature: string | null;
  attempts?: number;
  error: string | null;
  paid_at: string | null;
  created_at: string | null;
}

export interface MyBilling {
  fee_usd: number;
  profit_pct: number;
  grace_days: number;
  hold: boolean;
  coupon: { code: string; percent: number; months_left: number | null } | null;
  current: {
    trial: boolean;
    trial_ends: string | null;
    period_start: string | null;
    period_end: string;
    fee_usd: number;
    discount_usd: number;
    profit_pct: number;
    profit_usd: number;
    share_usd: number;
    total_usd: number;
    hwm_usd: number;
  };
  bills: Bill[];
}

export interface BillingConfig {
  enabled: boolean;
  fee_usd: number;
  profit_pct: number;
  trial_days: number;
  grace_days: number;
  wallet: string;
}

export interface BillingAdmin {
  config: BillingConfig;
  live: boolean;
  held: string[];
  totals: { paid_30d_usd: number; paid_30d_sol: number; due_usd: number; check: number };
  bills: Bill[];
}

export interface Coupon {
  id: number;
  code: string;
  percent: number;
  months: number | null;
  max_uses: number | null;
  uses: number;
  expires_at: string | null;
  active: boolean;
  note: string;
  created_at: string | null;
  users: string[];
}

export interface PublicTerms {
  billing: boolean;
  fee_usd: number;
  profit_pct: number;
  trial_days: number;
  grace_days: number;
}

export interface Me {
  user: { email: string; name: string; status: "pending" | "active" | "blocked"; lang?: "ar" | "en" };
  security: { totp_enabled: boolean; withdraw_lock_hours: number };
  telegram: { linked: boolean; daily: boolean };
  account: MyAccount | null;
}

export interface MyStats {
  trades: number;
  wins: number;
  losses: number;
  win_rate: number | null;
  total_pnl: number;
  roi_pct: number | null;
  avg_win: number | null;
  avg_loss: number | null;
  best: { symbol: string; pnl_usd: number | null; pct: number | null } | null;
  worst: { symbol: string; pnl_usd: number | null; pct: number | null } | null;
  daily: { day: string; pnl: number; trades: number }[];
}

export interface AuditEntry {
  at: string | null;
  actor: string;
  action: string;
  target: string;
  details: Record<string, unknown> | null;
  ip: string;
}

export interface SiteUser {
  id: number;
  email: string;
  name: string;
  status: "pending" | "active" | "blocked";
  account: string | null;
  created_at: string | null;
  last_login_at: string | null;
  totp_enabled: boolean;
  withdraw_locked_until: string | null;
}

export interface OverviewAccount {
  name: string;
  enabled: boolean;
  paused: boolean;
  billing_hold: boolean;
  pubkey: string | null;
  owner: { id: number; email: string; status: string } | null;
  size_usd: number;
  max_open_usd: number;
  open_count: number;
  open_cost_usd: number;
  open_value_usd: number | null;
  unrealized_usd: number | null;
  realized_24h: number;
  realized_7d: number;
  realized_all: number;
  trades: number;
  wins: number;
  win_rate: number | null;
  failed_24h: number;
  last_trade_at: string | null;
  heartbeat_at: string | null;
  sol_balance: number | null;
  sol_balance_usd: number | null;
}

export interface AdminOverview {
  mode: string;
  paused: boolean;
  updated_at: string;
  sol_usd: number | null;
  users: {
    total: number;
    pending: number;
    active: number;
    blocked: number;
    new_7d: number;
    pending_list: { id: number; email: string; name: string; created_at: string | null }[];
  };
  totals: {
    accounts: number;
    enabled: number;
    paused: number;
    open_count: number;
    open_cost_usd: number;
    open_value_usd: number | null;
    unrealized_usd: number | null;
    sol_balance: number | null;
    sol_balance_usd: number | null;
    realized_24h: number;
    realized_7d: number;
    realized_30d: number;
    realized_all: number;
    trades_30d: number;
    wins_30d: number;
    win_rate_30d: number | null;
    failed_24h: number;
  };
  accounts: OverviewAccount[];
  daily: { day: string; pnl: number; trades: number }[];
  recent: {
    id: number;
    account: string;
    symbol: string;
    token_address: string;
    status: "closed" | "failed";
    cost_usd: number;
    pnl_usd: number | null;
    pnl_pct: number | null;
    exit_reason: string | null;
    error: string | null;
    at: string | null;
  }[];
  signals: { last_24h: number; last_at: string | null };
}
