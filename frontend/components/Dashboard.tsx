"use client";

import { ScanSearch } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "@/lib/api";
import { pct, price } from "@/lib/format";
import { useLiveSignals } from "@/lib/useLiveSignals";
import type { LiveSignal, PaginatedTokens, ScannerStatus, TokenQuery, TokenRef } from "@/lib/types";

import { ChainPill } from "./Badges";
import { KpiBar } from "./KpiBar";
import { LiveFeed } from "./LiveFeed";
import { ScannerTable } from "./ScannerTable";
import { ScoreGauge } from "./ScoreGauge";
import { TokenInspector } from "./TokenInspector";

const DEFAULT_QUERY: TokenQuery = { sort: "score", order: "desc", page: 1, page_size: 50 };
const FALLBACK_REFRESH_MS = 60_000;

export function Dashboard({ initialToken = null }: { initialToken?: TokenRef | null }) {
  const [query, setQuery] = useState<TokenQuery>(DEFAULT_QUERY);
  const [debouncedQuery, setDebouncedQuery] = useState<TokenQuery>(DEFAULT_QUERY);
  const [tokens, setTokens] = useState<PaginatedTokens | null>(null);
  const [tokensError, setTokensError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState<ScannerStatus | null>(null);
  const [selected, setSelected] = useState<TokenRef | null>(initialToken);
  const [refreshKey, setRefreshKey] = useState(0);
  const [flash, setFlash] = useState<Set<string>>(new Set());
  const [toasts, setToasts] = useState<LiveSignal[]>([]);
  const [feedMin, setFeedMin] = useState(75);
  const [now, setNow] = useState(() => Date.now());

  // Debounce free-text search; apply other filter changes immediately.
  useEffect(() => {
    const delay = query.q !== debouncedQuery.q ? 300 : 0;
    const t = setTimeout(() => setDebouncedQuery(query), delay);
    return () => clearTimeout(t);
  }, [query, debouncedQuery.q]);

  // Tokens
  const tokenCtl = useRef<AbortController | null>(null);
  const loadTokens = useCallback(async (q: TokenQuery) => {
    tokenCtl.current?.abort();
    const ctl = new AbortController();
    tokenCtl.current = ctl;
    setLoading(true);
    try {
      const data = await api.tokens(q, ctl.signal);
      setTokens(data);
      setTokensError(null);
    } catch (e) {
      if ((e as Error).name !== "AbortError") setTokensError((e as Error).message || "Failed to load tokens");
    } finally {
      if (tokenCtl.current === ctl) setLoading(false);
    }
  }, []);

  const loadStatus = useCallback(async () => {
    try {
      setStatus(await api.status());
    } catch {
      /* KPI bar keeps last known value */
    }
  }, []);

  const debouncedQueryRef = useRef(debouncedQuery);
  debouncedQueryRef.current = debouncedQuery;

  useEffect(() => {
    loadTokens(debouncedQuery);
  }, [debouncedQuery, loadTokens]);

  // Fallback refresh in case the socket is down; status also keeps "x ago" honest.
  useEffect(() => {
    loadStatus();
    const a = setInterval(() => loadTokens(debouncedQueryRef.current), FALLBACK_REFRESH_MS);
    const b = setInterval(loadStatus, 15_000);
    const c = setInterval(() => setNow(Date.now()), 5_000);
    return () => {
      clearInterval(a);
      clearInterval(b);
      clearInterval(c);
    };
  }, [loadTokens, loadStatus]);

  // Live stream
  const onSignal = useCallback((s: LiveSignal) => {
    const key = `${s.chain}:${s.address}`;
    setFlash((prev) => new Set(prev).add(key));
    setTimeout(
      () =>
        setFlash((prev) => {
          const next = new Set(prev);
          next.delete(key);
          return next;
        }),
      2_600,
    );
    setToasts((prev) => [s, ...prev.filter((p) => p.signal_id !== s.signal_id)].slice(0, 3));
    setTimeout(() => setToasts((prev) => prev.filter((p) => p !== s)), 7_000);
  }, []);

  const onScanComplete = useCallback(() => {
    loadTokens(debouncedQueryRef.current);
    loadStatus();
    setRefreshKey((k) => k + 1);
  }, [loadTokens, loadStatus]);

  const { state: socket, feed } = useLiveSignals({ minScore: feedMin, onSignal, onScanComplete });
  const visibleFeed = useMemo(() => feed.filter((s) => s.score >= feedMin), [feed, feedMin]);

  // Inspector + shareable URL (/token/{chain}/{address}) without a route transition.
  const openToken = useCallback((t: TokenRef) => {
    setSelected(t);
    window.history.replaceState(null, "", `/token/${t.chain}/${encodeURIComponent(t.address)}`);
  }, []);
  const closeToken = useCallback(() => {
    setSelected(null);
    window.history.replaceState(null, "", "/");
  }, []);

  const updateQuery = useCallback((q: Partial<TokenQuery>) => setQuery((prev) => ({ ...prev, ...q })), []);

  return (
    <div className="mx-auto max-w-[1600px] px-4 pb-10 sm:px-6">
      <header className="flex items-center gap-3 py-5">
        <div className="grid size-9 place-items-center rounded-xl border border-bull/30 bg-bull-soft">
          <ScanSearch className="size-5 text-bull" />
        </div>
        <div>
          <h1 className="text-[15px] font-semibold tracking-tight">Alpha Scanner</h1>
          <p className="text-[11px] text-faint">Solana · Ethereum · Base · BSC — every 60s</p>
        </div>
        <p className="ml-auto hidden text-[11px] text-faint md:block">
          Scores are automated heuristics, not financial advice.
        </p>
      </header>

      <KpiBar status={status} socket={socket} now={now} onOpenToken={openToken} />

      <div className="mt-4 grid gap-4 xl:grid-cols-[minmax(0,1fr)_340px]">
        <ScannerTable
          data={tokens}
          query={query}
          onQuery={updateQuery}
          loading={loading}
          error={tokensError}
          onRetry={() => loadTokens(debouncedQuery)}
          onOpen={openToken}
          selected={selected}
          flash={flash}
          now={now}
        />
        <div className="xl:sticky xl:top-4 xl:self-start">
          <LiveFeed feed={visibleFeed} socket={socket} minScore={feedMin} onMinScore={setFeedMin} onOpen={openToken} now={now} />
        </div>
      </div>

      {/* Toasts */}
      <div className="pointer-events-none fixed top-4 right-4 z-30 flex w-[300px] flex-col gap-2" aria-live="assertive">
        {toasts.map((s) => (
          <button
            key={s.signal_id ?? s.address}
            onClick={() => openToken({ chain: s.chain, address: s.address })}
            className="card pointer-events-auto flex animate-toast-in items-center gap-3 p-3 text-left shadow-xl shadow-black/40 hover:border-line-strong"
          >
            <ScoreGauge score={s.score} size={38} />
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-1.5 text-[10px] font-semibold tracking-[0.14em] text-bull uppercase">
                New signal <ChainPill chain={s.chain} />
              </div>
              <div className="truncate text-sm font-semibold">${s.symbol}</div>
              <div className="num text-[11px] text-muted">
                {price(s.price_usd)} · 1h {pct(s.price_change_h1)}
              </div>
            </div>
          </button>
        ))}
      </div>

      <TokenInspector token={selected} refreshKey={refreshKey} onClose={closeToken} />
    </div>
  );
}
