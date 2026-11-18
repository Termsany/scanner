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
  source: "manual" | "auto";
  enabled: boolean;
  wins: number;
  appearances: number;
  win_tokens: { symbol: string; address: string; gain_pct: number }[];
  updated_at: string;
}

export interface SmartWalletList {
  items: SmartWallet[];
  enabled_manual: number;
  enabled_auto: number;
  disabled: number;
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
