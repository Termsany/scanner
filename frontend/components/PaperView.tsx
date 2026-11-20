"use client";

import { ArrowLeft, FlaskConical, Info } from "lucide-react";
import { useEffect, useState } from "react";

import { AppNav } from "@/components/AppNav";
import { api } from "@/lib/api";
import { ago, pct, pctClass, price } from "@/lib/format";
import type { PaperSummary } from "@/lib/types";

import { ChainPill } from "./Badges";

const WINDOWS = [
  { hours: 24, label: "24h" },
  { hours: 24 * 7, label: "7d" },
  { hours: 24 * 30, label: "30d" },
];

const REASON: Record<string, string> = {
  take_profit: "Take profit",
  stop_loss: "Stop loss",
  time: "Time limit",
  leader_sold: "Wallet sold",
  no_price: "No price",
};

function money(v: number | null | undefined) {
  if (v == null) return "—";
  const s = Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${v >= 0 ? "+" : "−"}$${s}`;
}

function hold(m: number) {
  return m >= 60 ? `${Math.round(m / 60)}h` : `${m}m`;
}

export function PaperView() {
  const [hours, setHours] = useState(24 * 7);
  const [data, setData] = useState<PaperSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const ctl = new AbortController();
    const load = () =>
      api
        .paper(hours, ctl.signal)
        .then((d) => {
          setData(d);
          setError(null);
          setNow(Date.now());
        })
        .catch((e) => e.name !== "AbortError" && setError("Could not load paper-trading data."));
    setData(null);
    load();
    const id = setInterval(load, 60_000);
    return () => {
      clearInterval(id);
      ctl.abort();
    };
  }, [hours]);

  return (
    <>
    <AppNav kind="public" />
    <main className="mx-auto max-w-[1400px] px-4 pt-5 pb-12 sm:px-6">
      <header className="flex flex-wrap items-center gap-3">
        <FlaskConical className="size-5 text-accent" />
        <h1 className="text-lg font-semibold tracking-tight">Paper trading</h1>
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
        <span>
          Simulated trades only, no real money.
          {data && ` Each trade is $${data.size_usd.toFixed(0)} and pays ${data.slippage_pct.toFixed(1)}% slippage on entry and again on exit.`}{" "}
          Prices come from DexScreener every minute, so exits can land past the target.
        </span>
      </p>

      {data && data.mirror_wallets.length > 0 && (
        <p className="num mt-2 text-xs text-muted">
          Copying: {data.mirror_wallets.map((w) => `${w.slice(0, 4)}…${w.slice(-4)}`).join(", ")}
        </p>
      )}

      {error ? (
        <p className="card mt-6 p-6 text-sm text-bear">{error}</p>
      ) : !data ? (
        <div className="mt-6 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {[0, 1, 2, 3].map((i) => (
            <span key={i} className="skeleton h-40" />
          ))}
        </div>
      ) : (
        <>
          <section className="mt-6 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {data.strategies.map((s) => (
              <div key={s.name} className="card px-4 py-3.5">
                <div className="num text-[11px] font-medium tracking-[0.14em] text-muted uppercase">{s.name}</div>
                <div className="mt-1 text-xs text-faint">{s.title}</div>
                <div className={`num mt-3 text-2xl font-semibold ${pctClass(s.realized_usd || null)}`}>{money(s.realized_usd)}</div>
                <div className="num mt-1 text-xs text-muted">
                  {s.closed} closed · win {s.win_rate == null ? "—" : `${(s.win_rate * 100).toFixed(0)}%`} · return {pct(s.return_pct)}
                </div>
                <div className="num mt-1 text-xs text-muted">
                  {s.open} open · unrealized <span className={pctClass(s.unrealized_usd || null)}>{money(s.unrealized_usd)}</span>
                </div>
                <div className="num mt-2 text-[11px] text-faint">
                  {s.mirror
                    ? `Exits with the wallet · max ${hold(s.max_hold_minutes)}`
                    : `TP +${s.tp_pct}% · SL ${s.sl_pct}% · max ${hold(s.max_hold_minutes)}`}
                </div>
              </div>
            ))}
          </section>

          <section className="card mt-4 overflow-x-auto">
            <h2 className="border-b border-line px-4 py-3 text-sm font-semibold">Positions</h2>
            {data.positions.length === 0 ? (
              <p className="px-4 py-6 text-sm text-muted">
                No paper trades yet. The first one opens on the next tracked-wallet buy or 80+ signal.
              </p>
            ) : (
              <table className="w-full min-w-[960px] text-sm">
                <thead className="text-[10.5px] tracking-[0.12em] text-muted uppercase">
                  <tr className="border-b border-line">
                    <th className="px-4 py-2 text-left">Token</th>
                    <th className="px-3 py-2 text-left">Strategy</th>
                    <th className="px-3 py-2 text-left">Trigger</th>
                    <th className="px-3 py-2 text-right">Opened</th>
                    <th className="px-3 py-2 text-right">Entry</th>
                    <th className="px-3 py-2 text-right">Now / exit</th>
                    <th className="px-3 py-2 text-right">Peak</th>
                    <th className="px-3 py-2 text-right">P&amp;L</th>
                    <th className="px-3 py-2 text-left">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {data.positions.map((p) => (
                    <tr key={p.id} className="border-b border-line/60 hover:bg-white/[0.025]">
                      <td className="px-4 py-2">
                        <a href={`/token/${p.chain}/${p.token_address}`} className="flex items-center gap-1.5 hover:underline">
                          <span className="font-semibold">${p.symbol || p.token_address.slice(0, 6)}</span>
                          <ChainPill chain={p.chain} />
                        </a>
                      </td>
                      <td className="num px-3 py-2 text-xs text-muted">{p.strategy}</td>
                      <td className="max-w-[220px] truncate px-3 py-2 text-xs text-muted" title={p.trigger}>
                        {p.trigger}
                      </td>
                      <td className="num px-3 py-2 text-right text-muted">{ago(p.entry_time, now)}</td>
                      <td className="num px-3 py-2 text-right">{price(p.entry_price)}</td>
                      <td className="num px-3 py-2 text-right">{price(p.status === "open" ? p.last_price : p.exit_price)}</td>
                      <td className="num px-3 py-2 text-right text-bull">{pct(p.peak_pct)}</td>
                      <td className={`num px-3 py-2 text-right ${pctClass(p.pnl_usd)}`}>
                        {money(p.pnl_usd)}
                        <span className="block text-[10px]">{pct(p.pnl_pct)}</span>
                      </td>
                      <td className="px-3 py-2 text-xs">
                        {p.status === "open" ? (
                          <span className="rounded-full border border-accent/40 px-2 py-0.5 text-accent">
                            Open{p.remaining_pct < 100 ? ` · ${p.remaining_pct.toFixed(0)}% left` : ""}
                          </span>
                        ) : (
                          <span className="text-muted">{REASON[p.exit_reason ?? ""] ?? p.exit_reason}</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        </>
      )}
    </main>
    </>
  );
}
