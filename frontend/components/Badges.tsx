import { Coins, Lock, Percent, ShieldAlert, ShieldCheck, Snowflake } from "lucide-react";

import { BADGE_LABEL, CHAIN_SHORT } from "@/lib/format";
import type { Badge, Chain, SecurityBadges as SB } from "@/lib/types";

const CHAIN_DOT: Record<Chain, string> = {
  solana: "bg-[#14f195]",
  ethereum: "bg-[#8c9eff]",
  base: "bg-[#3b82f6]",
  bsc: "bg-[#f3ba2f]",
};

export function ChainPill({ chain }: { chain: Chain }) {
  return (
    <span className="inline-flex items-center gap-1 rounded-md border border-line px-1.5 py-0.5 text-[10px] font-medium tracking-wide text-muted">
      <span className={`size-1.5 rounded-full ${CHAIN_DOT[chain]}`} aria-hidden />
      {CHAIN_SHORT[chain]}
    </span>
  );
}

const BADGE_STYLE: Record<Badge, string> = {
  STRONG_ALPHA: "bg-bull-soft text-bull border-bull/30",
  MODERATE: "bg-warn-soft text-warn border-warn/30",
  HIGH_RISK: "bg-bear-soft text-bear border-bear/30",
};

export function AlphaBadge({ badge, className = "" }: { badge: Badge; className?: string }) {
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-[11px] font-semibold tracking-wider uppercase ${BADGE_STYLE[badge]} ${className}`}
    >
      {BADGE_LABEL[badge]}
    </span>
  );
}

type State = "ok" | "bad" | "unknown";

function stateOf(v: boolean | null): State {
  return v === null ? "unknown" : v ? "ok" : "bad";
}

const PILL: Record<State, string> = {
  ok: "border-bull/25 text-bull bg-bull/[0.06]",
  bad: "border-bear/30 text-bear bg-bear/[0.08]",
  unknown: "border-dashed border-line-strong text-faint",
};

function Pill({ state, title, children }: { state: State; title: string; children: React.ReactNode }) {
  return (
    <span
      title={title}
      className={`inline-flex h-5 items-center gap-1 rounded border px-1.5 text-[10px] font-medium ${PILL[state]}`}
    >
      {children}
    </span>
  );
}

/** Compact security pills for the scanner table. */
export function SecurityBadges({ b }: { b: SB }) {
  if (b.honeypot) {
    return (
      <Pill state="bad" title="Honeypot: token cannot be sold">
        <ShieldAlert className="size-3" /> HONEYPOT
      </Pill>
    );
  }
  const tax = b.buy_tax === null && b.sell_tax === null ? null : Math.max(b.buy_tax ?? 0, b.sell_tax ?? 0);
  const taxState: State = tax === null ? "unknown" : tax <= 5 ? "ok" : "bad";
  const describe = (v: boolean | null, yes: string, no: string) => (v === null ? "Unknown" : v ? yes : no);
  return (
    <div className="flex flex-nowrap gap-1">
      <Pill
        state={b.honeypot === null ? "unknown" : "ok"}
        title={b.honeypot === null ? "Honeypot check: unknown" : "Not a honeypot: token can be sold"}
      >
        <ShieldCheck className="size-3" /> Safe
      </Pill>
      <Pill state={stateOf(b.lp_locked)} title={`LP lock: ${describe(b.lp_locked, "> 80% locked/burned", "< 80% locked")}`}>
        <Lock className="size-3" /> LP
      </Pill>
      <Pill state={stateOf(b.no_mint)} title={`Mint: ${describe(b.no_mint, "renounced", "mintable")}`}>
        <Coins className="size-3" /> Mint
      </Pill>
      <Pill state={stateOf(b.no_freeze)} title={`Freeze: ${describe(b.no_freeze, "no freeze authority", "freeze authority active")}`}>
        <Snowflake className="size-3" /> Frz
      </Pill>
      <Pill
        state={taxState}
        title={tax === null ? "Tax unknown" : `Buy ${b.buy_tax ?? 0}% / Sell ${b.sell_tax ?? 0}%`}
      >
        <Percent className="size-3" />
        {tax === null ? "Tax" : <span className="num">{tax % 1 === 0 ? tax : tax.toFixed(1)}</span>}
      </Pill>
    </div>
  );
}

export function TokenAvatar({ symbol, image, size = 32 }: { symbol: string; image?: string | null; size?: number }) {
  if (image) {
    // eslint-disable-next-line @next/next/no-img-element
    return (
      <img
        src={image}
        alt=""
        width={size}
        height={size}
        loading="lazy"
        className="shrink-0 rounded-full bg-raised object-cover ring-1 ring-line"
        style={{ width: size, height: size }}
        onError={(e) => ((e.currentTarget as HTMLImageElement).style.visibility = "hidden")}
      />
    );
  }
  const hue = [...symbol].reduce((h, ch) => (h * 31 + ch.charCodeAt(0)) % 360, 7);
  return (
    <span
      className="grid shrink-0 place-items-center rounded-full text-[11px] font-semibold ring-1 ring-line"
      style={{ width: size, height: size, background: `hsl(${hue} 45% 22%)`, color: `hsl(${hue} 80% 80%)` }}
      aria-hidden
    >
      {symbol.slice(0, 2).toUpperCase() || "?"}
    </span>
  );
}
