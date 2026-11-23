"use client";

import { Activity, FlaskConical, History, Info, RefreshCw, RotateCcw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { adminCredential, adminWho } from "@/components/AdminAuth";
import { AppNav } from "@/components/AppNav";
import { Badge, button, Empty, PageHeader, pageMain, Panel, SectionTabs, Segmented } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { ago, pct, pctClass, price } from "@/lib/format";
import type { PaperPositionOut, PaperStrategy, PaperSummary } from "@/lib/types";

import { ChainPill } from "./Badges";

const WINDOWS = [
  { hours: 24, label: "24h" },
  { hours: 24 * 7, label: "7d" },
  { hours: 24 * 30, label: "30d" },
];
const REFRESH_MS = 3_000;      // live: the server caches 2 s, the engine re-prices open trades every few seconds

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

type Tab = "open" | "history";

export function PaperView() {
  const [hours, setHours] = useState(24 * 7);
  const [data, setData] = useState<PaperSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [loading, setLoading] = useState(false);
  const [tab, setTabState] = useState<Tab>("open");
  const [strategy, setStrategy] = useState("");   // "" = every strategy
  const [admin, setAdmin] = useState(false);       // the Reset buttons are for the admin only
  const [note, setNote] = useState("");
  useEffect(() => {
    adminWho().then(() => setAdmin(true)).catch(() => setAdmin(false));
  }, []);

  useEffect(() => {
    const read = () => setTabState(window.location.hash === "#history" ? "history" : "open");
    read();
    window.addEventListener("hashchange", read);
    return () => window.removeEventListener("hashchange", read);
  }, []);
  const setTab = (t: Tab) => {
    setTabState(t);
    history.replaceState(null, "", t === "open" ? window.location.pathname : "#history");
  };

  const load = useCallback(async (h: number, signal?: AbortSignal, quiet = false) => {
    if (!quiet) setLoading(true);       // the 3 s refresh updates in place, without the loading state
    try {
      setData(await api.paper(h, signal));
      setError(null);
      setNow(Date.now());
    } catch (e) {
      if ((e as Error).name !== "AbortError" && !quiet) setError("Could not load paper-trading data.");
    } finally {
      if (!quiet) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const ctl = new AbortController();
    setData(null);
    load(hours, ctl.signal);
    let busy = false;                   // never stack requests when one is slow
    const refresh = async () => {
      if (busy || document.hidden) return;
      busy = true;
      try {
        await load(hours, ctl.signal, true);
      } finally {
        busy = false;
      }
    };
    const id = setInterval(refresh, REFRESH_MS);
    const onVisible = () => { if (!document.hidden) refresh(); };   // back on the tab: refresh at once
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
      ctl.abort();
    };
  }, [hours, load]);

  const [tick, setTick] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setTick(Date.now()), 1_000);
    return () => clearInterval(id);
  }, []);

  const mine = (data?.positions ?? []).filter((p) => !strategy || p.strategy === strategy);
  const open = mine.filter((p) => p.status === "open");
  const closed = mine.filter((p) => p.status !== "open");
  const strat = Object.fromEntries((data?.strategies ?? []).map((s) => [s.name, s]));

  return (
    <>
    <AppNav kind="public" />
    <main className={pageMain}>
      <PageHeader icon={<FlaskConical />} title="Paper trading"
        badges={<Badge tone="gold">SIMULATED</Badge>}
        subtitle={data && (
          <span className="inline-flex items-center gap-2">
            <span className={`size-1.5 rounded-full ${tick - now < REFRESH_MS * 2 ? "animate-pulse bg-mint" : "bg-warn"}`} />
            updated {Math.max(0, Math.round((tick - now) / 1000))}s ago
          </span>
        )}
        actions={
          <>
            <Segmented label="Window" value={hours} onChange={setHours}
              options={WINDOWS.map((w) => ({ value: w.hours, label: w.label }))} />
            <button type="button" className={button("secondary")} disabled={loading} onClick={() => load(hours)}
              aria-label="Refresh">
              <RefreshCw className={`size-3.5 ${loading ? "animate-spin" : ""}`} />
            </button>
          </>
        } />

      <p className="-mt-2 mb-5 flex items-start gap-1.5 text-xs text-faint">
        <Info className="mt-px size-3.5 shrink-0" />
        <span>
          Simulated trades only, no real money.
          {data && ` Each trade is $${data.size_usd.toFixed(0)} and pays ${data.slippage_pct.toFixed(1)}% slippage on entry and again on exit.`}
          {data && data.mirror_wallets.length > 0 && (
            <> Copying <span className="num">{data.mirror_wallets.map((w) => `${w.slice(0, 4)}…${w.slice(-4)}`).join(", ")}</span>.</>
          )}
        </span>
      </p>

      {error ? (
        <p className="card p-6 text-sm text-bear">{error}</p>
      ) : !data ? (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          {[0, 1, 2, 3].map((i) => <span key={i} className="skeleton h-40 rounded-2xl" />)}
        </div>
      ) : (
        <>
          {/* strategies: click one to see only its trades */}
          <section className="mb-6 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {data.strategies.map((s) => (
              <StrategyCard key={s.name} s={s} on={strategy === s.name}
                onClick={() => setStrategy(strategy === s.name ? "" : s.name)}
                onReset={admin ? async () => {
                  if (!confirm(`Start ${s.name} from zero? Its older trades stay saved but stop counting here.`)) return;
                  try {
                    await api.paperReset(adminCredential(), s.name);
                    setNote(`${s.name} starts from zero now`);
                    await load(hours);
                  } catch (e) {
                    setNote(e instanceof ApiError ? e.message : "Could not reset it");
                  }
                } : undefined} />
            ))}
          </section>

          {note && (
            <p className="-mt-3 mb-4 flex items-center gap-2 text-xs text-mint">
              {note}
              <button type="button" className="text-muted hover:text-ink" onClick={() => setNote("")} aria-label="Close">×</button>
            </p>
          )}
          <SectionTabs label="Paper sections" value={tab} onChange={setTab}
            tabs={[
              { key: "open", label: `Open trades · ${open.length}`, icon: <Activity /> },
              { key: "history", label: `History · ${closed.length}`, icon: <History /> },
            ]} />

          {strategy && (
            <div className="-mt-3 mb-4 flex items-center gap-2 text-xs text-muted">
              Showing <Badge tone="mint">{strategy}</Badge> only
              <button type="button" className="text-muted underline-offset-2 hover:text-ink hover:underline" onClick={() => setStrategy("")}>
                show all
              </button>
            </div>
          )}

          <div key={tab} className="animate-fade-in">
            {tab === "open" ? (
              open.length === 0 ? (
                <div className="card"><Empty>No open paper trades. The next one opens on a tracked-wallet buy or an 80+ signal.</Empty></div>
              ) : (
                <div className="grid items-start gap-3 md:grid-cols-2 xl:grid-cols-3">
                  {open.map((p) => <OpenCard key={p.id} p={p} s={strat[p.strategy]} now={tick} />)}
                </div>
              )
            ) : (
              <HistoryPanel rows={closed} now={tick} />
            )}
          </div>
        </>
      )}
    </main>
    </>
  );
}

function StrategyCard({ s, on, onClick, onReset }: {
  s: PaperStrategy;
  on: boolean;
  onClick: () => void;
  onReset?: () => void;
}) {
  const total = s.realized_usd + s.unrealized_usd;
  return (
    <div role="button" tabIndex={0} onClick={onClick} aria-pressed={on}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), onClick())}
      className={`card group relative cursor-pointer overflow-hidden p-5 text-left transition duration-300 hover:-translate-y-0.5 ${
        on ? "border-mint/60 shadow-[0_0_0_1px_rgb(43_228_167/0.35)]" : "hover:border-line-strong"}`}>
      <span className={`absolute inset-x-0 top-0 h-[2px] ${total >= 0 ? "bg-gradient-to-r from-mint/0 via-mint/70 to-mint/0" : "bg-gradient-to-r from-bear/0 via-bear/70 to-bear/0"}`} />
      <div className="flex items-center justify-between gap-2">
        <span className="num text-[11px] font-semibold tracking-[0.14em] text-muted uppercase">{s.name}</span>
        <span className="flex items-center gap-1.5">
          {on && <Badge tone="mint">filter</Badge>}
          {onReset && (
            <button type="button" title="Start this strategy from zero" aria-label={`Reset ${s.name}`}
              onClick={(e) => { e.stopPropagation(); onReset(); }}
              className="inline-flex items-center gap-1 rounded-md border border-line px-1.5 py-0.5 text-[10px] text-muted hover:border-bear/50 hover:text-bear">
              <RotateCcw className="size-3" /> Reset
            </button>
          )}
        </span>
      </div>
      {s.reset_at && <div className="mt-0.5 text-[10px] text-faint">since {new Date(s.reset_at).toLocaleString()}</div>}
      <div className="mt-1 line-clamp-2 text-xs text-faint">{s.title}</div>
      <div className={`num mt-3 text-2xl font-semibold ${pctClass(s.realized_usd || null)}`}>{money(s.realized_usd)}</div>
      <div className="num mt-1 text-xs text-muted">
        {s.closed} closed · win {s.win_rate == null ? "—" : `${(s.win_rate * 100).toFixed(0)}%`} · return {pct(s.return_pct)}
      </div>
      <div className="mt-2 flex h-1.5 overflow-hidden rounded-full bg-raised" title={`${s.wins} wins of ${s.closed}`}>
        <span className="h-full bg-mint transition-all duration-700" style={{ width: `${s.closed ? (s.wins / s.closed) * 100 : 0}%` }} />
        <span className="h-full bg-bear/70 transition-all duration-700" style={{ width: `${s.closed ? ((s.closed - s.wins) / s.closed) * 100 : 0}%` }} />
      </div>
      <div className="num mt-2 text-xs text-muted">
        {s.open} open · unrealized <span className={pctClass(s.unrealized_usd || null)}>{money(s.unrealized_usd)}</span>
      </div>
      <div className="num mt-2 text-[11px] text-faint">
        {s.mirror ? `Exits with the wallet · max ${hold(s.max_hold_minutes)}` : `TP +${s.tp_pct}% · SL ${s.sl_pct}% · max ${hold(s.max_hold_minutes)}`}
      </div>
    </div>
  );
}

/** One open paper trade: P&L, and for TP/SL strategies where it sits between the stop and the target. */
function OpenCard({ p, s, now }: { p: PaperPositionOut; s?: PaperStrategy; now: number }) {
  const up = (p.pnl_pct ?? 0) >= 0;
  const ranged = s && !s.mirror && s.tp_pct > 0 && s.sl_pct < 0;
  const at = ranged && p.pnl_pct != null
    ? Math.max(0, Math.min(100, ((p.pnl_pct - s.sl_pct) / (s.tp_pct - s.sl_pct)) * 100)) : null;
  const zero = ranged ? ((0 - s.sl_pct) / (s.tp_pct - s.sl_pct)) * 100 : null;
  return (
    <div className="card relative flex flex-col gap-3 overflow-hidden p-4 transition hover:border-line-strong">
      <span className={`absolute inset-y-0 start-0 w-[3px] ${p.pnl_pct == null ? "bg-faint" : up ? "bg-mint" : "bg-bear"}`} />
      <div className="flex items-start gap-3">
        <span className={`grid size-9 shrink-0 place-items-center rounded-full text-xs font-bold ${p.pnl_pct == null ? "bg-raised text-muted" : up ? "bg-mint-soft text-mint" : "bg-bear-soft text-bear"}`}>
          {(p.symbol || "?").slice(0, 2).toUpperCase()}
        </span>
        <div className="min-w-0 flex-1">
          <a href={`/token/${p.chain}/${p.token_address}`} className="flex items-center gap-1.5 hover:text-mint">
            <span className="truncate font-semibold">${p.symbol || p.token_address.slice(0, 6)}</span>
            <ChainPill chain={p.chain} />
          </a>
          <div className="truncate text-[11px] text-faint" title={p.trigger}>
            <span className="num text-muted">{p.strategy}</span> · {ago(p.entry_time, now)} · {p.trigger}
          </div>
        </div>
        <div className="text-right">
          <div className={`num text-base font-semibold ${pctClass(p.pnl_usd)}`}>{money(p.pnl_usd)}</div>
          <div className={`num text-[11px] ${pctClass(p.pnl_pct)}`}>{pct(p.pnl_pct)}</div>
        </div>
      </div>
      {at != null && s && (
        <div>
          <div className="relative h-1.5 rounded-full bg-gradient-to-r from-bear/40 via-raised to-mint/40">
            {zero != null && <span className="absolute inset-y-[-2px] w-px bg-line-strong" style={{ left: `${zero}%` }} />}
            <span className={`absolute top-1/2 size-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-carbon ${up ? "bg-mint" : "bg-bear"} transition-all duration-700`}
              style={{ left: `${at}%` }} />
          </div>
          <div className="num mt-1 flex justify-between text-[10px] text-faint">
            <span>SL {s.sl_pct}%</span><span>TP +{s.tp_pct}%</span>
          </div>
        </div>
      )}
      <div className="num flex flex-wrap items-center justify-between gap-x-3 text-[11px] text-muted">
        <span>entry {price(p.entry_price)}</span>
        <span>now {price(p.last_price)}</span>
        <span>peak <span className="text-bull">{pct(p.peak_pct)}</span></span>
        {p.remaining_pct < 100 && <span className="text-gold">{p.remaining_pct.toFixed(0)}% left</span>}
      </div>
    </div>
  );
}

type Result = "all" | "wins" | "losses";

function HistoryPanel({ rows, now }: { rows: PaperPositionOut[]; now: number }) {
  const [f, setF] = useState<Result>("all");
  const list = rows
    .filter((p) => f === "all" || (f === "wins" ? (p.pnl_usd ?? 0) > 0 : (p.pnl_usd ?? 0) <= 0))
    .sort((a, b) => (b.exit_time ?? "").localeCompare(a.exit_time ?? ""));
  const net = list.reduce((s, p) => s + (p.pnl_usd ?? 0), 0);
  const wins = list.filter((p) => (p.pnl_usd ?? 0) > 0).length;
  return (
    <Panel icon={<History />} title="History"
      meta={<>{list.length} trades · net <span className={pctClass(net)}>{money(net)}</span>{list.length ? ` · ${Math.round((wins / list.length) * 100)}% wins` : ""}</>}
      actions={<Segmented label="Result" value={f} onChange={setF}
        options={[{ value: "all", label: "All" }, { value: "wins", label: "Wins" }, { value: "losses", label: "Losses" }]} />}
      flush>
      {list.length === 0 ? <Empty>No closed paper trades in this window.</Empty> : (
        <div className="overflow-x-auto">
          <table className="data-table min-w-[900px]">
            <thead>
              <tr>
                <th className="text-left">Token</th>
                <th className="text-left">Strategy</th>
                <th className="text-left">Trigger</th>
                <th className="text-right">Closed</th>
                <th className="text-right">Entry</th>
                <th className="text-right">Exit</th>
                <th className="text-right">Peak</th>
                <th className="text-right">P&amp;L</th>
                <th className="text-left">Why</th>
              </tr>
            </thead>
            <tbody>
              {list.map((p) => (
                <tr key={p.id}>
                  <td>
                    <a href={`/token/${p.chain}/${p.token_address}`} className="flex items-center gap-1.5 hover:text-mint">
                      <span className="font-semibold">${p.symbol || p.token_address.slice(0, 6)}</span>
                      <ChainPill chain={p.chain} />
                    </a>
                  </td>
                  <td className="num text-xs text-muted">{p.strategy}</td>
                  <td className="max-w-[220px] truncate text-xs text-muted" title={p.trigger}>{p.trigger}</td>
                  <td className="num text-right text-muted">{ago(p.exit_time, now)}</td>
                  <td className="num text-right">{price(p.entry_price)}</td>
                  <td className="num text-right">{price(p.exit_price)}</td>
                  <td className="num text-right text-bull">{pct(p.peak_pct)}</td>
                  <td className={`num text-right ${pctClass(p.pnl_usd)}`}>
                    {money(p.pnl_usd)}
                    <span className="block text-[10px]">{pct(p.pnl_pct)}</span>
                  </td>
                  <td className="text-xs text-muted">{REASON[p.exit_reason ?? ""] ?? p.exit_reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
