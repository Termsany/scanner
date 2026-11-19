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

import { api, ApiError } from "@/lib/api";
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

function Stat({
  label,
  value,
  sub,
  cls,
}: {
  label: string;
  value: string;
  sub?: string;
  cls?: string;
}) {
  return (
    <div className="card px-4 py-3.5">
      <div className="text-[11px] font-medium tracking-[0.14em] text-muted uppercase">
        {label}
      </div>
      <div className={`num mt-2 text-2xl font-semibold ${cls ?? ""}`}>
        {value}
      </div>
      {sub && <div className="mt-1 text-xs text-faint">{sub}</div>}
    </div>
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
          className="rounded border border-bear/60 px-1.5 py-0.5 text-bear disabled:opacity-50"
        >
          Yes, sell
        </button>
        <button
          onClick={() => setConfirm(null)}
          className="rounded border border-line px-1.5 py-0.5 text-muted"
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
        className="rounded border border-line px-1.5 py-0.5 text-muted hover:text-ink"
      >
        Sell 50%
      </button>
      <button
        onClick={() => setConfirm(1)}
        className="rounded border border-bear/50 px-1.5 py-0.5 text-bear hover:bg-bear/10"
      >
        Sell 100%
      </button>
    </span>
  );
}

function PositionRows({
  rows,
  now,
  onSell,
}: {
  rows: LivePositionOut[];
  now: number;
  onSell: (p: LivePositionOut, share: number) => Promise<void>;
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
          <tr
            key={p.id}
            className="border-b border-line/60 align-top hover:bg-white/[0.025]"
          >
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
                  <span className="text-accent">Open</span>
                  <SellButtons p={p} onSell={onSell} />
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

  useEffect(() => {
    const saved = readSaved();
    if (saved) setPassword(saved);
  }, []);

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
      <main className="mx-auto flex min-h-[70vh] max-w-sm flex-col justify-center px-4">
        <form
          className="card flex flex-col gap-3 p-6"
          onSubmit={(e) => {
            e.preventDefault();
            if (input) setPassword(input);
          }}
        >
          <h1 className="flex items-center gap-2 text-base font-semibold">
            <Lock className="size-4 text-accent" /> Live trading
          </h1>
          <p className="text-xs text-muted">
            Private page. Enter the password the bot sent you on Telegram.
          </p>
          <input
            type="password"
            autoComplete="current-password"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Password"
            className="rounded-md border border-line bg-carbon px-3 py-2 text-sm outline-none focus:border-accent"
          />
          {error && <p className="text-xs text-bear">{error}</p>}
          <button
            type="submit"
            className="rounded-md bg-accent px-3 py-2 text-sm font-semibold text-carbon"
          >
            Unlock
          </button>
        </form>
      </main>
    );
  }

  const modeLabel =
    data?.mode === "live" ? "LIVE" : data?.mode === "dry" ? "DRY-RUN" : "OFF";

  return (
    <main className="mx-auto max-w-[1400px] px-4 pt-5 pb-12 sm:px-6">
      <header className="flex flex-wrap items-center gap-3">
        <a
          href="/"
          className="inline-flex items-center gap-1 rounded-md border border-line px-2 py-1 text-xs text-muted hover:bg-raised hover:text-ink"
        >
          <ArrowLeft className="size-3.5" /> Scanner
        </a>
        <Wallet className="size-5 text-accent" />
        <h1 className="text-lg font-semibold tracking-tight">
          Live copy trading
        </h1>
        {data && (
          <span
            className={`rounded-full border px-2.5 py-0.5 text-[11px] font-bold tracking-[0.12em] ${
              data.mode === "live"
                ? "border-bull/50 text-bull"
                : "border-line text-muted"
            }`}
          >
            {modeLabel}
            {data.paused ? " · PAUSED" : ""}
          </span>
        )}
        {data && (
          <span className="num inline-flex items-center gap-1.5 text-[11px] text-muted">
            <span
              className={`size-1.5 rounded-full ${tick - now < REFRESH_MS * 3 ? "animate-pulse bg-bull" : "bg-warn"}`}
            />
            <span
              className={`rounded border px-1.5 py-0.5 text-xs font-semibold ${tone(
                data.accounts.reduce(
                  (s, a) => s + a.realized_usd + (a.unrealized_usd ?? 0),
                  0,
                ),
              )}`}
            >
              Live P&amp;L{" "}
              {money(
                data.accounts.reduce(
                  (s, a) => s + a.realized_usd + (a.unrealized_usd ?? 0),
                  0,
                ),
                true,
              )}
            </span>
            updated {Math.max(0, Math.round((tick - now) / 1000))}s ago
            {data.sol_usd ? ` · SOL ${money(data.sol_usd)}` : ""}
          </span>
        )}
        <div className="ml-auto flex items-center gap-2">
          {data && data.mode !== "off" && (
            <button
              onClick={togglePause}
              disabled={busy}
              className={`inline-flex items-center gap-1 rounded-md border px-2.5 py-1 text-xs ${
                data.paused
                  ? "border-bull/50 text-bull"
                  : "border-bear/50 text-bear"
              } disabled:opacity-50`}
            >
              {data.paused ? (
                <Play className="size-3.5" />
              ) : (
                <Pause className="size-3.5" />
              )}
              {data.paused ? "Resume buys" : "Pause buys"}
            </button>
          )}
          <button
            onClick={() => {
              save(null);
              setPassword("");
              setInput("");
              setData(null);
            }}
            className="inline-flex items-center gap-1 rounded-md border border-line px-2 py-1 text-xs text-muted hover:text-ink"
          >
            <LogOut className="size-3.5" /> Lock
          </button>
        </div>
      </header>

      {error && <p className="card mt-4 p-4 text-sm text-bear">{error}</p>}

      {!data ? (
        <div className="mt-6 grid grid-cols-2 gap-3 lg:grid-cols-4">
          {[0, 1, 2, 3].map((i) => (
            <span key={i} className="skeleton h-24" />
          ))}
        </div>
      ) : (
        <>
          <p className="mt-3 text-xs text-faint">
            {data.mode === "dry"
              ? "Dry-run: real Jupiter quotes, no transactions are sent. "
              : data.mode === "live"
                ? "Real money. "
                : "Copy trading is off on the server. "}
            Pausing stops new buys on every account; open positions still sell
            with the wallet.
          </p>
          <LeaderFeed rows={data.leader_activity} now={tick} />
          {data.accounts.map((a) => (
            <AccountSection
              key={a.name}
              a={a}
              now={now}
              onSell={sellPosition}
            />
          ))}
        </>
      )}
    </main>
  );
}

function AccountSection({
  a: data,
  now,
  onSell,
}: {
  a: LiveAccount;
  now: number;
  onSell: (p: LivePositionOut, share: number) => Promise<void>;
}) {
  // failed buys spent nothing: they stay out of the table (count only, in the Budget card)
  const all = [...data.open, ...data.closed].sort((x, y) =>
    (y.opened_at ?? "").localeCompare(x.opened_at ?? ""),
  );
  const sellPosition = onSell;
  return (
    <section className="mt-6 border-t border-line pt-5">
      <h2 className="flex flex-wrap items-center gap-2 text-base font-semibold">
        Account{" "}
        <span className="num rounded border border-line px-1.5 text-sm">
          {data.name}
        </span>
        {!data.active && (
          <span className="text-xs font-normal text-warn">
            idle: no key on the server yet
          </span>
        )}
      </h2>
      <p className="mt-1 text-xs text-faint">
        Copying {data.wallets.map(short).join(", ") || "—"} ·{" "}
        {money(data.size_usd)} per buy · max {money(data.max_open_usd)} open ·
        buys pause after {money(data.max_daily_loss_usd)} loss in 24h.
        {data.pubkey && (
          <>
            {" "}
            Trading wallet{" "}
            <a
              href={`https://solscan.io/account/${data.pubkey}`}
              target="_blank"
              rel="noreferrer"
              className="num text-accent hover:underline"
            >
              {short(data.pubkey)}
            </a>
            .
          </>
        )}
      </p>

      <section className="mt-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat
          label="Wallet"
          value={
            data.sol_balance != null
              ? `${data.sol_balance.toFixed(3)} SOL`
              : "—"
          }
          sub={
            data.sol_balance_usd != null
              ? `≈ ${money(data.sol_balance_usd)} free to trade`
              : "balance unavailable"
          }
        />
        <Stat
          label="Live P&L now"
          value={money(data.realized_usd + (data.unrealized_usd ?? 0), true)}
          cls={tone(data.realized_usd + (data.unrealized_usd ?? 0))}
          sub={`realized ${money(data.realized_usd, true)} · open ${money(
            data.unrealized_usd ?? null,
            true,
          )}${
            data.open.some((p) => p.unrealized_usd == null)
              ? ` · ${data.open.filter((p) => p.unrealized_usd == null).length} without a price`
              : ""
          }`}
        />
        <Stat
          label="Realized P&L"
          value={money(data.realized_usd, true)}
          cls={tone(data.realized_usd)}
          sub={`${data.closed.length} closed · 24h ${money(data.realized_24h, true)}`}
        />
        <Stat
          label="Budget"
          value={`${money(data.open_cost_usd)} / ${money(data.max_open_usd)}`}
          sub={`${money(data.size_usd)} per buy${
            data.failed.length
              ? ` · ${data.failed.length} buys didn't go through (nothing spent)`
              : ""
          }`}
        />
      </section>

      <section className="card mt-4 overflow-x-auto">
        <h2 className="border-b border-line px-4 py-3 text-sm font-semibold">
          Trades
        </h2>
        {all.length === 0 ? (
          <p className="px-4 py-6 text-sm text-muted">
            No trades yet. The first one happens on the copied wallet&apos;s
            next buy.
          </p>
        ) : (
          <table className="w-full min-w-[860px] text-sm">
            <thead className="text-[10.5px] tracking-[0.12em] text-muted uppercase">
              <tr className="border-b border-line">
                <th className="px-4 py-2 text-left">Token</th>
                <th className="px-3 py-2 text-right">Opened</th>
                <th className="px-3 py-2 text-right">Cost</th>
                <th className="px-3 py-2 text-right">Sold for</th>
                <th className="px-3 py-2 text-right">Value now</th>
                <th className="px-3 py-2 text-right">P&amp;L</th>
                <th className="px-3 py-2 text-left">Status</th>
                <th className="px-3 py-2 text-left">Fills</th>
              </tr>
            </thead>
            <tbody>
              <PositionRows rows={all} now={now} onSell={sellPosition} />
            </tbody>
          </table>
        )}
      </section>
    </section>
  );
}

function LeaderFeed({ rows, now }: { rows: LeaderTrade[]; now: number }) {
  if (!rows.length) return null;
  return (
    <section className="card mt-4 overflow-x-auto">
      <h2 className="border-b border-line px-4 py-3 text-sm font-semibold">
        Copied wallet activity
      </h2>
      <ul className="divide-y divide-line/60 text-sm">
        {rows.map((t) => (
          <li
            key={t.id}
            className="flex flex-wrap items-center gap-x-3 gap-y-0.5 px-4 py-2"
          >
            <span
              className={`w-10 text-xs font-bold ${t.side === "buy" ? "text-bull" : "text-bear"}`}
            >
              {t.side === "buy" ? "BUY" : "SELL"}
            </span>
            <a
              href={`/token/solana/${t.token_address}`}
              className="font-semibold hover:underline"
            >
              ${t.symbol || short(t.token_address)}
            </a>
            <span className="num text-muted">
              {t.usd != null ? money(t.usd) : "—"}
              {t.side === "sell" && t.pct_sold != null
                ? ` · ${t.pct_sold.toFixed(0)}% of holding`
                : ""}
              {t.kind === "transfer" ? " · transfer out" : ""}
            </span>
            <span className="num text-xs text-faint">
              {t.label || short(t.wallet)}
            </span>
            <span className="num ml-auto text-xs text-muted">
              {ago(t.at, now)}
            </span>
            <TxLink sig={t.tx} />
          </li>
        ))}
      </ul>
    </section>
  );
}
