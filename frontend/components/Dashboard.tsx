"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "@/lib/api";
import { pct, price } from "@/lib/format";
import { useLiveSignals } from "@/lib/useLiveSignals";
import type { FeedSignal, LiveSignal, PaginatedTokens, ScannerStatus, TokenQuery, TokenRef } from "@/lib/types";

import { ChainPill } from "./Badges";
import { Radar } from "lucide-react";

import { AppNav } from "./AppNav";
import { PageHeader, pageMain } from "./ui";
import { KpiBar, TelemetryBar } from "./KpiBar";
import { LiveFeed } from "./LiveFeed";
import { type RowFlash, ScannerTable } from "./ScannerTable";
import { ScoreGauge } from "./ScoreGauge";
import { SmartWalletsPanel } from "./SmartWalletsPanel";
import { TokenInspector } from "./TokenInspector";

const DEFAULT_QUERY: TokenQuery = { sort: "score", order: "desc", page: 1, page_size: 50 };
const FALLBACK_REFRESH_MS = 60_000;
const FLASH_MS = 2_600;

type RowState = { score: number; price: number | null };

export function Dashboard({ initialToken = null }: { initialToken?: TokenRef | null }) {
  const [query, setQuery] = useState<TokenQuery>(DEFAULT_QUERY);
  const [debouncedQuery, setDebouncedQuery] = useState<TokenQuery>(DEFAULT_QUERY);
  const [tokens, setTokens] = useState<PaginatedTokens | null>(null);
  const [tokensError, setTokensError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState<ScannerStatus | null>(null);
  const [selected, setSelected] = useState<TokenRef | null>(initialToken);
  const [refreshKey, setRefreshKey] = useState(0);
  const [flash, setFlash] = useState<Map<string, RowFlash>>(new Map());
  const [toasts, setToasts] = useState<LiveSignal[]>([]);
  const [feedMin, setFeedMin] = useState(80);
  const [history, setHistory] = useState<FeedSignal[]>([]);
  const [fresh, setFresh] = useState<Set<number>>(new Set());
  const [now, setNow] = useState(() => Date.now());

  // ---- flash helpers --------------------------------------------------------
  const flashRows = useCallback((entries: [string, RowFlash][]) => {
    if (!entries.length) return;
    setFlash((prev) => new Map([...prev, ...entries]));
    setTimeout(
      () =>
        setFlash((prev) => {
          const next = new Map(prev);
          for (const [k, v] of entries) if (next.get(k) === v) next.delete(k);
          return next;
        }),
      FLASH_MS,
    );
  }, []);

  // Debounce free-text search; apply other filter changes immediately.
  useEffect(() => {
    const delay = query.q !== debouncedQuery.q ? 300 : 0;
    const t = setTimeout(() => setDebouncedQuery(query), delay);
    return () => clearTimeout(t);
  }, [query, debouncedQuery.q]);

  // ---- tokens: refetch; on periodic refreshes, diff against the previous rows -----
  const tokenCtl = useRef<AbortController | null>(null);
  const lastRows = useRef<Map<string, RowState>>(new Map());
  const loadTokens = useCallback(
    async (q: TokenQuery, diff = false) => {
      tokenCtl.current?.abort();
      const ctl = new AbortController();
      tokenCtl.current = ctl;
      setLoading(true);
      try {
        const data = await api.tokens(q, ctl.signal);
        const nextRows = new Map(data.items.map((t) => [`${t.chain}:${t.address}`, { score: t.alpha_score, price: t.price_usd }]));
        if (diff) {
          const changes: [string, RowFlash][] = [];
          for (const [k, cur] of nextRows) {
            const prev = lastRows.current.get(k);
            if (!prev) continue;
            const d = cur.score !== prev.score ? cur.score - prev.score : (cur.price ?? 0) - (prev.price ?? 0);
            if (d > 0) changes.push([k, "up"]);
            else if (d < 0) changes.push([k, "down"]);
          }
          flashRows(changes);
        }
        lastRows.current = nextRows;
        setTokens(data);
        setTokensError(null);
      } catch (e) {
        if ((e as Error).name !== "AbortError") setTokensError((e as Error).message || "Failed to load tokens");
      } finally {
        if (tokenCtl.current === ctl) setLoading(false);
      }
    },
    [flashRows],
  );

  const loadStatus = useCallback(async () => {
    try {
      setStatus(await api.status());
    } catch {
      /* ribbon keeps the last known value */
    }
  }, []);

  const feedMinRef = useRef(feedMin);
  feedMinRef.current = feedMin;
  const loadHistory = useCallback(async () => {
    try {
      const rows = await api.signals(feedMinRef.current, 30);
      setHistory(rows.map((r) => ({ ...(r.payload as unknown as LiveSignal), signal_id: r.id, gain_pct: r.gain_pct })));
    } catch {
      /* the WebSocket hello still provides recent signals */
    }
  }, []);

  const debouncedQueryRef = useRef(debouncedQuery);
  debouncedQueryRef.current = debouncedQuery;

  useEffect(() => {
    loadTokens(debouncedQuery);
  }, [debouncedQuery, loadTokens]);

  useEffect(() => {
    loadHistory();
  }, [feedMin, loadHistory]);

  // Fallback refresh in case the socket is down; status also keeps "x ago" honest.
  useEffect(() => {
    loadStatus();
    const a = setInterval(() => loadTokens(debouncedQueryRef.current, true), FALLBACK_REFRESH_MS);
    const b = setInterval(loadStatus, 15_000);
    const c = setInterval(() => setNow(Date.now()), 5_000);
    return () => {
      clearInterval(a);
      clearInterval(b);
      clearInterval(c);
    };
  }, [loadTokens, loadStatus]);

  // ---- live stream ---------------------------------------------------------------
  const onSignal = useCallback(
    (s: LiveSignal) => {
      flashRows([[`${s.chain}:${s.address}`, "signal"]]);
      if (s.signal_id) {
        const id = s.signal_id;
        setFresh((prev) => new Set(prev).add(id));
        setTimeout(() => setFresh((prev) => {
          const next = new Set(prev);
          next.delete(id);
          return next;
        }), FLASH_MS);
      }
      if (s.score >= feedMinRef.current) {
        setToasts((prev) => [s, ...prev.filter((p) => p.signal_id !== s.signal_id)].slice(0, 3));
        setTimeout(() => setToasts((prev) => prev.filter((p) => p !== s)), 7_000);
      }
    },
    [flashRows],
  );

  const onScanComplete = useCallback(() => {
    loadTokens(debouncedQueryRef.current, true);
    loadStatus();
    loadHistory();
    setRefreshKey((k) => k + 1);
  }, [loadTokens, loadStatus, loadHistory]);

  const { state: socket, feed } = useLiveSignals({ minScore: feedMin, onSignal, onScanComplete });

  // Merge REST history (has gain since signal) with live pushes; newest first, de-duplicated.
  const visibleFeed = useMemo(() => {
    const byId = new Map<string, FeedSignal>();
    for (const s of [...history, ...feed]) {
      const k = s.signal_id ? `id:${s.signal_id}` : `${s.chain}:${s.address}:${s.created_at}`;
      byId.set(k, { ...byId.get(k), ...s, gain_pct: (s as FeedSignal).gain_pct ?? byId.get(k)?.gain_pct ?? null });
    }
    return [...byId.values()]
      .filter((s) => s.score >= feedMin)
      .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
      .slice(0, 40);
  }, [history, feed, feedMin]);

  // ---- inspector + shareable URL (/token/{chain}/{address}) without a route transition --------
  const openToken = useCallback((t: TokenRef) => {
    setSelected(t);
    window.history.replaceState(null, "", `/token/${t.chain}/${encodeURIComponent(t.address)}`);
  }, []);
  const closeToken = useCallback(() => {
    setSelected(null);
    window.history.replaceState(null, "", "/scanner");
  }, []);

  const updateQuery = useCallback((q: Partial<TokenQuery>) => setQuery((prev) => ({ ...prev, ...q })), []);

  return (
    <>
      <AppNav kind="public" />

      <main className={pageMain}>
        <PageHeader icon={<Radar />} title="Token scanner"
          subtitle="New tokens scored live: security, momentum and smart money. A signal fires at 75." />
        <TelemetryBar status={status} socket={socket} now={now} />
        <KpiBar status={status} now={now} onOpenToken={openToken} />

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
          <aside className="xl:sticky xl:top-[68px] xl:self-start" aria-label="Signals and wallets">
            <LiveFeed
              feed={visibleFeed}
              fresh={fresh}
              socket={socket}
              minScore={feedMin}
              onMinScore={setFeedMin}
              onOpen={openToken}
              now={now}
            />
            <SmartWalletsPanel refreshKey={refreshKey} />
          </aside>
        </div>
      </main>

      {/* Toasts */}
      <div className="pointer-events-none fixed top-[68px] right-4 z-30 flex w-[300px] flex-col gap-2" aria-live="assertive">
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
    </>
  );
}
