"use client";

import {
  ArrowDownWideNarrow,
  ArrowUpNarrowWide,
  Check,
  ChevronLeft,
  ChevronRight,
  Copy,
  PanelRightOpen,
  RefreshCw,
  Search,
  Users,
} from "lucide-react";
import { useState } from "react";

import { BADGE_LABEL, age, pct, pctClass, price, scoreTone, shortAddr, usd } from "@/lib/format";
import { CHAINS, type Chain, type PaginatedTokens, type SortField, type TokenQuery, type TokenRef } from "@/lib/types";

import { ChainPill, SecurityBadges, TokenAvatar } from "./Badges";
import { ScoreGauge } from "./ScoreGauge";

export type RowFlash = "up" | "down" | "signal";

interface Props {
  data: PaginatedTokens | null;
  query: TokenQuery;
  onQuery: (q: Partial<TokenQuery>) => void;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  onOpen: (t: TokenRef) => void;
  selected: TokenRef | null;
  flash: Map<string, RowFlash>;
  now: number;
}

const CHAIN_LABEL: Record<Chain | "all", string> = { all: "All", solana: "Solana", ethereum: "Ethereum", base: "Base", bsc: "BSC" };
const SCORE_PILLS: { value: number; label: string }[] = [
  { value: 0, label: "All" },
  { value: 60, label: "60+" },
  { value: 80, label: "80+ Strong" },
];
const LIQ_OPTIONS = [0, 10_000, 50_000, 100_000];
const SORTS: { value: SortField; label: string }[] = [
  { value: "score", label: "Alpha Score" },
  { value: "volume_1h", label: "1h Volume" },
  { value: "liquidity", label: "Liquidity" },
  { value: "change_1h", label: "1h Change %" },
  { value: "age", label: "Age" },
];
const TONE_TEXT = { bull: "text-bull", warn: "text-warn", bear: "text-bear" } as const;
const FLASH_CLASS: Record<RowFlash, string> = { up: "flash-up", down: "flash-down", signal: "animate-flash" };

function Pills<T extends string | number>({
  value,
  options,
  onChange,
  label,
}: {
  value: T;
  options: { value: T; label: string }[];
  onChange: (v: T) => void;
  label: string;
}) {
  return (
    <div role="radiogroup" aria-label={label} className="flex rounded-lg border border-line bg-carbon/60 p-0.5">
      {options.map((o) => (
        <button
          key={String(o.value)}
          role="radio"
          aria-checked={value === o.value}
          onClick={() => onChange(o.value)}
          className={`rounded-md px-2.5 py-1 text-xs font-medium whitespace-nowrap transition-colors ${
            value === o.value ? "bg-raised text-ink shadow-sm" : "text-muted hover:text-ink"
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function CopyAddress({ address }: { address: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      onClick={async (e) => {
        e.stopPropagation();
        try {
          await navigator.clipboard.writeText(address);
          setDone(true);
          setTimeout(() => setDone(false), 1200);
        } catch {
          /* clipboard unavailable */
        }
      }}
      title={`Copy ${address}`}
      aria-label={`Copy contract address ${address}`}
      className="num inline-flex shrink-0 items-center gap-1 rounded px-0.5 text-[11px] whitespace-nowrap text-faint hover:bg-raised hover:text-ink"
    >
      {shortAddr(address)}
      {done ? <Check className="size-3 text-bull" /> : <Copy className="size-2.5" />}
    </button>
  );
}

/** 1h volume / liquidity: optimal 0.25-2x (scoring_engine M1); >6x looks like wash trading. */
function VolLiqBar({ vol, liq }: { vol: number | null; liq: number | null }) {
  if (!vol || !liq) return <span className="text-[10px] text-faint">V/L —</span>;
  const r = vol / liq;
  const tone = r >= 0.25 && r <= 2 ? "bg-bull" : r < 0.25 ? "bg-faint" : r <= 6 ? "bg-warn" : "bg-bear";
  const width = Math.min(100, (Math.log10(Math.max(r, 0.01)) + 2) / 3 * 100); // 0.01x..10x on a log scale
  return (
    <span className="flex items-center justify-end gap-1.5" title={`1h volume is ${r.toFixed(2)}x liquidity (optimal 0.25–2x)`}>
      <span className="h-1 w-10 overflow-hidden rounded-full bg-white/[0.06]">
        <span className={`block h-full rounded-full ${tone}`} style={{ width: `${width}%` }} />
      </span>
      <span className="num text-[10.5px] text-faint">{r.toFixed(2)}x</span>
    </span>
  );
}

export function ScannerTable({ data, query, onQuery, loading, error, onRetry, onOpen, selected, flash, now }: Props) {
  const page = query.page ?? 1;
  const pageSize = query.page_size ?? 50;
  const totalPages = data ? Math.max(1, Math.ceil(data.total / pageSize)) : 1;
  const desc = query.order !== "asc";

  return (
    <section className="card flex min-w-0 flex-col" aria-label="Alpha scanner">
      {/* Command toolbar */}
      <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3">
        <Pills<Chain | "all">
          label="Chain"
          value={query.chain ?? "all"}
          onChange={(v) => onQuery({ chain: v === "all" ? undefined : v, page: 1 })}
          options={(["all", ...CHAINS] as const).map((c) => ({ value: c, label: CHAIN_LABEL[c] }))}
        />
        <Pills<number>
          label="Minimum Alpha Score"
          value={query.min_score ?? 0}
          onChange={(v) => onQuery({ min_score: v || undefined, page: 1 })}
          options={SCORE_PILLS}
        />
        <label className="flex items-center gap-2 rounded-lg border border-line bg-carbon/60 px-2.5 py-1 text-xs text-muted">
          Liq
          <select
            value={query.min_liquidity ?? 0}
            onChange={(e) => onQuery({ min_liquidity: Number(e.target.value) || undefined, page: 1 })}
            className="num cursor-pointer bg-transparent text-ink outline-none"
            aria-label="Minimum liquidity"
          >
            {LIQ_OPTIONS.map((v) => (
              <option key={v} value={v} className="bg-surface">
                {v ? `${usd(v)}+` : "All"}
              </option>
            ))}
          </select>
        </label>
        <div className="flex items-center rounded-lg border border-line bg-carbon/60 text-xs text-muted">
          <label className="flex items-center gap-2 py-1 pr-1 pl-2.5">
            Sort
            <select
              value={query.sort ?? "score"}
              onChange={(e) => onQuery({ sort: e.target.value as SortField, page: 1 })}
              className="cursor-pointer bg-transparent text-ink outline-none"
              aria-label="Sort by"
            >
              {SORTS.map((s) => (
                <option key={s.value} value={s.value} className="bg-surface">
                  {s.label}
                </option>
              ))}
            </select>
          </label>
          <button
            onClick={() => onQuery({ order: desc ? "asc" : "desc", page: 1 })}
            className="grid size-7 place-items-center border-l border-line hover:text-ink"
            aria-label={desc ? "Sorted descending; switch to ascending" : "Sorted ascending; switch to descending"}
            title={query.sort === "age" ? (desc ? "Youngest first" : "Oldest first") : desc ? "Highest first" : "Lowest first"}
          >
            {desc ? <ArrowDownWideNarrow className="size-3.5" /> : <ArrowUpNarrowWide className="size-3.5" />}
          </button>
        </div>
        <label className="relative ml-auto flex min-w-[180px] flex-1 items-center sm:max-w-[280px]">
          <Search className="pointer-events-none absolute left-2.5 size-3.5 text-faint" />
          <input
            type="search"
            placeholder="Symbol, name or contract"
            defaultValue={query.q ?? ""}
            onChange={(e) => onQuery({ q: e.target.value.trim() || undefined, page: 1 })}
            className="w-full rounded-lg border border-line bg-carbon/60 py-1.5 pr-3 pl-8 text-xs text-ink outline-none placeholder:text-faint focus:border-line-strong"
            aria-label="Search tokens"
          />
        </label>
      </div>

      {/* Grid */}
      <div className="relative max-h-[calc(100dvh-260px)] min-h-[320px] overflow-auto">
        {loading && data && <div className="absolute inset-x-0 top-0 z-20 h-px animate-pulse bg-bull/60" aria-hidden />}
        <table className="w-full min-w-[1040px] border-collapse text-sm">
          <thead className="sticky top-0 z-10 bg-surface text-[10.5px] font-medium tracking-[0.12em] text-muted uppercase">
            <tr className="border-b border-line">
              <th scope="col" className="px-4 py-2.5 text-left">Token</th>
              <th scope="col" className="px-3 py-2.5 text-right">Age</th>
              <th scope="col" className="px-3 py-2.5 text-right">Price</th>
              <th scope="col" className="px-3 py-2.5 text-right">5m</th>
              <th scope="col" className="px-3 py-2.5 text-right">1h</th>
              <th scope="col" className="px-3 py-2.5 text-right">Liquidity</th>
              <th scope="col" className="px-3 py-2.5 text-right">Vol 1h</th>
              <th scope="col" className="px-3 py-2.5 text-left">Security</th>
              <th scope="col" className="px-3 py-2.5 text-center">Alpha</th>
              <th scope="col" className="w-12 px-3 py-2.5"><span className="sr-only">Inspect</span></th>
            </tr>
          </thead>
          <tbody>
            {error && !data ? (
              <tr>
                <td colSpan={10} className="px-4 py-16 text-center">
                  <p className="text-sm text-bear">{error}</p>
                  <p className="mt-1 text-xs text-faint">
                    Start the backend with <code className="num text-muted">uvicorn app.main:app --port 8000</code>
                  </p>
                  <button onClick={onRetry} className="mt-4 inline-flex items-center gap-1.5 rounded-lg border border-line px-3 py-1.5 text-xs hover:bg-raised">
                    <RefreshCw className="size-3.5" /> Retry
                  </button>
                </td>
              </tr>
            ) : !data ? (
              Array.from({ length: 10 }).map((_, i) => (
                <tr key={i} className="border-b border-line/60">
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-3">
                      <span className="skeleton size-8 rounded-full" />
                      <span className="skeleton h-4 w-28" />
                    </div>
                  </td>
                  {Array.from({ length: 9 }).map((__, j) => (
                    <td key={j} className="px-3 py-3"><span className="skeleton ml-auto block h-4 w-14" /></td>
                  ))}
                </tr>
              ))
            ) : data.items.length === 0 ? (
              <tr>
                <td colSpan={10} className="px-4 py-16 text-center text-sm text-muted">
                  No tokens match these filters yet. The scanner refreshes every cycle.
                </td>
              </tr>
            ) : (
              data.items.map((t) => {
                const key = `${t.chain}:${t.address}`;
                const isSel = selected?.chain === t.chain && selected?.address === t.address;
                const f = flash.get(key);
                const tone = scoreTone(t.alpha_score);
                return (
                  <tr
                    key={key}
                    onClick={() => onOpen({ chain: t.chain, address: t.address })}
                    className={`group cursor-pointer border-b border-line/60 transition-colors hover:bg-white/[0.025] ${
                      isSel ? "bg-white/[0.04]" : ""
                    } ${f ? FLASH_CLASS[f] : ""}`}
                  >
                    <td className="px-4 py-2">
                      <div className="flex min-w-0 items-center gap-3">
                        <TokenAvatar symbol={t.symbol} image={t.image_url} />
                        <div className="min-w-0">
                          <div className="flex items-center gap-1.5">
                            <span className="max-w-[130px] truncate font-semibold">{t.symbol || "?"}</span>
                            <ChainPill chain={t.chain} />
                            {t.smart_money_hits > 0 && (
                              <span title={`${t.smart_money_hits} tracked smart wallet(s) hold this token`} className="text-cyan">
                                <Users className="size-3.5" />
                              </span>
                            )}
                          </div>
                          <div className="flex items-center gap-1.5">
                            <span className="max-w-[110px] truncate text-xs text-faint">{t.name}</span>
                            <CopyAddress address={t.address} />
                          </div>
                        </div>
                      </div>
                    </td>
                    <td className="num px-3 py-2 text-right text-muted">{age(t.pair_created_at, now)}</td>
                    <td className="num px-3 py-2 text-right">{price(t.price_usd)}</td>
                    <td className={`num px-3 py-2 text-right ${pctClass(t.price_change_m5)}`}>{pct(t.price_change_m5)}</td>
                    <td className={`num px-3 py-2 text-right ${pctClass(t.price_change_h1)}`}>{pct(t.price_change_h1)}</td>
                    <td className="num px-3 py-2 text-right">{usd(t.liquidity_usd)}</td>
                    <td className="px-3 py-2 text-right whitespace-nowrap">
                      <div className="num">{usd(t.volume_h1)}</div>
                      <VolLiqBar vol={t.volume_h1} liq={t.liquidity_usd} />
                    </td>
                    <td className="px-3 py-2"><SecurityBadges b={t.badges} /></td>
                    <td className="px-3 py-1.5">
                      <div className="flex flex-col items-center gap-0.5">
                        <ScoreGauge score={t.alpha_score} size={38} />
                        <span className={`text-[9px] font-semibold tracking-wider uppercase ${TONE_TEXT[tone]}`}>
                          {tone === "bull" ? "Strong" : tone === "warn" ? "Moderate" : "Weak"}
                        </span>
                      </div>
                    </td>
                    <td className="px-3 py-2 text-right">
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          onOpen({ chain: t.chain, address: t.address });
                        }}
                        aria-label={`Inspect ${t.symbol}`}
                        title={`Inspect ${t.symbol} · ${BADGE_LABEL[t.badge]}`}
                        className="grid size-8 place-items-center rounded-lg border border-line text-muted transition-colors group-hover:border-line-strong group-hover:text-ink hover:bg-raised"
                      >
                        <PanelRightOpen className="size-4" />
                      </button>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {data && data.total > 0 && (
        <div className="flex items-center justify-between border-t border-line px-4 py-2.5 text-xs text-muted">
          <span className="num">
            {(page - 1) * pageSize + 1}–{Math.min(page * pageSize, data.total)} of {data.total}
          </span>
          <div className="flex items-center gap-1">
            <button
              disabled={page <= 1}
              onClick={() => onQuery({ page: page - 1 })}
              className="grid size-7 place-items-center rounded-md border border-line enabled:hover:bg-raised disabled:opacity-30"
              aria-label="Previous page"
            >
              <ChevronLeft className="size-4" />
            </button>
            <span className="num px-2">
              {page} / {totalPages}
            </span>
            <button
              disabled={page >= totalPages}
              onClick={() => onQuery({ page: page + 1 })}
              className="grid size-7 place-items-center rounded-md border border-line enabled:hover:bg-raised disabled:opacity-30"
              aria-label="Next page"
            >
              <ChevronRight className="size-4" />
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
