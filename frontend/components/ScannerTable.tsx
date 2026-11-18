"use client";

import { ArrowDown, ArrowUp, ChevronLeft, ChevronRight, PanelRightOpen, RefreshCw, Search, Users } from "lucide-react";

import { age, pct, pctClass, price, usd } from "@/lib/format";
import { CHAINS, type Chain, type PaginatedTokens, type SortField, type TokenQuery, type TokenRef } from "@/lib/types";

import { ChainPill, SecurityBadges, TokenAvatar } from "./Badges";
import { ScoreGauge } from "./ScoreGauge";

interface Props {
  data: PaginatedTokens | null;
  query: TokenQuery;
  onQuery: (q: Partial<TokenQuery>) => void;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  onOpen: (t: TokenRef) => void;
  selected: TokenRef | null;
  flash: Set<string>;
  now: number;
}

const SCORE_OPTIONS = [0, 60, 75, 80];
const LIQ_OPTIONS = [0, 10_000, 50_000, 250_000, 1_000_000];
const ALIGN = { left: "text-left", right: "text-right", center: "text-center" } as const;

function SortHeader({
  field,
  query,
  onQuery,
  children,
  align = "right",
}: {
  field: SortField;
  query: TokenQuery;
  onQuery: Props["onQuery"];
  children: React.ReactNode;
  align?: "left" | "right" | "center";
}) {
  const active = query.sort === field;
  const desc = query.order !== "asc";
  return (
    <th scope="col" aria-sort={active ? (desc ? "descending" : "ascending") : "none"} className={`px-3 py-2.5 ${ALIGN[align]}`}>
      <button
        onClick={() => onQuery({ sort: field, order: active && desc ? "asc" : "desc", page: 1 })}
        className={`inline-flex items-center gap-1 uppercase transition-colors hover:text-ink ${active ? "text-ink" : ""}`}
      >
        {children}
        {active ? (desc ? <ArrowDown className="size-3" /> : <ArrowUp className="size-3" />) : null}
      </button>
    </th>
  );
}

function Segmented<T extends string | number>({
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
          className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
            value === o.value ? "bg-raised text-ink shadow-sm" : "text-muted hover:text-ink"
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function Select({
  value,
  onChange,
  options,
  label,
}: {
  value: number;
  onChange: (v: number) => void;
  options: { value: number; label: string }[];
  label: string;
}) {
  return (
    <label className="flex items-center gap-2 rounded-lg border border-line bg-carbon/60 px-2.5 py-1 text-xs text-muted">
      {label}
      <select
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="num cursor-pointer bg-transparent text-ink outline-none"
      >
        {options.map((o) => (
          <option key={o.value} value={o.value} className="bg-surface">
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}

export function ScannerTable({ data, query, onQuery, loading, error, onRetry, onOpen, selected, flash, now }: Props) {
  const page = query.page ?? 1;
  const pageSize = query.page_size ?? 50;
  const totalPages = data ? Math.max(1, Math.ceil(data.total / pageSize)) : 1;

  return (
    <section className="card flex min-w-0 flex-col" aria-label="Alpha scanner">
      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3">
        <h2 className="mr-2 text-sm font-semibold tracking-wide">Alpha Scanner</h2>
        <Segmented<Chain | "all">
          label="Chain"
          value={query.chain ?? "all"}
          onChange={(v) => onQuery({ chain: v === "all" ? undefined : v, page: 1 })}
          options={[{ value: "all", label: "All" }, ...CHAINS.map((c) => ({ value: c, label: c === "ethereum" ? "ETH" : c === "solana" ? "SOL" : c.toUpperCase() }))]}
        />
        <Select
          label="Score ≥"
          value={query.min_score ?? 0}
          onChange={(v) => onQuery({ min_score: v || undefined, page: 1 })}
          options={SCORE_OPTIONS.map((v) => ({ value: v, label: v ? String(v) : "Any" }))}
        />
        <Select
          label="Liq ≥"
          value={query.min_liquidity ?? 0}
          onChange={(v) => onQuery({ min_liquidity: v || undefined, page: 1 })}
          options={LIQ_OPTIONS.map((v) => ({ value: v, label: v ? usd(v) : "Any" }))}
        />
        <label className="relative ml-auto flex min-w-[180px] flex-1 items-center sm:max-w-[260px]">
          <Search className="pointer-events-none absolute left-2.5 size-3.5 text-faint" />
          <input
            type="search"
            placeholder="Symbol, name or address"
            defaultValue={query.q ?? ""}
            onChange={(e) => onQuery({ q: e.target.value.trim() || undefined, page: 1 })}
            className="w-full rounded-lg border border-line bg-carbon/60 py-1.5 pr-3 pl-8 text-xs text-ink placeholder:text-faint outline-none focus:border-line-strong"
            aria-label="Search tokens"
          />
        </label>
      </div>

      {/* Table */}
      <div className="relative min-h-[320px] overflow-x-auto">
        {loading && data && <div className="absolute inset-x-0 top-0 h-px animate-pulse bg-bull/60" aria-hidden />}
        <table className="w-full min-w-[1080px] border-collapse text-sm">
          <thead className="sticky top-0 z-10 bg-surface text-[10.5px] font-medium tracking-[0.12em] text-muted">
            <tr className="border-b border-line">
              <th scope="col" className="px-4 py-2.5 text-left uppercase">Token</th>
              <SortHeader field="age" query={query} onQuery={onQuery}>Age</SortHeader>
              <th scope="col" className="px-3 py-2.5 text-right uppercase">Price</th>
              <th scope="col" className="px-3 py-2.5 text-right uppercase">5m</th>
              <SortHeader field="change_1h" query={query} onQuery={onQuery}>1h</SortHeader>
              <th scope="col" className="px-3 py-2.5 text-right uppercase">24h</th>
              <SortHeader field="liquidity" query={query} onQuery={onQuery}>Liquidity</SortHeader>
              <SortHeader field="volume" query={query} onQuery={onQuery}>Volume</SortHeader>
              <th scope="col" className="px-3 py-2.5 text-left uppercase">Security</th>
              <SortHeader field="score" query={query} onQuery={onQuery} align="center">Score</SortHeader>
              <th scope="col" className="w-12 px-3 py-2.5"><span className="sr-only">Inspect</span></th>
            </tr>
          </thead>
          <tbody>
            {error && !data ? (
              <tr>
                <td colSpan={11} className="px-4 py-16 text-center">
                  <p className="text-sm text-bear">{error}</p>
                  <p className="mt-1 text-xs text-faint">
                    Start the backend with <code className="num text-muted">uvicorn app.main:app --port 8000</code>
                  </p>
                  <button
                    onClick={onRetry}
                    className="mt-4 inline-flex items-center gap-1.5 rounded-lg border border-line px-3 py-1.5 text-xs hover:bg-raised"
                  >
                    <RefreshCw className="size-3.5" /> Retry
                  </button>
                </td>
              </tr>
            ) : !data ? (
              Array.from({ length: 10 }).map((_, i) => (
                <tr key={i} className="border-b border-line/60">
                  <td className="px-4 py-3"><div className="flex items-center gap-3"><span className="skeleton size-8 rounded-full" /><span className="skeleton h-4 w-28" /></div></td>
                  {Array.from({ length: 10 }).map((__, j) => (
                    <td key={j} className="px-3 py-3"><span className="skeleton ml-auto block h-4 w-14" /></td>
                  ))}
                </tr>
              ))
            ) : data.items.length === 0 ? (
              <tr>
                <td colSpan={11} className="px-4 py-16 text-center text-sm text-muted">
                  No tokens match these filters yet. The scanner refreshes every cycle.
                </td>
              </tr>
            ) : (
              data.items.map((t) => {
                const key = `${t.chain}:${t.address}`;
                const isSel = selected?.chain === t.chain && selected?.address === t.address;
                return (
                  <tr
                    key={key}
                    onClick={() => onOpen({ chain: t.chain, address: t.address })}
                    className={`group cursor-pointer border-b border-line/60 transition-colors hover:bg-white/[0.025] ${
                      isSel ? "bg-white/[0.04]" : ""
                    } ${flash.has(key) ? "animate-flash" : ""}`}
                  >
                    <td className="px-4 py-2.5">
                      <div className="flex min-w-0 items-center gap-3">
                        <TokenAvatar symbol={t.symbol} image={t.image_url} />
                        <div className="min-w-0">
                          <div className="flex items-center gap-1.5">
                            <span className="max-w-[140px] truncate font-semibold">{t.symbol || "?"}</span>
                            <ChainPill chain={t.chain} />
                            {t.smart_money_hits > 0 && (
                              <span title={`${t.smart_money_hits} tracked smart wallet(s)`} className="text-accent">
                                <Users className="size-3.5" />
                              </span>
                            )}
                          </div>
                          <div className="max-w-[190px] truncate text-xs text-faint">{t.name}</div>
                        </div>
                      </div>
                    </td>
                    <td className="num px-3 py-2.5 text-right text-muted">{age(t.pair_created_at, now)}</td>
                    <td className="num px-3 py-2.5 text-right">{price(t.price_usd)}</td>
                    <td className={`num px-3 py-2.5 text-right ${pctClass(t.price_change_m5)}`}>{pct(t.price_change_m5)}</td>
                    <td className={`num px-3 py-2.5 text-right ${pctClass(t.price_change_h1)}`}>{pct(t.price_change_h1)}</td>
                    <td className={`num px-3 py-2.5 text-right ${pctClass(t.price_change_h24)}`}>{pct(t.price_change_h24)}</td>
                    <td className="num px-3 py-2.5 text-right">{usd(t.liquidity_usd)}</td>
                    <td className="px-3 py-2.5 text-right whitespace-nowrap">
                      <div className="num">{usd(t.volume_h24)}</div>
                      <div className="num text-[11px] text-faint">1h {usd(t.volume_h1)}</div>
                    </td>
                    <td className="px-3 py-2.5"><SecurityBadges b={t.badges} /></td>
                    <td className="px-3 py-1.5 text-center"><ScoreGauge score={t.alpha_score} size={40} /></td>
                    <td className="px-3 py-2.5 text-right">
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          onOpen({ chain: t.chain, address: t.address });
                        }}
                        aria-label={`Inspect ${t.symbol}`}
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

      {/* Pagination */}
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
