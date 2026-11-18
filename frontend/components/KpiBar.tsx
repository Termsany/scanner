import { Activity, Radar, Trophy, Zap } from "lucide-react";

import { ago, pct } from "@/lib/format";
import type { SocketState } from "@/lib/useLiveSignals";
import type { ScannerStatus, TokenRef } from "@/lib/types";

interface Props {
  status: ScannerStatus | null;
  socket: SocketState;
  now: number;
  onOpenToken: (t: TokenRef) => void;
}

function Kpi({
  icon,
  label,
  children,
  sub,
}: {
  icon: React.ReactNode;
  label: string;
  children: React.ReactNode;
  sub?: React.ReactNode;
}) {
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

export function KpiBar({ status, socket, now, onOpenToken }: Props) {
  const loading = status === null;
  const live = !!status?.live;
  const g = status?.top_gainer;

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
        {loading ? (
          <span className="skeleton h-7 w-12" />
        ) : (
          <span className="num text-2xl font-semibold text-bull">{status.high_alpha_24h}</span>
        )}
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
            className="flex items-baseline gap-2 truncate text-left hover:underline decoration-line-strong underline-offset-4"
          >
            <span className="truncate text-lg font-semibold">${g.symbol}</span>
            <span className={`num text-lg font-semibold ${g.gain_pct >= 0 ? "text-bull" : "text-bear"}`}>
              {pct(g.gain_pct)}
            </span>
          </button>
        ) : (
          <span className="num text-2xl font-semibold text-faint">—</span>
        )}
      </Kpi>

      <Kpi
        icon={<Activity className="size-3.5" />}
        label="Scanner status"
        sub={
          loading
            ? " "
            : `Last cycle ${ago(status.last_run_at, now)}${status.last_run_scored != null ? ` · ${status.last_run_scored} scored` : ""}`
        }
      >
        <span className="relative mr-1 inline-flex size-2.5 self-center">
          {live && <span className="absolute inset-0 animate-pulse-ring rounded-full bg-bull" />}
          <span className={`relative size-2.5 rounded-full ${live ? "bg-bull" : loading ? "bg-faint" : "bg-bear"}`} />
        </span>
        <span className={`text-lg font-semibold ${live ? "text-bull" : loading ? "text-muted" : "text-bear"}`}>
          {loading ? "Connecting" : live ? "Live" : "Stalled"}
        </span>
        <span className="ml-auto self-center text-[11px] text-faint" title="Live signal stream">
          WS {socket === "open" ? "●" : socket === "connecting" ? "◌" : "○"}
        </span>
      </Kpi>
    </section>
  );
}
