"use client";

import {
  ArrowLeft,
  ExternalLink,
  Lock,
  LogOut,
  Pause,
  Play,
  Wallet,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { AdminSignIn, adminSignOut, useAdminResume } from "@/components/AdminAuth";
import { AppNav } from "@/components/AppNav";
import { Badge, button, Empty, PageHeader, Panel, StatTile } from "@/components/ui";
import { api, ApiError, SESSION } from "@/lib/api";
import { ago, pct, price } from "@/lib/format";
import type {
  LeaderTrade,
  LiveAccount,
  LivePositionOut,
  LiveStatus,
} from "@/lib/types";

const REFRESH_MS = 3_000;

const KEY = "alpha-live-password";

function readSaved(): string {
  try {
    return localStorage.getItem(KEY) ?? "";
  } catch {
    return "";
  }
}

function save(v: string | null) {
  try {
    if (v === SESSION) return;
    if (v) localStorage.setItem(KEY, v);
    else localStorage.removeItem(KEY);
  } catch {
    /* storage unavailable */
  }
}

function money(v: number | null | undefined, signed = false) {
  if (v == null) return "—";
  const s = Math.abs(v).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  if (!signed) return `$${s}`;
  return `${v >= 0 ? "+" : "−"}$${s}`;
}

function tone(v: number | null | undefined) {
  if (v == null || v === 0) return "";
  return v > 0 ? "text-bull" : "text-bear";
}

function short(a: string) {
  return `${a.slice(0, 4)}…${a.slice(-4)}`;
}

function TxLink({ sig }: { sig?: string | null }) {
  if (!sig) return null;
  if (sig.startsWith("dry-"))
    return <span className="text-faint">simulated</span>;
  return (
    <a
      href={`https://solscan.io/tx/${sig}`}
      target="_blank"
      rel="noreferrer"
      className="inline-flex items-center gap-0.5 text-accent hover:underline"
    >
      tx <ExternalLink className="size-3" />
    </a>
  );
}

function SellButtons({
  p,
  onSell,
}: {
  p: LivePositionOut;
  onSell: (p: LivePositionOut, share: number) => Promise<void>;
}) {
  const [confirm, setConfirm] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  if (p.manual_sell_frac)
    return (
      <span className="block text-warn">
        Selling {(p.manual_sell_frac * 100).toFixed(0)}%…
      </span>
    );
  if (confirm != null) {
    return (
      <span className="mt-1 flex flex-wrap items-center gap-1">
        <span className="text-muted">Sell {confirm * 100}%?</span>
        <button
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            try {
              await onSell(p, confirm);
            } finally {
              setBusy(false);
              setConfirm(null);
            }
          }}
          className="rounded-md border border-bear/60 px-2 py-0.5 text-bear disabled:opacity-50"
        >
          Yes, sell
        </button>
        <button
          onClick={() => setConfirm(null)}
          className="rounded-md border border-line px-2 py-0.5 text-muted"
        >
          Cancel
        </button>
      </span>
    );
  }
  return (
    <span className="mt-1 flex gap-1">
      <button
        onClick={() => setConfirm(0.5)}
        className="rounded-md border border-line bg-raised px-2 py-0.5 text-muted hover:text-ink"
      >
        Sell 50%
      </button>
      <button
        onClick={() => setConfirm(1)}
        className="rounded-md border border-bear/50 px-2 py-0.5 text-bear hover:bg-bear-soft"
      >
        Sell 100%
      </button>
    </span>
  );
}

/** The owner's own sell target / stop for one open position. */
function TargetEditor({
  p,
  onTarget,
}: {
  p: LivePositionOut;
  onTarget: (p: LivePositionOut, body: { target_pct: number | null; share: number; stop_pct: number | null }) => Promise<string>;
}) {
  const [open, setOpen] = useState(false);
  const [tp, setTp] = useState(p.target_pct != null ? String(p.target_pct) : "");
  const [share, setShare] = useState(p.target_share != null ? String(Math.round(p.target_share * 100)) : "100");
  const [stop, setStop] = useState(p.stop_pct != null ? String(Math.abs(p.stop_pct)) : "");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const box = "num w-14 rounded border border-line bg-carbon px-1 py-0.5 text-center text-[11px] text-ink outline-none focus:border-accent";
  const has = p.target_pct != null || p.stop_pct != null;

  async function save(clear = false) {
    setBusy(true);
    setMsg("");
    try {
      const t = clear || tp.trim() === "" ? null : Number(tp);
      const st = clear || stop.trim() === "" ? null : -Math.abs(Number(stop.replace("-", "")));
      const sh = Math.min(Math.max(Number(share) || 100, 5), 100) / 100;
      if ((t != null && !Number.isFinite(t)) || (st != null && !Number.isFinite(st))) {
        setMsg("Numbers only");
        return;
      }
      setMsg(await onTarget(p, { target_pct: t, share: sh, stop_pct: st }));
      if (clear) {
        setTp("");
        setStop("");
      }
      setOpen(false);
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Could not save");
    } finally {
      setBusy(false);
    }
  }

  return (
    <span className="mt-1 block text-[11px]">
      {has && !open && (
        <span className="block text-gold">
          🎯 {p.target_pct != null && <>sell {Math.round((p.target_share ?? 1) * 100)}% at +{p.target_pct}%</>}
          {p.target_pct != null && p.stop_pct != null && " · "}
          {p.stop_pct != null && <>stop {p.stop_pct}%</>}
        </span>
      )}
      {!open ? (
        <button onClick={() => setOpen(true)} className="mt-0.5 rounded-md border border-line px-2 py-0.5 text-muted hover:text-ink">
          {has ? "Edit target" : "Target"}
        </button>
      ) : (
        <span className="mt-1 flex flex-wrap items-center gap-1 text-muted">
          <span>Sell</span>
          <input className={box} inputMode="decimal" value={share} onChange={(e) => setShare(e.target.value)} />
          <span>% at +</span>
          <input className={box} inputMode="decimal" placeholder="100" value={tp} onChange={(e) => setTp(e.target.value)} />
          <span>% · stop −</span>
          <input className={box} inputMode="decimal" placeholder="—" value={stop} onChange={(e) => setStop(e.target.value)} />
          <span>%</span>
          <button disabled={busy} onClick={() => save()} className="rounded-md border border-mint/60 px-2 py-0.5 text-mint disabled:opacity-50">
            Save
          </button>
          {has && (
            <button disabled={busy} onClick={() => save(true)} className="rounded-md border border-line px-2 py-0.5 text-muted disabled:opacity-50">
              Clear
            </button>
          )}
          <button onClick={() => setOpen(false)} className="rounded-md border border-line px-2 py-0.5 text-muted">Cancel</button>
        </span>
      )}
      {msg && <span className="block text-faint">{msg}</span>}
    </span>
  );
}

function PositionRows({
  rows,
  now,
  onSell,
  onTarget,
}: {
  rows: LivePositionOut[];
  now: number;
  onSell: (p: LivePositionOut, share: number) => Promise<void>;
  onTarget?: (p: LivePositionOut, body: { target_pct: number | null; share: number; stop_pct: number | null }) => Promise<string>;
}) {
  return (
    <>
      {rows.map((p) => {
        const live = p.status === "open";
        const pnl =
          p.status === "closed"
            ? p.pnl_usd
            : live
              ? (p.unrealized_usd ?? null)
              : null;
        const pnlPct =
          p.status === "closed"
            ? p.cost_usd
              ? ((p.pnl_usd ?? 0) / p.cost_usd) * 100
              : null
            : (p.unrealized_pct ?? null);
        return (
          <tr key={p.id} className="align-top">
            <td className="px-4 py-2">
              <a
                href={`/token/solana/${p.token_address}`}
                className="font-semibold hover:underline"
              >
                ${p.symbol || short(p.token_address)}
              </a>
              {p.mode === "dry" && (
                <span className="ml-1.5 rounded border border-line px-1 text-[10px] text-faint">
                  DRY
                </span>
              )}
            </td>
            <td className="num px-3 py-2 text-right text-muted">
              {ago(p.opened_at, now)}
            </td>
            <td className="num px-3 py-2 text-right">
              {money(p.cost_usd)}
              <span className="block text-[10px] text-faint">
                {p.sol_spent.toFixed(4)} SOL
              </span>
            </td>
            <td className="num px-3 py-2 text-right">
              {p.proceeds_usd ? money(p.proceeds_usd) : "—"}
            </td>
            <td className="num px-3 py-2 text-right">
              {live ? (
                <>
                  {money(p.value_usd)}
                  {p.price_usd != null && (
                    <span className="block text-[10px] text-faint">
                      @ {price(p.price_usd)}
                    </span>
                  )}
                </>
              ) : (
                "—"
              )}
            </td>
            <td className={`num px-3 py-2 text-right ${tone(pnl)}`}>
              {money(pnl, true)}
              {pnlPct != null && (
                <span className="block text-[10px]">{pct(pnlPct)}</span>
              )}
              {live && pnl != null && (
                <span className="block text-[10px] text-faint">unrealized</span>
              )}
            </td>
            <td className="px-3 py-2 text-xs">
              {p.status === "open" ? (
                <>
                  <Badge tone="mint" dot>Open</Badge>
                  <SellButtons p={p} onSell={onSell} />
                  {onTarget && <TargetEditor p={p} onTarget={onTarget} />}
                </>
              ) : p.status === "failed" ? (
                <span className="text-bear" title={p.error ?? ""}>
                  Failed: {(p.error ?? "").slice(0, 60)}
                </span>
              ) : (
                <span className="text-muted">
                  {(p.exit_reason ?? "").replace(/_/g, " ")}
                </span>
              )}
              {p.status === "open" && p.error && (
                <span className="block text-bear">
                  sell retrying: {p.error.slice(0, 50)}
                </span>
              )}
            </td>
            <td className="px-3 py-2 text-xs">
              <div className="flex flex-col gap-0.5">
                {p.fills
                  .filter((f) => f.side !== "sell_failed")
                  .map((f, i) => (
                    <span key={i} className="num text-muted">
                      {f.side === "buy" ? "Buy" : "Sell"} {money(f.usd)}{" "}
                      <TxLink sig={f.sig} />
                    </span>
                  ))}
              </div>
            </td>
          </tr>
        );
      })}
    </>
  );
}

export function LiveView() {
  const [password, setPassword] = useState("");
  const [input, setInput] = useState("");
  const [data, setData] = useState<LiveStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(() => Date.now());

  useAdminResume(setPassword);

  const load = useCallback(async (pw: string, signal?: AbortSignal) => {
    try {
      const d = await api.live(pw, signal);
      setData(d);
      setError(null);
      setNow(Date.now());
      save(pw);
    } catch (e) {
      if ((e as Error).name === "AbortError") return;
      if (
        e instanceof ApiError &&
        (e.status === 401 || e.status === 429 || e.status === 404)
      ) {
        setPassword("");
        setData(null);
        save(null);
        setError(e.status === 401 ? "Wrong password." : e.message);
      } else {
        setError("Could not load live data.");
      }
    }
  }, []);

  useEffect(() => {
    if (!password) return;
    const ctl = new AbortController();
    load(password, ctl.signal);
    let inFlight = false;
    const id = setInterval(async () => {
      if (inFlight || document.hidden) return; // no polling in a background tab
      inFlight = true;
      try {
        await load(password, ctl.signal);
      } finally {
        inFlight = false;
      }
    }, REFRESH_MS);
    const onVisible = () => !document.hidden && load(password, ctl.signal);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
      ctl.abort();
    };
  }, [password, load]);

  const [tick, setTick] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setTick(Date.now()), 1_000);
    return () => clearInterval(id);
  }, []);

  async function setTarget(p: LivePositionOut, body: { target_pct: number | null; share: number; stop_pct: number | null }) {
    try {
      const r = await api.liveTarget(password, p.id, body);
      await load(password);
      return r.message;
    } catch (e) {
      throw new Error(e instanceof ApiError ? e.message : "Could not save the target.");
    }
  }

  async function sellPosition(p: LivePositionOut, share: number) {
    try {
      await api.liveSell(password, p.id, share);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Sell request failed.");
    }
    await load(password);
  }

  async function togglePause() {
    if (!data) return;
    setBusy(true);
    try {
      await api.livePause(password, !data.paused);
      await load(password);
    } finally {
      setBusy(false);
    }
  }

  if (!password) {
    return (
      <>
        <AppNav />
        <AdminSignIn onDone={() => setPassword(SESSION)} />
      </>
    );
  }

  const totalLive = data ? data.accounts.reduce((s, a) => s + a.realized_usd + (a.unrealized_usd ?? 0), 0) : null;
  const totalReal = data ? data.accounts.reduce((s, a) => s + a.realized_usd, 0) : null;
  const total24 = data ? data.accounts.reduce((s, a) => s + a.realized_24h, 0) : null;
  const totalOpen = data ? data.accounts.reduce((s, a) => s + a.open_cost_usd, 0) : null;
  const totalSol = data ? data.accounts.reduce((s, a) => s + (a.sol_balance ?? 0), 0) : null;
  const totalSolUsd = data ? data.accounts.reduce((s, a) => s + (a.sol_balance_usd ?? 0), 0) : null;
  const fresh = tick - now < REFRESH_MS * 3;

  return (
    <>
    <AppNav />
    <main className="mx-auto max-w-[1400px] px-4 pt-6 pb-14 sm:px-6">
      <PageHeader icon={<Wallet className="size-5" />} title="Live copy trading"
        badges={data && (
          <>
            <Badge tone={data.mode === "live" ? "mint" : data.mode === "dry" ? "gold" : "muted"} dot>
              {data.mode === "live" ? "LIVE" : data.mode === "dry" ? "DRY-RUN" : "OFF"}
            </Badge>
            {data.paused && <Badge tone="gold">ALL BUYS PAUSED</Badge>}
          </>
        )}
        subtitle={data && (
          <span className="inline-flex flex-wrap items-center gap-x-2">
            <span className={`size-1.5 rounded-full ${fresh ? "animate-pulse bg-mint" : "bg-warn"}`} />
            updated {Math.max(0, Math.round((tick - now) / 1000))}s ago
            {data.sol_usd ? <span>· SOL <span className="num">{money(data.sol_usd)}</span></span> : null}
            <span className="text-faint">
              · {data.mode === "dry" ? "Dry-run: real quotes, nothing is sent." : data.mode === "live" ? "Real money." : "Copy trading is off on the server."}
              {" "}Pausing stops new buys on every account; open positions still sell.
            </span>
          </span>
        )}
        actions={
          <>
            {data && data.mode !== "off" && (
              <button onClick={togglePause} disabled={busy} className={button(data.paused ? "good" : "danger")}>
                {data.paused ? <Play className="size-3.5" /> : <Pause className="size-3.5" />}
                {data.paused ? "Resume buys" : "Pause buys"}
              </button>
            )}
            <button onClick={async () => { await adminSignOut(); setPassword(""); setInput(""); setData(null); }} className={button("ghost")}>
              <LogOut className="size-3.5" /> Sign out
            </button>
          </>
        } />

      {error && <p className="card mb-4 border-bear/40 p-4 text-sm text-bear">{error}</p>}

      {!data ? (
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          {[0, 1, 2, 3].map((i) => <span key={i} className="skeleton h-28 rounded-2xl" />)}
        </div>
      ) : (
        <div className="flex flex-col gap-6">
          <section className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <StatTile label="Live P&L · all accounts" value={money(totalLive, true)} tone={tone(totalLive) || "text-ink"}
              sub={`realized ${money(totalReal, true)} · open ${money((totalLive ?? 0) - (totalReal ?? 0), true)}`} />
            <StatTile label="Realized · 24h" value={money(total24, true)} tone={tone(total24) || "text-ink"}
              sub={`${data.accounts.reduce((s, a) => s + a.closed.length, 0)} closed trades shown`} />
            <StatTile label="In open trades" value={money(totalOpen)}
              sub={`${data.accounts.reduce((s, a) => s + a.open.length, 0)} open across ${data.accounts.length} accounts`} />
            <StatTile label="Wallets" value={`${(totalSol ?? 0).toFixed(3)} SOL`} sub={`≈ ${money(totalSolUsd)} free to trade`} />
          </section>
          <LeaderFeed rows={data.leader_activity} now={tick} />
          {data.accounts.map((a) => (
            <AccountSection key={a.name} a={a} now={now} onSell={sellPosition} onTarget={setTarget} />
          ))}
        </div>
      )}
    </main>
    </>
  );
}

function AccountSection({
  a: data,
  now,
  onSell,
  onTarget,
}: {
  a: LiveAccount;
  now: number;
  onSell: (p: LivePositionOut, share: number) => Promise<void>;
  onTarget?: (p: LivePositionOut, body: { target_pct: number | null; share: number; stop_pct: number | null }) => Promise<string>;
}) {
  // failed buys spent nothing: they stay out of the table (count only, in the Budget tile)
  const all = [...data.open, ...data.closed].sort((x, y) =>
    (y.opened_at ?? "").localeCompare(x.opened_at ?? ""),
  );
  const livePnl = data.realized_usd + (data.unrealized_usd ?? 0);
  const unpriced = data.open.filter((p) => p.unrealized_usd == null).length;
  return (
    <section className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 pt-2">
        <h2 className="text-lg font-semibold tracking-tight">Account</h2>
        <Badge tone="muted" className="num text-[12px] text-ink">{data.name}</Badge>
        {!data.active && <Badge tone="gold">idle: no key on the server yet</Badge>}
        {data.paused && <Badge tone="gold">paused</Badge>}
        <span className="text-xs text-muted">
          {money(data.size_usd)} per buy · max {money(data.max_open_usd)} open · buys pause after {money(data.max_daily_loss_usd)} loss in 24h
          {data.pubkey && (
            <> · wallet{" "}
              <a href={`https://solscan.io/account/${data.pubkey}`} target="_blank" rel="noreferrer" className="num text-mint hover:underline">
                {short(data.pubkey)}
              </a>
            </>
          )}
          {data.wallets.length > 0 && <> · copying {data.wallets.map(short).join(", ")}</>}
        </span>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatTile label="Wallet" value={data.sol_balance != null ? `${data.sol_balance.toFixed(3)} SOL` : "—"}
          sub={data.sol_balance_usd != null ? `≈ ${money(data.sol_balance_usd)} free to trade` : "balance unavailable"} />
        <StatTile label="Live P&L now" value={money(livePnl, true)} tone={tone(livePnl) || "text-ink"}
          sub={`realized ${money(data.realized_usd, true)} · open ${money(data.unrealized_usd ?? null, true)}${unpriced ? ` · ${unpriced} without a price` : ""}`} />
        <StatTile label="Realized P&L" value={money(data.realized_usd, true)} tone={tone(data.realized_usd) || "text-ink"}
          sub={`${data.closed.length} closed · 24h ${money(data.realized_24h, true)}`} />
        <StatTile label="Budget" value={<>{money(data.open_cost_usd)} <span className="text-base text-muted">/ {money(data.max_open_usd)}</span></>}
          sub={<>
            <span className="mt-1 block h-1.5 overflow-hidden rounded-full bg-raised">
              <span className="block h-full rounded-full bg-mint" style={{ width: `${Math.min(100, (data.open_cost_usd / Math.max(data.max_open_usd, 1)) * 100)}%` }} />
            </span>
            <span className="mt-1.5 block">{money(data.size_usd)} per buy{data.failed.length ? ` · ${data.failed.length} buys didn't go through (nothing spent)` : ""}</span>
          </>} />
      </div>

      <Panel title="Trades" meta={`${data.open.length} open · ${data.closed.length} closed`} flush>
        {all.length === 0 ? (
          <Empty>No trades yet. The first one happens on the copied wallet&apos;s next buy.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="data-table min-w-[900px]">
              <thead>
                <tr>
                  <th className="text-left">Token</th>
                  <th className="text-right">Opened</th>
                  <th className="text-right">Cost</th>
                  <th className="text-right">Sold for</th>
                  <th className="text-right">Value now</th>
                  <th className="text-right">P&amp;L</th>
                  <th className="text-left">Status</th>
                  <th className="text-left">Fills</th>
                </tr>
              </thead>
              <tbody>
                <PositionRows rows={all} now={now} onSell={onSell} onTarget={onTarget} />
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </section>
  );
}

function LeaderFeed({ rows, now }: { rows: LeaderTrade[]; now: number }) {
  if (!rows.length) return null;
  return (
    <Panel title="Copied wallet activity" meta="latest buys and sells of the wallets being copied" flush>
      <div className="overflow-x-auto">
        <table className="data-table compact min-w-[640px]">
          <tbody>
            {rows.map((t) => (
              <tr key={t.id}>
                <td className="w-16"><Badge tone={t.side === "buy" ? "mint" : "bear"}>{t.side === "buy" ? "BUY" : "SELL"}</Badge></td>
                <td>
                  <a href={`/token/solana/${t.token_address}`} className="font-semibold hover:text-mint">
                    ${t.symbol || short(t.token_address)}
                  </a>
                </td>
                <td className="num text-muted">
                  {t.usd != null ? money(t.usd) : "—"}
                  {t.side === "sell" && t.pct_sold != null ? ` · ${t.pct_sold.toFixed(0)}% of holding` : ""}
                  {t.kind === "transfer" ? " · transfer out" : ""}
                </td>
                <td className="num text-xs text-faint">{t.label || short(t.wallet)}</td>
                <td className="num text-right text-xs text-muted">{ago(t.at, now)}</td>
                <td className="w-12 text-right"><TxLink sig={t.tx} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}
