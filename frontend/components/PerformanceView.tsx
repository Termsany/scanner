"use client";

import { ArrowLeft, BarChart3, Info } from "lucide-react";
import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { ago, pct, pctClass, price } from "@/lib/format";
import type { Performance, PerfStats } from "@/lib/types";

import { ChainPill } from "./Badges";
import { ScoreGauge } from "./ScoreGauge";

const WINDOWS = [
  { hours: 24, label: "24h" },
  { hours: 24 * 7, label: "7d" },
  { hours: 24 * 30, label: "30d" },
];

function share(v: number | null) {
  return v === null ? "—" : `${v.toFixed(0)}%`;
}

function Stat({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: string }) {
  return (
    <div className="card px-4 py-3.5">
      <div className="text-[11px] font-medium tracking-[0.14em] text-muted uppercase">{label}</div>
      <div className={`num mt-2 text-2xl font-semibold ${tone ?? ""}`}>{value}</div>
      {sub && <div className="mt-1 text-xs text-faint">{sub}</div>}
    </div>
  );
}

function StatsTable({ title, rows }: { title: string; rows: (PerfStats & { key: string; label: React.ReactNode })[] }) {
  return (
    <section className="card overflow-x-auto">
      <h2 className="border-b border-line px-4 py-3 text-sm font-semibold">{title}</h2>
      <table className="w-full min-w-[640px] text-sm">
        <thead className="text-[10.5px] tracking-[0.12em] text-muted uppercase">
          <tr className="border-b border-line">
            <th className="px-4 py-2 text-left">Group</th>
            <th className="px-3 py-2 text-right">Signals</th>
            <th className="px-3 py-2 text-right">Median 1h</th>
            <th className="px-3 py-2 text-right">Median 6h</th>
            <th className="px-3 py-2 text-right">Median 24h</th>
            <th className="px-3 py-2 text-right">Up at 24h</th>
            <th className="px-3 py-2 text-right">Hit +50%</th>
            <th className="px-3 py-2 text-right">Median drawdown</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key} className="border-b border-line/60">
              <td className="px-4 py-2.5 font-medium">{r.label}</td>
              <td className="num px-3 py-2.5 text-right text-muted">
                {r.n}
                {r.n_24h < r.n && <span className="text-faint"> ({r.n_24h} at 24h)</span>}
              </td>
              <td className={`num px-3 py-2.5 text-right ${pctClass(r.median_1h)}`}>{pct(r.median_1h)}</td>
              <td className={`num px-3 py-2.5 text-right ${pctClass(r.median_6h)}`}>{pct(r.median_6h)}</td>
              <td className={`num px-3 py-2.5 text-right ${pctClass(r.median_24h)}`}>{pct(r.median_24h)}</td>
              <td className="num px-3 py-2.5 text-right">{share(r.positive_24h_pct)}</td>
              <td className="num px-3 py-2.5 text-right text-bull">{share(r.hit_50_pct)}</td>
              <td className="num px-3 py-2.5 text-right text-bear">{pct(r.median_trough)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

export function PerformanceView() {
  const [hours, setHours] = useState(24 * 7);
  const [data, setData] = useState<Performance | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

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

  return (
    <main className="mx-auto max-w-[1400px] px-4 pt-5 pb-12 sm:px-6">
      <header className="flex flex-wrap items-center gap-3">
        <a href="/" className="inline-flex items-center gap-1 rounded-md border border-line px-2 py-1 text-xs text-muted hover:bg-raised hover:text-ink">
          <ArrowLeft className="size-3.5" /> Scanner
        </a>
        <BarChart3 className="size-5 text-accent" />
        <h1 className="text-lg font-semibold tracking-tight">Signal performance</h1>
        <div role="radiogroup" aria-label="Window" className="ml-auto flex rounded-lg border border-line bg-carbon/60 p-0.5">
          {WINDOWS.map((w) => (
            <button
              key={w.hours}
              role="radio"
              aria-checked={hours === w.hours}
              onClick={() => setHours(w.hours)}
              className={`num rounded-md px-2.5 py-1 text-xs ${hours === w.hours ? "bg-raised text-ink" : "text-muted hover:text-ink"}`}
            >
              {w.label}
            </button>
          ))}
        </div>
      </header>

      <p className="mt-3 flex items-start gap-1.5 text-xs text-faint">
        <Info className="mt-px size-3.5 shrink-0" />
        Price change after each signal, from the pool&apos;s 15-minute candles (GeckoTerminal), including tokens that
        crashed and left the scanner. Signals are measured when they are 1h, 6h and 24h old. Peaks are highs, not
        executed trades.
      </p>

      {error ? (
        <p className="card mt-6 p-6 text-sm text-bear">{error}</p>
      ) : !data || !o ? (
        <div className="mt-6 grid grid-cols-2 gap-3 lg:grid-cols-4">
          {[0, 1, 2, 3].map((i) => (
            <span key={i} className="skeleton h-24" />
          ))}
        </div>
      ) : data.measured === 0 ? (
        <p className="card mt-6 p-6 text-sm text-muted">
          No signal in this window is old enough to measure yet (the first measurement happens 1 hour after a signal).
        </p>
      ) : (
        <>
          <section className="mt-6 grid grid-cols-2 gap-3 lg:grid-cols-4">
            <Stat label="Measured" value={`${data.measured}`} sub={`of ${data.signals_in_window} signals · ${o.n_24h} with a 24h result`} />
            <Stat
              label="Median change"
              value={pct(o.median_24h ?? o.median_6h)}
              tone={pctClass(o.median_24h ?? o.median_6h)}
              sub={`1h ${pct(o.median_1h)} · 6h ${pct(o.median_6h)} · 24h ${pct(o.median_24h)}`}
            />
            <Stat label="Hit +50%" value={share(o.hit_50_pct)} tone="text-bull" sub={`median peak ${pct(o.median_peak)}`} />
            <Stat
              label="Up after 24h"
              value={share(o.positive_24h_pct ?? o.positive_6h_pct)}
              sub={`after 6h: ${share(o.positive_6h_pct)} · median drawdown ${pct(o.median_trough)}`}
            />
          </section>

          <div className="mt-4 grid gap-4">
            <StatsTable
              title="By Alpha Score"
              rows={data.by_score.map((b) => ({ ...b, key: b.label, label: `Score ${b.label}` }))}
            />
            <StatsTable
              title="By chain"
              rows={data.by_chain.map((c) => ({ ...c, key: c.chain, label: <ChainPill chain={c.chain} /> }))}
            />

            <section className="card overflow-x-auto">
              <h2 className="border-b border-line px-4 py-3 text-sm font-semibold">Recent signals</h2>
              <table className="w-full min-w-[820px] text-sm">
                <thead className="text-[10.5px] tracking-[0.12em] text-muted uppercase">
                  <tr className="border-b border-line">
                    <th className="px-4 py-2 text-left">Token</th>
                    <th className="px-3 py-2 text-right">Signalled</th>
                    <th className="px-3 py-2 text-center">Score</th>
                    <th className="px-3 py-2 text-right">Entry</th>
                    <th className="px-3 py-2 text-right">1h</th>
                    <th className="px-3 py-2 text-right">6h</th>
                    <th className="px-3 py-2 text-right">24h</th>
                    <th className="px-3 py-2 text-right">Peak</th>
                    <th className="px-3 py-2 text-right">Drawdown</th>
                  </tr>
                </thead>
                <tbody>
                  {data.recent.map((r, i) => (
                    <tr key={`${r.token_address}-${i}`} className="border-b border-line/60 hover:bg-white/[0.025]">
                      <td className="px-4 py-2">
                        <a href={`/token/${r.chain}/${r.token_address}`} className="flex items-center gap-1.5 hover:underline">
                          <span className="font-semibold">${r.symbol}</span>
                          <ChainPill chain={r.chain} />
                        </a>
                      </td>
                      <td className="num px-3 py-2 text-right text-muted">{ago(r.signal_time, now)}</td>
                      <td className="px-3 py-1 text-center">
                        <ScoreGauge score={r.score} size={30} />
                      </td>
                      <td className="num px-3 py-2 text-right">{price(r.entry_price)}</td>
                      <td className={`num px-3 py-2 text-right ${pctClass(r.r_1h)}`}>{pct(r.r_1h)}</td>
                      <td className={`num px-3 py-2 text-right ${pctClass(r.r_6h)}`}>{pct(r.r_6h)}</td>
                      <td className={`num px-3 py-2 text-right ${pctClass(r.r_24h)}`}>{pct(r.r_24h)}</td>
                      <td className="num px-3 py-2 text-right text-bull">
                        {pct(r.peak_pct)}
                        {r.peak_minutes != null && (
                          <span className="block text-[10px] text-faint">
                            after {r.peak_minutes >= 60 ? `${Math.round(r.peak_minutes / 60)}h` : `${r.peak_minutes}m`}
                          </span>
                        )}
                      </td>
                      <td className="num px-3 py-2 text-right text-bear">{pct(r.trough_pct)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          </div>
        </>
      )}
    </main>
  );
}
