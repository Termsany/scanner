import { Activity, BarChart3, HeartPulse, Radar, ScanSearch, ShieldCheck, Timer, Trophy, Wifi, WifiOff, Zap } from "lucide-react";

import { ago, pct } from "@/lib/format";
import type { SocketState } from "@/lib/useLiveSignals";
import type { ScannerStatus, TokenRef } from "@/lib/types";

/** Sticky glass telemetry ribbon: brand, scanner pulse, cycle duration, audit coverage, socket state. */
export function TelemetryBar({ status, socket, now }: { status: ScannerStatus | null; socket: SocketState; now: number }) {
  const live = !!status?.live;
  const state = status === null ? "Connecting" : live ? "Running" : "Stalled";
  const cov = status?.security_coverage_pct;
  return (
    <header className="glass sticky top-0 z-30">
      <div className="mx-auto flex max-w-[1600px] flex-wrap items-center gap-x-5 gap-y-2 px-4 py-2.5 sm:px-6">
        <div className="flex items-center gap-2.5">
          <div className="grid size-8 place-items-center rounded-lg border border-bull/30 bg-bull-soft">
            <ScanSearch className="size-4 text-bull" />
          </div>
          <div className="leading-tight">
            <h1 className="text-[14px] font-semibold tracking-tight">Alpha Scanner</h1>
            <p className="text-[10.5px] text-faint">SOL · ETH · BASE · BSC</p>
          </div>
        </div>

        <div className="flex items-center gap-2 text-xs" title="Scanner state">
          <span className="relative inline-flex size-2.5">
            {live && <span className="absolute inset-0 animate-pulse-ring rounded-full bg-bull" />}
            <span className={`relative size-2.5 rounded-full ${live ? "bg-bull" : status ? "bg-bear" : "bg-faint"}`} />
          </span>
          <span className={`font-semibold ${live ? "text-bull" : status ? "text-bear" : "text-muted"}`}>{state}</span>
          {status && <span className="num text-faint">{ago(status.last_run_at, now)}</span>}
        </div>

        <Metric icon={<Timer className="size-3.5" />} label="Cycle">
          {status?.last_cycle_seconds != null ? `${status.last_cycle_seconds.toFixed(1)}s` : "—"}
          <span className="text-faint"> / {status?.scan_interval_seconds ?? 60}s</span>
        </Metric>
        <Metric icon={<ShieldCheck className="size-3.5" />} label="Audit coverage">
          <span className={cov == null ? "" : cov >= 80 ? "text-bull" : cov >= 50 ? "text-warn" : "text-bear"}>
            {cov != null ? `${cov.toFixed(0)}%` : "—"}
          </span>
        </Metric>

        <div className="ml-auto flex items-center gap-3">
          <a href="/performance" className="inline-flex items-center gap-1 rounded-md border border-line px-2 py-1 text-[11px] text-muted hover:bg-raised hover:text-ink">
            <BarChart3 className="size-3.5" /> Performance
          </a>
          <p className="hidden text-[11px] text-faint lg:block">Automated heuristics, not financial advice.</p>
          <span
            className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[10.5px] font-bold tracking-[0.14em] ${
              socket === "open"
                ? "border-bull/30 bg-bull-soft text-bull"
                : "border-warn/30 bg-warn-soft text-warn"
            }`}
            title="Live signal WebSocket"
            role="status"
          >
            {socket === "open" ? <Wifi className="size-3" /> : <WifiOff className="size-3" />}
            {socket === "open" ? "LIVE" : socket === "connecting" ? "CONNECTING" : "RECONNECTING"}
          </span>
        </div>
      </div>
    </header>
  );
}

function Metric({ icon, label, children }: { icon: React.ReactNode; label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-1.5 text-xs text-muted" title={label}>
      {icon}
      <span className="hidden sm:inline">{label}</span>
      <span className="num text-ink">{children}</span>
    </div>
  );
}

function Kpi({ icon, label, children, sub }: { icon: React.ReactNode; label: string; children: React.ReactNode; sub?: React.ReactNode }) {
  return (
    <div className="card relative overflow-hidden px-4 py-3.5">
      <div className="flex items-center gap-2 text-[11px] font-medium tracking-[0.14em] text-muted uppercase">
        {icon}
        {label}
      </div>
      <div className="mt-2 flex items-baseline gap-2">{children}</div>
      {sub && <div className="mt-1 truncate text-xs text-faint">{sub}</div>}
    </div>
  );
}

export function KpiBar({ status, now, onOpenToken }: { status: ScannerStatus | null; now: number; onOpenToken: (t: TokenRef) => void }) {
  const loading = status === null;
  const g = status?.top_gainer;
  const errors = status?.last_run_errors ?? 0;
  // Warnings (e.g. security audits deferred to the next cycle by rate limits) are normal; only a failed
  // cycle is unhealthy. A stalled scanner is shown in the telemetry ribbon.
  const healthy = !!status?.last_run_ok;

  return (
    <section aria-label="Scanner KPIs" className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <Kpi icon={<Radar className="size-3.5" />} label="Active tokens" sub="Scanned in the last hour">
        {loading ? <span className="skeleton h-7 w-16" /> : <span className="num text-2xl font-semibold">{status.active_tokens}</span>}
      </Kpi>

      <Kpi
        icon={<Zap className="size-3.5 text-bull" />}
        label="High-alpha 24h"
        sub={loading ? " " : `Score ≥ 80 · ${status.signals_24h} signals total`}
      >
        {loading ? <span className="skeleton h-7 w-12" /> : <span className="num text-2xl font-semibold text-bull">{status.high_alpha_24h}</span>}
      </Kpi>

      <Kpi
        icon={<Trophy className="size-3.5 text-warn" />}
        label="Top gainer caught"
        sub={g ? `since signal ${ago(g.signalled_at, now)}` : loading ? " " : "No signals in the last 24h"}
      >
        {loading ? (
          <span className="skeleton h-7 w-28" />
        ) : g ? (
          <button
            onClick={() => onOpenToken({ chain: g.chain, address: g.address })}
            className="flex items-baseline gap-2 truncate text-left decoration-line-strong underline-offset-4 hover:underline"
          >
            <span className="truncate text-lg font-semibold">${g.symbol}</span>
            <span className={`num text-lg font-semibold ${g.gain_pct >= 0 ? "text-bull" : "text-bear"}`}>{pct(g.gain_pct)}</span>
          </button>
        ) : (
          <span className="num text-2xl font-semibold text-faint">—</span>
        )}
      </Kpi>

      <Kpi
        icon={<HeartPulse className="size-3.5" />}
        label="Latest scan health"
        sub={
          loading
            ? " "
            : `${status.last_run_candidates ?? "—"} candidates · ${errors} ${errors === 1 ? "warning" : "warnings"}`
        }
      >
        {loading ? (
          <span className="skeleton h-7 w-24" />
        ) : (
          <>
            <Activity className={`size-4 self-center ${healthy ? "text-bull" : "text-bear"}`} />
            <span className={`text-lg font-semibold ${healthy ? "text-bull" : "text-bear"}`}>
              {healthy ? "Healthy" : status.last_run_ok === null ? "No runs" : "Failing"}
            </span>
            <span className="num text-sm text-muted">{status.last_run_scored ?? 0} scored</span>
          </>
        )}
      </Kpi>
    </section>
  );
}
