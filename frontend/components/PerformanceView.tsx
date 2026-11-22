"use client";

import { BarChart3, Gauge, Info, ListOrdered } from "lucide-react";
import { useEffect, useState } from "react";

import { AppNav } from "@/components/AppNav";
import { Empty, PageHeader, pageMain, Panel, SectionTabs, Segmented, StatTile } from "@/components/ui";
import { api } from "@/lib/api";
import { ago, pct, pctClass, price } from "@/lib/format";
import type { Performance, PerfRow, PerfStats } from "@/lib/types";

import { ChainPill } from "./Badges";
import { ScoreGauge } from "./ScoreGauge";

const WINDOWS = [
  { hours: 24, label: "24h" },
  { hours: 24 * 7, label: "7d" },
  { hours: 24 * 30, label: "30d" },
];

function share(v: number | null | undefined) {
  return v == null ? "—" : `${v.toFixed(0)}%`;
}

type Metric = "median_24h" | "hit_50_pct" | "positive_24h_pct" | "median_peak" | "median_trough";
const METRICS: { value: Metric; label: string; kind: "pct" | "share" }[] = [
  { value: "median_24h", label: "Median 24h", kind: "pct" },
  { value: "hit_50_pct", label: "Hit +50%", kind: "share" },
  { value: "positive_24h_pct", label: "Up at 24h", kind: "share" },
  { value: "median_peak", label: "Peak", kind: "pct" },
  { value: "median_trough", label: "Drawdown", kind: "pct" },
];

type Row = PerfStats & { key: string; label: React.ReactNode };

/** Groups as bars for the chosen metric; click a row for all its numbers. */
function MetricBars({ title, icon, rows }: { title: string; icon: React.ReactNode; rows: Row[] }) {
  const [metric, setMetric] = useState<Metric>("median_24h");
  const [open, setOpen] = useState<string | null>(null);
  const m = METRICS.find((x) => x.value === metric)!;
  const val = (r: Row): number | null => r[metric] ?? null;
  const vals = rows.map(val).filter((v): v is number => v != null);
  const max = Math.max(1, ...vals.map((v) => Math.abs(v)));
  const diverging = m.kind === "pct";
  return (
    <Panel icon={icon} title={title}
      actions={<Segmented label="Metric" value={metric} onChange={setMetric}
        options={METRICS.map((x) => ({ value: x.value, label: x.label }))} className="flex-wrap" />}>
      {rows.length === 0 ? <Empty>No signals measured in this window.</Empty> : (
        <ul className="flex flex-col gap-1">
          {rows.map((r) => {
            const v = val(r);
            const w = v == null ? 0 : (Math.abs(v) / max) * (diverging ? 50 : 100);
            const good = v != null && metric !== "median_trough" && v >= 0;
            const isOpen = open === r.key;
            return (
              <li key={r.key}>
                <button type="button" onClick={() => setOpen(isOpen ? null : r.key)} aria-expanded={isOpen}
                  className={`grid w-full grid-cols-[110px_1fr_72px] items-center gap-3 rounded-lg px-2 py-2 text-left text-sm transition hover:bg-raised/60 sm:grid-cols-[140px_1fr_90px] ${isOpen ? "bg-raised/60" : ""}`}>
                  <span className="flex min-w-0 items-center gap-2 font-medium">
                    {r.label}
                    <span className="num text-[11px] font-normal text-faint">{r.n}</span>
                  </span>
                  <span className="relative h-2.5 overflow-hidden rounded-full bg-raised">
                    {diverging && <span className="absolute inset-y-0 left-1/2 w-px bg-line-strong" />}
                    <span
                      className={`absolute inset-y-0 rounded-full transition-all duration-700 ${good ? "bg-mint" : "bg-bear"}`}
                      style={diverging
                        ? (v ?? 0) >= 0 ? { left: "50%", width: `${w}%` } : { right: "50%", width: `${w}%` }
                        : { left: 0, width: `${w}%` }} />
                  </span>
                  <span className={`num text-right text-sm ${m.kind === "pct" ? pctClass(v) : metric === "hit_50_pct" ? "text-bull" : ""}`}>
                    {m.kind === "pct" ? pct(v) : share(v)}
                  </span>
                </button>
                {isOpen && (
                  <div className="num mx-2 mb-2 grid animate-fade-in grid-cols-2 gap-x-6 gap-y-1 rounded-lg border border-line bg-carbon/50 px-3 py-2 text-xs text-muted sm:grid-cols-4">
                    <span>signals <b className="font-medium text-ink">{r.n}</b>{r.n_24h < r.n ? ` (${r.n_24h} at 24h)` : ""}</span>
                    <span>1h <b className={`font-medium ${pctClass(r.median_1h)}`}>{pct(r.median_1h)}</b></span>
                    <span>6h <b className={`font-medium ${pctClass(r.median_6h)}`}>{pct(r.median_6h)}</b></span>
                    <span>24h <b className={`font-medium ${pctClass(r.median_24h)}`}>{pct(r.median_24h)}</b></span>
                    <span>up at 24h <b className="font-medium text-ink">{share(r.positive_24h_pct)}</b></span>
                    <span>hit +50% <b className="font-medium text-bull">{share(r.hit_50_pct)}</b></span>
                    <span>peak <b className="font-medium text-bull">{pct(r.median_peak)}</b></span>
                    <span>drawdown <b className="font-medium text-bear">{pct(r.median_trough)}</b></span>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}

type Tab = "overview" | "signals";
type MinScore = 0 | 75 | 80 | 90;
type Outcome = "all" | "hit50" | "up" | "down";
type SortKey = "time" | "score" | "r_24h" | "peak_pct" | "trough_pct";

function sortValue(r: PerfRow, k: SortKey): number {
  if (k === "time") return new Date(r.signal_time).getTime();
  return r[k] ?? -Infinity;
}

export function PerformanceView() {
  const [hours, setHours] = useState(24 * 7);
  const [data, setData] = useState<Performance | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [tab, setTabState] = useState<Tab>("overview");
  const [minScore, setMinScore] = useState<MinScore>(0);
  const [outcome, setOutcome] = useState<Outcome>("all");
  const [sort, setSort] = useState<SortKey>("time");
  const [desc, setDesc] = useState(true);

  useEffect(() => {
    const read = () => setTabState(window.location.hash === "#signals" ? "signals" : "overview");
    read();
    window.addEventListener("hashchange", read);
    return () => window.removeEventListener("hashchange", read);
  }, []);
  const setTab = (t: Tab) => {
    setTabState(t);
    history.replaceState(null, "", t === "overview" ? window.location.pathname : "#signals");
  };

  useEffect(() => {
    const ctl = new AbortController();
    setData(null);
    api
      .performance(hours, ctl.signal)
      .then((d) => {
        setData(d);
        setError(null);
        setNow(Date.now());
      })
      .catch((e) => e.name !== "AbortError" && setError("Could not load performance data."));
    return () => ctl.abort();
  }, [hours]);

  const o = data?.overall;
  const recent = (data?.recent ?? [])
    .filter((r) => r.score >= minScore)
    .filter((r) => {
      const end = r.r_24h ?? r.r_6h;
      if (outcome === "hit50") return (r.peak_pct ?? -1) >= 50;
      if (outcome === "up") return end != null && end > 0;
      if (outcome === "down") return end != null && end <= 0;
      return true;
    })
    .sort((a, b) => {
      const x = sortValue(a, sort), y = sortValue(b, sort);
      const c = x < y ? -1 : x > y ? 1 : 0;
      return desc ? -c : c;
    });
  const th = (k: SortKey, label: string, cls = "text-right") => (
    <th className={cls}>
      <button type="button" className="uppercase hover:text-ink"
        onClick={() => (k === sort ? setDesc(!desc) : (setSort(k), setDesc(true)))}>
        {label}{sort === k ? (desc ? " ↓" : " ↑") : ""}
      </button>
    </th>
  );

  return (
    <>
    <AppNav kind="public" />
    <main className={pageMain}>
      <PageHeader icon={<BarChart3 />} title="Signal performance"
        actions={<Segmented label="Window" value={hours} onChange={setHours}
          options={WINDOWS.map((w) => ({ value: w.hours, label: w.label }))} />} />

      <p className="-mt-2 mb-5 flex items-start gap-1.5 text-xs text-faint">
        <Info className="mt-px size-3.5 shrink-0" />
        Price change after each signal, from the pool&apos;s 15-minute candles (GeckoTerminal), including tokens that
        crashed and left the scanner. Signals are measured when they are 1h, 6h and 24h old. Peaks are highs, not
        executed trades.
      </p>

      {error ? (
        <p className="card p-6 text-sm text-bear">{error}</p>
      ) : !data || !o ? (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          {[0, 1, 2, 3].map((i) => <span key={i} className="skeleton h-24 rounded-2xl" />)}
        </div>
      ) : data.measured === 0 ? (
        <p className="card p-6 text-sm text-muted">
          No signal in this window is old enough to measure yet (the first measurement happens 1 hour after a signal).
        </p>
      ) : (
        <>
          <section className="mb-6 grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
            <StatTile label="Measured" value={`${data.measured}`} sub={`of ${data.signals_in_window} signals · ${o.n_24h} with a 24h result`} />
            <StatTile label="Median change" value={pct(o.median_24h ?? o.median_6h)} tone={pctClass(o.median_24h ?? o.median_6h)}
              sub={`1h ${pct(o.median_1h)} · 6h ${pct(o.median_6h)} · 24h ${pct(o.median_24h)}`} />
            <StatTile label="Hit +50%" value={share(o.hit_50_pct)} tone="text-bull" sub={`median peak ${pct(o.median_peak)}`} />
            <StatTile label="Up after 24h" value={share(o.positive_24h_pct ?? o.positive_6h_pct)}
              sub={`after 6h: ${share(o.positive_6h_pct)} · median drawdown ${pct(o.median_trough)}`} />
          </section>

          <SectionTabs label="Performance sections" value={tab} onChange={setTab}
            tabs={[
              { key: "overview", label: "By score & chain", icon: <Gauge /> },
              { key: "signals", label: `Signals · ${data.recent.length}`, icon: <ListOrdered /> },
            ]} />

          <div key={tab} className="flex animate-fade-in flex-col gap-6">
            {tab === "overview" ? (
              <div className="grid gap-6 xl:grid-cols-2">
                <MetricBars title="By Alpha Score" icon={<Gauge />}
                  rows={data.by_score.map((b) => ({ ...b, key: b.label, label: `Score ${b.label}` }))} />
                <MetricBars title="By chain" icon={<BarChart3 />}
                  rows={data.by_chain.map((c) => ({ ...c, key: c.chain, label: <ChainPill chain={c.chain} /> }))} />
              </div>
            ) : (
              <Panel icon={<ListOrdered />} title="Signals" meta={`${recent.length} shown · click a column to sort`} flush
                actions={
                  <>
                    <Segmented label="Score" value={minScore} onChange={setMinScore}
                      options={[{ value: 0, label: "All" }, { value: 75, label: "75+" }, { value: 80, label: "80+" }, { value: 90, label: "90+" }]} />
                    <Segmented label="Outcome" value={outcome} onChange={setOutcome}
                      options={[{ value: "all", label: "All" }, { value: "hit50", label: "Hit +50%" }, { value: "up", label: "Up" }, { value: "down", label: "Down" }]} />
                  </>
                }>
                {recent.length === 0 ? <Empty>No signals match.</Empty> : (
                  <div className="overflow-x-auto">
                    <table className="data-table min-w-[820px]">
                      <thead>
                        <tr>
                          <th className="text-left">Token</th>
                          {th("time", "Signalled")}
                          {th("score", "Score", "text-center")}
                          <th className="text-right">Entry</th>
                          <th className="text-right">1h</th>
                          <th className="text-right">6h</th>
                          {th("r_24h", "24h")}
                          {th("peak_pct", "Peak")}
                          {th("trough_pct", "Drawdown")}
                        </tr>
                      </thead>
                      <tbody>
                        {recent.map((r, i) => (
                          <tr key={`${r.token_address}-${i}`}>
                            <td>
                              <a href={`/token/${r.chain}/${r.token_address}`} className="flex items-center gap-1.5 hover:text-mint">
                                <span className="font-semibold">${r.symbol}</span>
                                <ChainPill chain={r.chain} />
                              </a>
                            </td>
                            <td className="num text-right text-muted">{ago(r.signal_time, now)}</td>
                            <td className="px-3 py-1 text-center"><ScoreGauge score={r.score} size={30} /></td>
                            <td className="num text-right">{price(r.entry_price)}</td>
                            <td className={`num text-right ${pctClass(r.r_1h)}`}>{pct(r.r_1h)}</td>
                            <td className={`num text-right ${pctClass(r.r_6h)}`}>{pct(r.r_6h)}</td>
                            <td className={`num text-right ${pctClass(r.r_24h)}`}>{pct(r.r_24h)}</td>
                            <td className="num text-right text-bull">
                              {pct(r.peak_pct)}
                              {r.peak_minutes != null && (
                                <span className="block text-[10px] text-faint">
                                  after {r.peak_minutes >= 60 ? `${Math.round(r.peak_minutes / 60)}h` : `${r.peak_minutes}m`}
                                </span>
                              )}
                            </td>
                            <td className="num text-right text-bear">{pct(r.trough_pct)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </Panel>
            )}
          </div>
        </>
      )}
    </main>
    </>
  );
}
