import type { Badge, Chain } from "./types";

export function usd(v: number | null | undefined, opts: { compact?: boolean } = {}): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  const a = Math.abs(v);
  if (opts.compact !== false && a >= 1_000) {
    if (a >= 1e9) return `$${(v / 1e9).toFixed(2)}B`;
    if (a >= 1e6) return `$${(v / 1e6).toFixed(2)}M`;
    return `$${(v / 1e3).toFixed(1)}K`;
  }
  return `$${v.toLocaleString("en-US", { maximumFractionDigits: 2 })}`;
}

/** Token prices span 1e-10..1e5: show 4 significant digits, with a subscript-zero form for tiny prices. */
export function price(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  if (Math.abs(v) < 1e-14) return "$0"; // chart axis ticks can land on ±epsilon
  if (v < 0) return `-${price(-v)}`;
  if (v >= 1) return `$${v.toLocaleString("en-US", { maximumFractionDigits: v >= 1000 ? 0 : 4 })}`;
  const exp = Math.floor(Math.log10(v));
  if (exp <= -5) {
    const zeros = -exp - 1;
    const digits = Math.round(v * 10 ** (-exp + 3)).toString().slice(0, 4);
    return `$0.0${toSubscript(zeros)}${digits}`;
  }
  return `$${v.toFixed(-exp + 3)}`;
}

function toSubscript(n: number): string {
  return String(n)
    .split("")
    .map((d) => "₀₁₂₃₄₅₆₇₈₉"[Number(d)])
    .join("");
}

export function pct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  const s = Math.abs(v) >= 1000 ? `${(v / 1000).toFixed(1)}K` : v.toFixed(digits);
  return `${v > 0 ? "+" : ""}${s}%`;
}

export function pctClass(v: number | null | undefined): string {
  if (v === null || v === undefined || v === 0) return "text-muted";
  return v > 0 ? "text-bull" : "text-bear";
}

export function age(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "—";
  const mins = Math.max(0, (now - new Date(iso).getTime()) / 60_000);
  if (mins < 60) return `${Math.floor(mins)}m`;
  const h = mins / 60;
  if (h < 48) return `${Math.floor(h)}h`;
  const d = h / 24;
  if (d < 60) return `${Math.floor(d)}d`;
  return `${Math.floor(d / 30)}mo`;
}

export function ago(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "never";
  const s = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

export function shortAddr(a: string): string {
  return a.length > 12 ? `${a.slice(0, 4)}…${a.slice(-4)}` : a;
}

export const CHAIN_LABEL: Record<Chain, string> = {
  solana: "Solana",
  ethereum: "Ethereum",
  base: "Base",
  bsc: "BSC",
};

export const CHAIN_SHORT: Record<Chain, string> = { solana: "SOL", ethereum: "ETH", base: "BASE", bsc: "BSC" };

export const BADGE_LABEL: Record<Badge, string> = {
  STRONG_ALPHA: "Strong Alpha",
  MODERATE: "Moderate",
  HIGH_RISK: "High Risk",
};

export function scoreTone(score: number): "bull" | "warn" | "bear" {
  if (score >= 80) return "bull";
  if (score >= 60) return "warn";
  return "bear";
}

export function explorerUrl(chain: Chain, address: string): string {
  switch (chain) {
    case "solana":
      return `https://solscan.io/token/${address}`;
    case "ethereum":
      return `https://etherscan.io/token/${address}`;
    case "base":
      return `https://basescan.org/token/${address}`;
    case "bsc":
      return `https://bscscan.com/token/${address}`;
  }
}
