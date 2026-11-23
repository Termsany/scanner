"use client";

import {
  Activity,
  Bot,
  Coins,
  TrendingUp,
  Crosshair,
  ExternalLink,
  History,
  ListChecks,
  Radio,
  Wallet,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { AdminSignIn, useAdminResume } from "@/components/AdminAuth";
import { AppNav } from "@/components/AppNav";
import { Badge, Empty, PageHeader, pageMain, Panel, RangeBar, SectionTabs, Segmented, StatTile, Toggle } from "@/components/ui";
import { api, ApiError, SESSION } from "@/lib/api";
import { ago, pct, price } from "@/lib/format";
import type {
  HoldingCoin,
  Holdings,
  LeaderTrade,
  LiveAccount,
  LivePositionOut,
  TargetBody,
  LiveStatus,
  PriceOrderIn,
  PriceOrderOut,
  CurveBots,
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
    <span className="flex gap-1.5">
      <button
        onClick={() => setConfirm(0.5)}
        className="rounded-md border border-line bg-raised px-2.5 py-1 text-muted hover:text-ink"
      >
        Sell 50%
      </button>
      <button
        onClick={() => setConfirm(1)}
        className="rounded-md border border-bear/50 px-2.5 py-1 text-bear hover:bg-bear-soft"
      >
        Sell 100%
      </button>
    </span>
  );
}

/** Our average entry price per token: live price x cost / value x (left / bought). */
function entryPrice(p: LivePositionOut): number | null {
  const left = Number(p.tokens_left), bought = Number(p.tokens_bought) || left;
  return p.price_usd && p.value_usd && left > 0 ? (p.price_usd * p.cost_usd / p.value_usd) * (left / bought) : null;
}

/** The stop-to-target bar of a live trade: the owner's target / stop when set, else the bot's rule. */
function TradeBar({ p, peak }: { p: LivePositionOut; peak?: Peak }) {
  const entry = entryPrice(p);
  const fromEntry = (price?: number | null) => (price && entry ? (price / entry - 1) * 100 : null);
  const now = fromEntry(p.price_usd) ?? p.unrealized_pct ?? null;
  const target = p.target_pct ?? fromEntry(p.target_price);
  const stop = p.stop_pct ?? fromEntry(p.stop_price);
  const hi = target != null && target > 0 ? target : 100;
  const lo = stop != null && stop < 0 ? stop : -50;
  const share = Math.round((p.target_share ?? 1) * 100);
  const hiLabel = target != null && target > 0
    ? `🎯 ${share < 100 ? `${share}% at ` : ""}+${target.toFixed(0)}%`
    : p.hands_off ? "no target" : "2x · bot takes the capital";
  const loLabel = stop != null && stop < 0 ? `stop ${stop.toFixed(0)}%` : "no stop";
  return <RangeBar pct={now} lo={lo} hi={hi} loLabel={loLabel} hiLabel={hiLabel} peak={fromEntry(peak?.price)} />;
}

/** A coin price, USD per token, without exponents: 0.0000042, 0.01234, 1.234. */
function usdPrice(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return `$${v.toLocaleString("en-US", { maximumSignificantDigits: 4, maximumFractionDigits: 12 })}`;
}

/** The owner's own sell target / stop for one open position: by the coin's price or in % over the entry. */
function TargetEditor({
  p,
  onTarget,
  peak,
}: {
  p: LivePositionOut;
  onTarget: (p: LivePositionOut, body: TargetBody) => Promise<string>;
  peak?: Peak;
}) {
  const byPrice = p.target_price != null || p.stop_price != null;
  const byPct = p.target_pct != null || p.stop_pct != null;
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<"price" | "pct">(byPct ? "pct" : "price");
  const [tp, setTp] = useState(p.target_pct != null ? String(p.target_pct) : "");
  const [stop, setStop] = useState(p.stop_pct != null ? String(Math.abs(p.stop_pct)) : "");
  const [tpx, setTpx] = useState(p.target_price != null ? String(p.target_price) : "");
  const [spx, setSpx] = useState(p.stop_price != null ? String(p.stop_price) : "");
  const [share, setShare] = useState(p.target_share != null ? String(Math.round(p.target_share * 100)) : "100");
  // a new target starts as "I manage it"; an existing one shows what was saved
  const [mine, setMine] = useState(byPrice || byPct || p.hands_off ? !!p.hands_off : true);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const box = "num w-16 rounded-md border border-line bg-carbon px-1.5 py-1 text-center text-xs text-ink outline-none focus:border-gold/70";
  const wide = "num w-32 rounded-md border border-line bg-carbon px-2 py-1 text-xs text-ink outline-none focus:border-gold/70";
  const has = byPrice || byPct;
  // our average entry price, from the live price and value: price × cost / value × (left / bought)
  const left = Number(p.tokens_left), bought = Number(p.tokens_bought) || left;
  const entry = p.price_usd && p.value_usd && left > 0 ? (p.price_usd * p.cost_usd / p.value_usd) * (left / bought) : null;

  async function save(clear = false) {
    setBusy(true);
    setMsg("");
    try {
      const num = (v: string) => (clear || v.trim() === "" ? null : Number(v.replace(/[$,\s]/g, "")));
      const sh = Math.min(Math.max(Number(share) || 100, 5), 100) / 100;
      let body: TargetBody;
      if (mode === "price") {
        const t = num(tpx), st = num(spx);
        if ((t != null && !(t > 0)) || (st != null && !(st > 0))) {
          setMsg("Prices must be numbers above 0");
          return;
        }
        if (t != null && st != null && st >= t) {
          setMsg("The stop price must be below the target price");
          return;
        }
        body = { target_pct: null, stop_pct: null, share: sh, target_price: t, stop_price: st, hands_off: !clear && mine };
      } else {
        const t = num(tp);
        const s0 = num(stop.replace("-", ""));
        const st = s0 == null ? null : -Math.abs(s0);
        if ((t != null && !Number.isFinite(t)) || (st != null && !Number.isFinite(st))) {
          setMsg("Numbers only");
          return;
        }
        body = { target_pct: t, share: sh, stop_pct: st, hands_off: !clear && mine };
      }
      setMsg(await onTarget(p, body));
      if (clear) {
        setTp("");
        setStop("");
        setTpx("");
        setSpx("");
      }
      setOpen(false);
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Could not save");
    } finally {
      setBusy(false);
    }
  }

  const tab = (m: "price" | "pct", label: string) => (
    <button type="button" onClick={() => setMode(m)}
      className={`rounded px-1.5 py-0.5 ${mode === m ? "bg-raised text-ink" : "text-muted hover:text-ink"}`}>{label}</button>
  );

  return (
    <>
      {has && !open && (
        <span className="order-first flex basis-full items-center gap-1.5 rounded-md border border-gold/30 bg-gold-soft px-2 py-1 text-[11px] text-gold">
          <Crosshair className="size-3.5 shrink-0" />
          <span className="min-w-0 flex-1">
            {p.target_pct != null && <>sell {Math.round((p.target_share ?? 1) * 100)}% at +{p.target_pct}%</>}
            {p.target_price != null && <>sell {Math.round((p.target_share ?? 1) * 100)}% at {usdPrice(p.target_price)}</>}
            {(p.target_pct != null || p.target_price != null) && (p.stop_pct != null || p.stop_price != null) && " · "}
            {p.stop_pct != null && <>stop {p.stop_pct}%</>}
            {p.stop_price != null && <>stop {usdPrice(p.stop_price)}</>}
          </span>
        </span>
      )}
      {p.hands_off && !open && (p.leader_wallet?.startsWith("curve:") ? (
        <span className="order-first basis-full text-[11px] text-gold">🤖 curve bot {p.leader_wallet.slice(6)}: sells at the target, the stop or its time limit</span>
      ) : (
        <span className="order-first basis-full text-[11px] text-sky">✋ you manage it: the bot won&apos;t sell it</span>
      ))}
      {!open && (
        <button onClick={() => setOpen(true)}
          className="inline-flex items-center gap-1 rounded-md border border-gold/50 bg-gold-soft px-2.5 py-1 font-medium text-gold hover:border-gold">
          <Crosshair className="size-3.5" /> {has ? "Edit target" : "Sell target"}
        </button>
      )}
      {open && (
        <span className="order-last flex basis-full flex-col gap-2 rounded-lg border border-gold/30 bg-carbon/60 p-3 text-xs text-muted">
          <span className="flex flex-wrap items-center gap-2">
            <span className="flex rounded-md border border-line p-0.5">{tab("price", "Coin price $")}{tab("pct", "% from entry")}</span>
            <span className="text-faint">now <b className="num font-medium text-ink">{usdPrice(p.price_usd)}</b> · entry <b className="num font-medium text-ink">{usdPrice(entry)}</b></span>
          </span>
          {peak && (
            <span className="flex flex-wrap items-center gap-2 rounded-md bg-mint-soft/50 px-2 py-1 text-mint">
              <TrendingUp className="size-3.5" />
              high since you bought <b className="num font-medium">{usdPrice(peak.price)}</b>
              {entry ? <span className="num text-mint/80">({pct((peak.price / entry - 1) * 100)})</span> : null}
              <button type="button" className="ms-auto rounded border border-mint/50 px-1.5 py-0.5 hover:bg-mint-soft"
                onClick={() => { setMode("price"); setTpx(String(Number(peak.price.toPrecision(4)))); }}>
                Use it
              </button>
            </span>
          )}
          {mode === "price" ? (
            <span className="grid gap-2 sm:grid-cols-[auto_1fr]">
              <span className="self-center">Sell</span>
              <span className="flex flex-wrap items-center gap-1.5">
                <input className={box} inputMode="decimal" value={share} onChange={(e) => setShare(e.target.value)} />
                <span>% when the price reaches $</span>
                <input className={wide} inputMode="decimal" placeholder={entry ? String(Number((entry * 2).toPrecision(3))) : "target"}
                  value={tpx} onChange={(e) => setTpx(e.target.value)} />
              </span>
              <span className="self-center">Stop</span>
              <span className="flex flex-wrap items-center gap-1.5">
                <span>sell everything if it drops to $</span>
                <input className={wide} inputMode="decimal" placeholder="optional" value={spx} onChange={(e) => setSpx(e.target.value)} />
              </span>
            </span>
          ) : (
            <span className="grid gap-2 sm:grid-cols-[auto_1fr]">
              <span className="self-center">Sell</span>
              <span className="flex flex-wrap items-center gap-1.5">
                <input className={box} inputMode="decimal" value={share} onChange={(e) => setShare(e.target.value)} />
                <span>% when it is up +</span>
                <input className={box} inputMode="decimal" placeholder="100" value={tp} onChange={(e) => setTp(e.target.value)} />
                <span>%</span>
              </span>
              <span className="self-center">Stop</span>
              <span className="flex flex-wrap items-center gap-1.5">
                <span>sell everything at −</span>
                <input className={box} inputMode="decimal" placeholder="optional" value={stop} onChange={(e) => setStop(e.target.value)} />
                <span>%</span>
              </span>
            </span>
          )}
          <label className="flex items-start gap-2">
            <Toggle on={mine} label="I manage it" onClick={() => setMine(!mine)} />
            <span>I manage it: the bot never sells it (no leader exit, 2x, quick exit, max hold), only my target / stop</span>
          </label>
          <span className="flex flex-wrap gap-1.5">
            <button disabled={busy} onClick={() => save()}
              className="rounded-md bg-gold px-3 py-1 font-semibold text-carbon hover:brightness-110 disabled:opacity-50">
              Save target
            </button>
            {(has || p.hands_off) && (
              <button disabled={busy} onClick={() => save(true)} className="rounded-md border border-line px-2.5 py-1 text-muted disabled:opacity-50">
                Clear (back to the bot)
              </button>
            )}
            <button onClick={() => setOpen(false)} className="rounded-md border border-line px-2.5 py-1 text-muted">Cancel</button>
          </span>
        </span>
      )}
      {msg && <span className="order-last basis-full text-[11px] text-faint">{msg}</span>}
    </>
  );
}

function PositionRows({
  rows,
  now,
  onSell,
  onTarget,
  accountOf,
  showAccount,
  closedOnly,
}: {
  rows: LivePositionOut[];
  now: number;
  onSell: (p: LivePositionOut, share: number) => Promise<void>;
  onTarget?: (p: LivePositionOut, body: TargetBody) => Promise<string>;
  accountOf?: (p: LivePositionOut) => string;
  showAccount?: boolean;
  closedOnly?: boolean;      // history: no "value now" column
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
              {showAccount && accountOf && (
                <span className="ml-1.5"><Badge tone="muted" className="num">{accountOf(p)}</Badge></span>
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
            {!closedOnly && <td className="num px-3 py-2 text-right">
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
            </td>}
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
                  <span className="mt-1 flex flex-wrap items-center gap-1.5">
                    {onTarget && <TargetEditor p={p} onTarget={onTarget} />}
                    <SellButtons p={p} onSell={onSell} />
                  </span>
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
                      {f.side === "buy" || f.side === "buy_hand" ? "Buy" : f.side === "held" ? "Held" : "Sell"} {money(f.usd)}{" "}
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

type LiveTab = "open" | "bots" | "coins" | "orders" | "history" | "wallets";

type Peak = { price: number; at: string };

/** Each coin's highest price since the given time (GeckoTerminal candles), refreshed every minute. */
function usePeaks(password: string, items: { mint: string; since: number }[]): Record<string, Peak> {
  const [peaks, setPeaks] = useState<Record<string, Peak>>({});
  const key = items.map((i) => `${i.mint}:${i.since}`).sort().join(",");
  useEffect(() => {
    if (!password || !key) return;
    const list = key.split(",").map((k) => {
      const [mint, since] = k.split(":");
      return { mint, since: Number(since) };
    });
    const ctl = new AbortController();
    const run = () => api.peaks(password, list, ctl.signal).then((r) => setPeaks((p) => ({ ...p, ...r.peaks }))).catch(() => undefined);
    run();
    const id = setInterval(() => !document.hidden && run(), 60_000);
    return () => {
      clearInterval(id);
      ctl.abort();
    };
  }, [password, key]);
  return peaks;
}

const sinceOf = (iso: string | null) => Math.floor(new Date(iso ?? Date.now()).getTime() / 1000);

export function LiveView() {
  const [password, setPassword] = useState("");
  const [data, setData] = useState<LiveStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [tab, setTabState] = useState<LiveTab>("open");

  useAdminResume(setPassword);

  useEffect(() => {
    const read = () => {
      const h = window.location.hash.slice(1);
      if (h === "history" || h === "wallets" || h === "open" || h === "coins" || h === "orders" || h === "bots") setTabState(h);
    };
    read();
    window.addEventListener("hashchange", read);
    return () => window.removeEventListener("hashchange", read);
  }, []);
  const setTab = (t: LiveTab) => {
    setTabState(t);
    history.replaceState(null, "", t === "open" ? window.location.pathname : `#${t}`);
  };

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

  const peaks = usePeaks(password, (data?.accounts ?? []).flatMap((a) => a.open)
    .map((p) => ({ mint: p.token_address, since: sinceOf(p.opened_at) })).slice(0, 25));

  async function setTarget(p: LivePositionOut, body: TargetBody) {
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

  /** New buys on every account (global switch). */
  async function toggleAll() {
    if (!data) return;
    setBusy("all");
    try {
      await api.livePause(password, !data.paused);
      await load(password);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not change it.");
    } finally {
      setBusy(null);
    }
  }

  /** New buys on one account (same switch as on /accounts). */
  async function toggleAccount(a: LiveAccount) {
    setBusy(a.name);
    try {
      await api.editAccount(password, a.name, { paused: !a.paused });
      await load(password);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not change it.");
    } finally {
      setBusy(null);
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
  const openCount = data ? data.accounts.reduce((s, a) => s + a.open.length, 0) : 0;
  const closedCount = data ? data.accounts.reduce((s, a) => s + a.closed.length, 0) : 0;
  const fresh = tick - now < REFRESH_MS * 3;

  return (
    <>
    <AppNav />
    <main className={pageMain}>
      <PageHeader icon={<Wallet />} title="Live copy trading"
        badges={data && (
          <Badge tone={data.mode === "live" ? "mint" : data.mode === "dry" ? "gold" : "muted"} dot>
            {data.mode === "live" ? "LIVE" : data.mode === "dry" ? "DRY-RUN" : "OFF"}
          </Badge>
        )}
        subtitle={data && (
          <span className="inline-flex flex-wrap items-center gap-x-2">
            <span className={`size-1.5 rounded-full ${fresh ? "animate-pulse bg-mint" : "bg-warn"}`} />
            updated {Math.max(0, Math.round((tick - now) / 1000))}s ago
            {data.sol_usd ? <span>· SOL <span className="num">{money(data.sol_usd)}</span></span> : null}
            {data.mode !== "live" && (
              <span className="text-faint">· {data.mode === "dry" ? "Dry-run: real quotes, nothing is sent." : "Copy trading is off on the server."}</span>
            )}
          </span>
        )}
        actions={data && data.mode !== "off" && (
          <label className="card inline-flex items-center gap-2.5 px-3.5 py-2 text-sm" title="Off: no new buys on any account; open positions still sell">
            <Toggle on={!data.paused} busy={busy === "all"} label="New buys, all accounts" onClick={toggleAll} />
            <span className={data.paused ? "text-gold" : "text-ink"}>{data.paused ? "All buys paused" : "Buying"}</span>
          </label>
        )} />

      {error && <p className="card mb-4 border-bear/40 p-4 text-sm text-bear">{error}</p>}

      {!data ? (
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          {[0, 1, 2, 3].map((i) => <span key={i} className="skeleton h-28 rounded-2xl" />)}
        </div>
      ) : (
        <>
          <section className="mb-6 grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
            <StatTile label="Live P&L · all accounts" value={money(totalLive, true)} tone={tone(totalLive) || "text-ink"}
              sub={`realized ${money(totalReal, true)} · open ${money((totalLive ?? 0) - (totalReal ?? 0), true)}`} />
            <StatTile label="Realized · 24h" value={money(total24, true)} tone={tone(total24) || "text-ink"}
              sub={`${closedCount} closed trades in history`} />
            <StatTile label="In open trades" value={money(totalOpen)}
              sub={`${openCount} open across ${data.accounts.length} accounts`} />
            <StatTile label="Wallets" value={`${(totalSol ?? 0).toFixed(3)} SOL`} sub={`≈ ${money(totalSolUsd)} free to trade`} />
          </section>

          <SectionTabs label="Live sections" value={tab} onChange={setTab}
            tabs={[
              { key: "open", label: `Open trades · ${openCount}`, icon: <Activity /> },
              { key: "bots", label: "Curve bots", icon: <Bot /> },
              { key: "coins", label: "Wallet coins", icon: <Coins /> },
              { key: "orders", label: "Price orders", icon: <ListChecks /> },
              { key: "history", label: `History · ${closedCount}`, icon: <History /> },
              { key: "wallets", label: "Wallet activity", icon: <Radio /> },
            ]} />

          <div key={tab} className="flex animate-fade-in flex-col gap-6">
            {tab === "open" && data.accounts.map((a) => (
              <OpenAccount key={a.name} a={a} now={tick} busy={busy === a.name} globalPaused={data.paused} peaks={peaks}
                onToggle={() => toggleAccount(a)} onSell={sellPosition} onTarget={setTarget} />
            ))}
            {tab === "bots" && <CurveBotsPanel password={password} />}
            {tab === "coins" && <WalletCoins password={password} onSell={sellPosition} onTarget={setTarget} />}
            {tab === "orders" && <OrdersPanel password={password} accounts={data.accounts.map((a) => a.name)} now={tick} />}
            {tab === "history" && <HistoryPanel accounts={data.accounts} now={tick} onSell={sellPosition} />}
            {tab === "wallets" && (
              data.leader_activity.length ? <LeaderFeed rows={data.leader_activity} now={tick} />
                : <Panel title="Copied wallet activity"><Empty>No activity yet.</Empty></Panel>
            )}
          </div>
        </>
      )}
    </main>
    </>
  );
}

/** One account on the Open tab: a compact header with its switch, then its open trades as cards. */
function OpenAccount({ a, now, busy, globalPaused, onToggle, onSell, onTarget, peaks }: {
  a: LiveAccount;
  now: number;
  busy: boolean;
  globalPaused: boolean;
  peaks: Record<string, Peak>;
  onToggle: () => void;
  onSell: (p: LivePositionOut, share: number) => Promise<void>;
  onTarget?: (p: LivePositionOut, body: TargetBody) => Promise<string>;
}) {
  const livePnl = a.realized_usd + (a.unrealized_usd ?? 0);
  const used = Math.min(100, (a.open_cost_usd / Math.max(a.max_open_usd, 1)) * 100);
  const rows = [...a.open].sort((x, y) => (y.opened_at ?? "").localeCompare(x.opened_at ?? ""));
  return (
    <section className="card overflow-hidden">
      <header className="flex flex-wrap items-center gap-x-5 gap-y-3 border-b border-line px-5 py-4">
        <div className="flex items-center gap-2">
          <h2 className="num text-[15px] font-semibold">{a.name}</h2>
          {!a.active && <Badge tone="gold">idle: no key on the server</Badge>}
          {a.pubkey && (
            <a href={`https://solscan.io/account/${a.pubkey}`} target="_blank" rel="noreferrer"
              className="num inline-flex items-center gap-1 text-xs text-muted hover:text-ink">
              {short(a.pubkey)} <ExternalLink className="size-3" />
            </a>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-x-5 gap-y-1 text-xs text-muted">
          <span>Wallet <b className="num font-medium text-ink">{a.sol_balance != null ? `${a.sol_balance.toFixed(3)} SOL` : "—"}</b></span>
          <span>P&amp;L <b className={`num font-medium ${tone(livePnl) || "text-ink"}`}>{money(livePnl, true)}</b></span>
          <span>24h <b className={`num font-medium ${tone(a.realized_24h) || "text-ink"}`}>{money(a.realized_24h, true)}</b></span>
          <span className="flex items-center gap-2">
            Budget <b className="num font-medium text-ink">{money(a.open_cost_usd)}</b>/{money(a.max_open_usd)}
            <span className="h-1.5 w-20 overflow-hidden rounded-full bg-raised">
              <span className="block h-full rounded-full bg-mint transition-all duration-700" style={{ width: `${used}%` }} />
            </span>
          </span>
        </div>
        <label className="ms-auto flex items-center gap-2 text-xs" title="Off: no new buys on this account; open positions still sell">
          <Toggle on={!a.paused && !globalPaused} busy={busy || globalPaused} label={`New buys on ${a.name}`} onClick={onToggle} />
          <span className={!a.paused && !globalPaused ? "text-mint" : "text-muted"}>
            {globalPaused ? "Paused (all)" : a.paused ? "Buys paused" : "Buying"}
          </span>
        </label>
      </header>
      {rows.length === 0 ? (
        <Empty>No open trades. {a.failed.length ? `${a.failed.length} buys didn't go through (nothing spent).` : ""}</Empty>
      ) : (
        <div className="grid items-start gap-3 p-4 md:grid-cols-2 xl:grid-cols-3">
          {rows.map((p) => <OpenCard key={p.id} p={p} now={now} onSell={onSell} onTarget={onTarget} peak={peaks[p.token_address]} />)}
        </div>
      )}
    </section>
  );
}

function OpenCard({ p, now, onSell, onTarget, peak }: {
  p: LivePositionOut;
  now: number;
  peak?: Peak;
  onSell: (p: LivePositionOut, share: number) => Promise<void>;
  onTarget?: (p: LivePositionOut, body: TargetBody) => Promise<string>;
}) {
  const pnl = p.unrealized_usd ?? null;
  const pp = p.unrealized_pct ?? null;
  const up = (pnl ?? 0) >= 0;
  return (
    <div className="group relative flex flex-col gap-3 overflow-hidden rounded-xl border border-line bg-surface-2/60 p-4 transition hover:border-line-strong">
      <span className={`absolute inset-y-0 start-0 w-[3px] ${pnl == null ? "bg-faint" : up ? "bg-mint" : "bg-bear"}`} />
      <div className="flex items-start gap-3">
        <span className={`grid size-9 shrink-0 place-items-center rounded-full text-xs font-bold ${pnl == null ? "bg-raised text-muted" : up ? "bg-mint-soft text-mint" : "bg-bear-soft text-bear"}`}>
          {(p.symbol || "?").slice(0, 2).toUpperCase()}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <a href={`/token/solana/${p.token_address}`} className="truncate font-semibold hover:text-mint">
              ${p.symbol || short(p.token_address)}
            </a>
            {p.mode === "dry" && <span className="rounded border border-line px-1 text-[10px] text-faint">DRY</span>}
            {p.hands_off && (p.leader_wallet?.startsWith("curve:")
              ? <Badge tone="gold">🤖 {p.leader_wallet.slice(6)}</Badge>
              : <Badge tone="sky">✋ yours</Badge>)}
          </div>
          <div className="text-[11px] text-faint">
            {ago(p.opened_at, now)} · in {money(p.cost_usd)}{p.proceeds_usd ? ` · out ${money(p.proceeds_usd)}` : ""}
          </div>
        </div>
        <div className="text-right">
          <div className={`num text-base font-semibold ${tone(pnl)}`}>{money(pnl, true)}</div>
          <div className={`num text-[11px] ${tone(pp)}`}>{pp != null ? pct(pp) : "no price yet"}</div>
        </div>
      </div>
      <div className="flex items-center justify-between text-[11px] text-muted">
        <span>value <b className="num font-medium text-ink">{money(p.value_usd)}</b></span>
        {p.price_usd != null && <span className="num">@ {price(p.price_usd)}</span>}
        {peak && <span className="num text-mint" title={`highest since you bought, ${new Date(peak.at).toLocaleString()}`}>high {usdPrice(peak.price)}</span>}
        <span className="flex gap-2">{p.fills.filter((f) => f.side !== "sell_failed").slice(-2).map((f, i) => <TxLink key={i} sig={f.sig} />)}</span>
      </div>
      <TradeBar p={p} peak={peak} />
      {p.error && <span className="text-[11px] text-bear">sell retrying: {p.error.slice(0, 60)}</span>}
      <div className="flex flex-wrap items-center gap-1.5 border-t border-line pt-3 text-xs">
        {onTarget && <TargetEditor p={p} onTarget={onTarget} peak={peak} />}
        <SellButtons p={p} onSell={onSell} />
      </div>
    </div>
  );
}

type HistFilter = "all" | "wins" | "losses";

/** Closed trades of every account, newest first, with filters. */
function HistoryPanel({ accounts, now, onSell }: {
  accounts: LiveAccount[];
  now: number;
  onSell: (p: LivePositionOut, share: number) => Promise<void>;
}) {
  const [acct, setAcct] = useState("");
  const [f, setF] = useState<HistFilter>("all");
  const all = accounts.flatMap((a) => a.closed.map((p) => ({ ...p, account: a.name })))
    .sort((x, y) => (y.closed_at ?? y.opened_at ?? "").localeCompare(x.closed_at ?? x.opened_at ?? ""));
  const rows = all.filter((p) => (!acct || p.account === acct)
    && (f === "all" || (f === "wins" ? (p.pnl_usd ?? 0) > 0 : (p.pnl_usd ?? 0) <= 0)));
  const net = rows.reduce((s, p) => s + (p.pnl_usd ?? 0), 0);
  const wins = rows.filter((p) => (p.pnl_usd ?? 0) > 0).length;
  return (
    <Panel icon={<History />} title="History"
      meta={<>{rows.length} trades · net <span className={tone(net)}>{money(net, true)}</span>{rows.length ? ` · ${Math.round((wins / rows.length) * 100)}% wins` : ""}</>}
      actions={
        <>
          <Segmented label="Result" value={f} onChange={setF}
            options={[{ value: "all", label: "All" }, { value: "wins", label: "Wins" }, { value: "losses", label: "Losses" }]} />
          {accounts.length > 1 && (
            <Segmented label="Account" value={acct} onChange={setAcct}
              options={[{ value: "", label: "All" }, ...accounts.map((a) => ({ value: a.name, label: a.name }))]} />
          )}
        </>
      } flush>
      {rows.length === 0 ? <Empty>No closed trades yet.</Empty> : (
        <div className="overflow-x-auto">
          <table className="data-table min-w-[900px]">
            <thead>
              <tr>
                <th className="text-left">Token</th>
                <th className="text-right">Opened</th>
                <th className="text-right">Cost</th>
                <th className="text-right">Sold for</th>
                <th className="text-right">P&amp;L</th>
                <th className="text-left">Exit</th>
                <th className="text-left">Fills</th>
              </tr>
            </thead>
            <tbody>
              <PositionRows rows={rows} now={now} onSell={onSell} accountOf={(p) => (p as LivePositionOut & { account: string }).account}
                showAccount={accounts.length > 1} closedOnly />
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

function LeaderFeed({ rows, now }: { rows: LeaderTrade[]; now: number }) {
  return (
    <Panel icon={<Radio />} title="Copied wallet activity" meta="latest buys and sells of the wallets being copied" flush>
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

/** Every coin in each account's wallet, with a sell price / stop on any of them. */
function WalletCoins({ password, onSell, onTarget }: {
  password: string;
  onSell: (p: LivePositionOut, share: number) => Promise<void>;
  onTarget: (p: LivePositionOut, body: TargetBody) => Promise<string>;
}) {
  const [data, setData] = useState<Holdings | null>(null);
  const [error, setError] = useState("");
  const [small, setSmall] = useState(false);
  const day = Math.floor(Date.now() / 1000 / 3600) * 3600 - 86400;        // stable for an hour
  const peaks = usePeaks(password, (data?.accounts ?? []).flatMap((a) => a.coins
    .filter((c) => !c.stable && ((c.value_usd ?? 0) >= 1 || c.position))
    .map((c) => ({ mint: c.mint, since: c.position ? sinceOf(c.position.opened_at) : day }))).slice(0, 25));

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setData(await api.holdings(password, signal));
      setError("");
    } catch (e) {
      if ((e as Error).name !== "AbortError") setError(e instanceof ApiError ? e.message : "Could not read the wallets.");
    }
  }, [password]);

  useEffect(() => {
    const ctl = new AbortController();
    load(ctl.signal);
    const id = setInterval(() => !document.hidden && load(ctl.signal), 15_000);
    return () => {
      clearInterval(id);
      ctl.abort();
    };
  }, [load]);

  async function holdTarget(account: string, c: HoldingCoin, body: { share: number; target_price: number | null; stop_price: number | null }) {
    try {
      const r = await api.holdingsTarget(password, { account, mint: c.mint, ...body });
      await load();
      return r.message;
    } catch (e) {
      throw new Error(e instanceof ApiError ? e.message : "Could not save the target.");
    }
  }
  async function positionTarget(p: LivePositionOut, body: TargetBody) {
    const m = await onTarget(p, body);
    await load();
    return m;
  }

  if (error) return <p className="card p-4 text-sm text-bear">{error}</p>;
  if (!data) return <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">{[0, 1, 2].map((i) => <span key={i} className="skeleton h-32 rounded-2xl" />)}</div>;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center gap-3 text-xs text-muted">
        <span>Every coin in the wallets, read from the chain every 15 s. Set a sell price on any of them: the bot sells it when the price reaches it.</span>
        <label className="ms-auto flex items-center gap-2">
          <Toggle on={small} label="Show coins under $1" onClick={() => setSmall(!small)} />
          Show coins under $1
        </label>
      </div>
      {data.accounts.map((a) => {
        const coins = a.coins.filter((c) => small || (c.value_usd ?? 0) >= 1 || c.position);
        const total = a.coins.reduce((s, c) => s + (c.value_usd ?? 0), 0);
        const hidden = a.coins.length - coins.length;
        return (
          <section key={a.name} className="card overflow-hidden">
            <header className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-line px-5 py-4">
              <h2 className="num text-[15px] font-semibold">{a.name}</h2>
              <a href={`https://solscan.io/account/${a.pubkey}`} target="_blank" rel="noreferrer"
                className="num inline-flex items-center gap-1 text-xs text-muted hover:text-ink">
                {short(a.pubkey)} <ExternalLink className="size-3" />
              </a>
              <span className="text-xs text-muted">{a.coins.length} coins · worth <b className="num font-medium text-ink">{money(total)}</b></span>
              {hidden > 0 && <span className="text-xs text-faint">{hidden} under $1 hidden</span>}
            </header>
            {coins.length === 0 ? <Empty>No coins in this wallet.</Empty> : (
              <div className="grid items-start gap-3 p-4 md:grid-cols-2 xl:grid-cols-3">
                {coins.map((c) => (
                  <CoinCard key={c.mint} c={c} account={a.name} onSell={onSell} onPositionTarget={positionTarget}
                    onHoldTarget={holdTarget} peak={peaks[c.mint]} />
                ))}
              </div>
            )}
          </section>
        );
      })}
    </div>
  );
}

function CoinCard({ c, account, onSell, onPositionTarget, onHoldTarget, peak }: {
  c: HoldingCoin;
  peak?: Peak;
  account: string;
  onSell: (p: LivePositionOut, share: number) => Promise<void>;
  onPositionTarget: (p: LivePositionOut, body: TargetBody) => Promise<string>;
  onHoldTarget: (account: string, c: HoldingCoin, body: { share: number; target_price: number | null; stop_price: number | null }) => Promise<string>;
}) {
  const p = c.position;
  return (
    <div className="relative flex flex-col gap-3 overflow-hidden rounded-xl border border-line bg-surface-2/60 p-4 transition hover:border-line-strong">
      <div className="flex items-start gap-3">
        <span className={`grid size-9 shrink-0 place-items-center rounded-full text-xs font-bold ${p ? "bg-mint-soft text-mint" : "bg-raised text-ink"}`}>
          {c.symbol.slice(0, 2).toUpperCase()}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <a href={`/token/solana/${c.mint}`} className="truncate font-semibold hover:text-mint">${c.symbol}</a>
            {p ? <Badge tone={p.hands_off ? "sky" : "mint"}>{p.hands_off ? "✋ yours" : "bot position"}</Badge>
              : c.stable ? <Badge tone="muted">stable</Badge> : <Badge tone="muted">not traded by the bot</Badge>}
          </div>
          <div className="num text-[11px] text-faint">
            {c.amount.toLocaleString("en-US", { maximumFractionDigits: c.amount < 1 ? 6 : 2 })} · @ {usdPrice(c.price_usd)}
            {peak && <span className="text-mint"> · {p ? "high since bought" : "24h high"} {usdPrice(peak.price)}</span>}
          </div>
        </div>
        <div className="text-right">
          <div className="num text-base font-semibold text-ink">{c.value_usd != null ? money(c.value_usd) : "—"}</div>
          {p && p.unrealized_pct != null && <div className={`num text-[11px] ${tone(p.unrealized_pct)}`}>{pct(p.unrealized_pct)}</div>}
        </div>
      </div>
      {!c.stable && (
        <div className="flex flex-wrap items-center gap-1.5 border-t border-line pt-3 text-xs">
          {p ? (
            <>
              <TargetEditor p={{ ...p, price_usd: p.price_usd ?? c.price_usd, value_usd: p.value_usd ?? c.value_usd }}
                onTarget={onPositionTarget} peak={peak} />
              <SellButtons p={p} onSell={onSell} />
            </>
          ) : (
            <HoldTarget c={c} peak={peak} onSave={(body) => onHoldTarget(account, c, body)} />
          )}
        </div>
      )}
    </div>
  );
}

/** Sell price / stop for a coin the bot does not hold yet (price only: there is no entry to count % from). */
function HoldTarget({ c, onSave, peak }: {
  c: HoldingCoin;
  peak?: Peak;
  onSave: (body: { share: number; target_price: number | null; stop_price: number | null }) => Promise<string>;
}) {
  const [open, setOpen] = useState(false);
  const [tpx, setTpx] = useState("");
  const [spx, setSpx] = useState("");
  const [share, setShare] = useState("100");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const box = "num w-16 rounded-md border border-line bg-carbon px-1.5 py-1 text-center text-xs text-ink outline-none focus:border-gold/70";
  const wide = "num w-32 rounded-md border border-line bg-carbon px-2 py-1 text-xs text-ink outline-none focus:border-gold/70";

  async function save() {
    const num = (v: string) => (v.trim() === "" ? null : Number(v.replace(/[$,\s]/g, "")));
    const t = num(tpx), st = num(spx);
    if (t == null && st == null) return setMsg("Enter a sell price or a stop price");
    if ((t != null && !(t > 0)) || (st != null && !(st > 0))) return setMsg("Prices must be numbers above 0");
    if (t != null && st != null && st >= t) return setMsg("The stop price must be below the sell price");
    setBusy(true);
    setMsg("");
    try {
      setMsg(await onSave({ share: Math.min(Math.max(Number(share) || 100, 5), 100) / 100, target_price: t, stop_price: st }));
      setOpen(false);
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Could not save");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {!open ? (
        <button onClick={() => setOpen(true)}
          className="inline-flex items-center gap-1 rounded-md border border-gold/50 bg-gold-soft px-2.5 py-1 font-medium text-gold hover:border-gold">
          <Crosshair className="size-3.5" /> Sell at a price
        </button>
      ) : (
        <span className="flex basis-full flex-col gap-2 rounded-lg border border-gold/30 bg-carbon/60 p-3 text-xs text-muted">
          <span className="text-faint">now <b className="num font-medium text-ink">{usdPrice(c.price_usd)}</b></span>
          {peak && (
            <span className="flex flex-wrap items-center gap-2 rounded-md bg-mint-soft/50 px-2 py-1 text-mint">
              <TrendingUp className="size-3.5" /> 24h high <b className="num font-medium">{usdPrice(peak.price)}</b>
              <button type="button" className="ms-auto rounded border border-mint/50 px-1.5 py-0.5 hover:bg-mint-soft"
                onClick={() => setTpx(String(Number(peak.price.toPrecision(4))))}>
                Use it
              </button>
            </span>
          )}
          <span className="flex flex-wrap items-center gap-1.5">
            Sell <input className={box} inputMode="decimal" value={share} onChange={(e) => setShare(e.target.value)} />
            % when the price reaches $
            <input className={wide} inputMode="decimal" placeholder={c.price_usd ? String(Number((c.price_usd * 2).toPrecision(3))) : "price"}
              value={tpx} onChange={(e) => setTpx(e.target.value)} />
          </span>
          <span className="flex flex-wrap items-center gap-1.5">
            Stop: sell everything if it drops to $
            <input className={wide} inputMode="decimal" placeholder="optional" value={spx} onChange={(e) => setSpx(e.target.value)} />
          </span>
          <span className="text-faint">The bot will watch it and sell only at your prices (no other rule touches it).</span>
          <span className="flex flex-wrap gap-1.5">
            <button disabled={busy} onClick={save}
              className="rounded-md bg-gold px-3 py-1 font-semibold text-carbon hover:brightness-110 disabled:opacity-50">
              Save
            </button>
            <button onClick={() => setOpen(false)} className="rounded-md border border-line px-2.5 py-1 text-muted">Cancel</button>
          </span>
        </span>
      )}
      {msg && <span className="basis-full text-[11px] text-faint">{msg}</span>}
    </>
  );
}


/** Price orders: buy a coin once it falls to / rises to a price, then (optionally) sell at a price or stop. */
function OrdersPanel({ password, accounts, now }: { password: string; accounts: string[]; now: number }) {
  const [orders, setOrders] = useState<PriceOrderOut[] | null>(null);
  const [all, setAll] = useState(false);
  const [error, setError] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [f, setF] = useState({
    account: accounts[0] ?? "main", mint: "", condition: "below" as "below" | "above", trigger: "", amount: "20",
    target: "", share: "100", stop: "", hours: "24", over: "5",
  });
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const box = "num rounded-md border border-line bg-carbon px-2 py-1.5 text-xs text-ink outline-none focus:border-gold/70";

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setOrders((await api.orders(password, all, signal)).orders);
      setError("");
    } catch (e) {
      if ((e as Error).name !== "AbortError") setError(e instanceof ApiError ? e.message : "Could not read the orders.");
    }
  }, [password, all]);

  useEffect(() => {
    const ctl = new AbortController();
    load(ctl.signal);
    const id = setInterval(() => !document.hidden && load(ctl.signal), 10_000);
    return () => {
      clearInterval(id);
      ctl.abort();
    };
  }, [load]);

  async function place() {
    const num = (v: string) => (v.trim() === "" ? null : Number(v.replace(/[$,\s]/g, "")));
    const trigger = num(f.trigger), amount = num(f.amount);
    if (!f.mint.trim()) return setMsg("Paste the coin address");
    if (!trigger || !(trigger > 0)) return setMsg("Enter the price that triggers the buy");
    if (!amount || !(amount > 0)) return setMsg("Enter the amount in $");
    const body: PriceOrderIn = {
      account: f.account, mint: f.mint.trim(), condition: f.condition, trigger_price: trigger, amount_usd: amount,
      max_over_pct: num(f.over), target_price: num(f.target), target_share: (num(f.share) ?? 100) / 100,
      stop_price: num(f.stop), expires_hours: num(f.hours),
    };
    setBusy(true);
    setMsg("");
    try {
      const r = await api.placeOrder(password, body);
      setMsg(`${r.message}: #${r.order.id} $${r.order.symbol}`);
      setF({ ...f, mint: "", trigger: "", target: "", stop: "" });
      await load();
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : "Could not place the order");
    } finally {
      setBusy(false);
    }
  }

  async function cancel(o: PriceOrderOut) {
    try {
      await api.cancelOrder(password, o.id);
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not cancel");
    }
  }

  const statusTone = (s: PriceOrderOut["status"]) =>
    s === "open" ? "gold" : s === "filled" ? "mint" : s === "failed" ? "bear" : "muted";

  return (
    <div className="flex flex-col gap-6">
      <Panel title="New price order">
        <div className="flex flex-col gap-3 p-4 text-xs text-muted">
          <span className="flex flex-wrap items-center gap-2">
            Account
            <select className={box} value={f.account} onChange={set("account")}>
              {accounts.map((a) => <option key={a} value={a}>{a}</option>)}
            </select>
            Coin address
            <input className={`${box} w-[26rem] max-w-full`} placeholder="paste the mint address" value={f.mint} onChange={set("mint")} />
          </span>
          <span className="flex flex-wrap items-center gap-2">
            Buy $<input className={`${box} w-20`} inputMode="decimal" value={f.amount} onChange={set("amount")} />
            when the price
            <select className={box} value={f.condition} onChange={set("condition")}>
              <option value="below">falls to</option>
              <option value="above">rises to</option>
            </select>
            $<input className={`${box} w-36`} inputMode="decimal" placeholder="price per coin" value={f.trigger} onChange={set("trigger")} />
            <span className="text-faint">pay at most</span>
            <input className={`${box} w-14 text-center`} inputMode="decimal" value={f.over} onChange={set("over")} />
            <span className="text-faint">% above it</span>
          </span>
          <span className="flex flex-wrap items-center gap-2">
            Then sell <input className={`${box} w-14 text-center`} inputMode="decimal" value={f.share} onChange={set("share")} />
            % at $<input className={`${box} w-36`} inputMode="decimal" placeholder="optional" value={f.target} onChange={set("target")} />
            stop at $<input className={`${box} w-36`} inputMode="decimal" placeholder="optional" value={f.stop} onChange={set("stop")} />
            expires in <input className={`${box} w-16 text-center`} inputMode="decimal" value={f.hours} onChange={set("hours")} /> h
          </span>
          <span className="text-faint">
            The bot watches the price and buys through the cheapest route (PumpPortal or Jupiter, fees included) once
            it gets there. If the coin jumps past your limit before the buy lands, the order keeps waiting. Prices can
            gap: a stop can fill below its price.
          </span>
          <span className="flex flex-wrap items-center gap-2">
            <button disabled={busy} onClick={place}
              className="rounded-md bg-gold px-3 py-1.5 font-semibold text-carbon hover:brightness-110 disabled:opacity-50">
              Place order
            </button>
            {msg && <span className="text-faint">{msg}</span>}
          </span>
        </div>
      </Panel>

      <Panel title="Orders">
        <div className="flex items-center gap-2 border-b border-line px-4 py-2 text-xs text-muted">
          <Toggle on={all} label="Show finished orders" onClick={() => setAll(!all)} /> Show finished orders
        </div>
        {error ? <p className="p-4 text-sm text-bear">{error}</p>
          : !orders ? <span className="skeleton m-4 block h-16 rounded-xl" />
            : orders.length === 0 ? <Empty>No {all ? "" : "open "}orders.</Empty> : (
              <div className="flex flex-col divide-y divide-line">
                {orders.map((o) => {
                  const dist = o.last_price ? (o.trigger_price / o.last_price - 1) * 100 : null;
                  return (
                    <div key={o.id} className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-3 text-xs">
                      <span className="num text-faint">#{o.id}</span>
                      <a href={`/token/solana/${o.mint}`} className="font-semibold text-ink hover:text-mint">${o.symbol}</a>
                      <Badge tone={statusTone(o.status)}>{o.status}</Badge>
                      <span className="text-muted">
                        buy <b className="num text-ink">{money(o.amount_usd)}</b> when it {o.condition === "below" ? "falls to" : "rises to"}{" "}
                        <b className="num text-ink">{usdPrice(o.trigger_price)}</b>
                      </span>
                      {o.status === "open" && o.last_price != null && (
                        <span className="num text-faint">
                          now {usdPrice(o.last_price)}{dist != null && ` · ${dist > 0 ? "+" : ""}${dist.toFixed(1)}% away`}
                        </span>
                      )}
                      {(o.then_target_price || o.then_stop_price) && (
                        <span className="num text-faint">
                          then{o.then_target_price ? ` sell ${Math.round((o.then_target_share ?? 1) * 100)}% at ${usdPrice(o.then_target_price)}` : ""}
                          {o.then_stop_price ? ` · stop ${usdPrice(o.then_stop_price)}` : ""}
                        </span>
                      )}
                      <span className="num text-faint">{o.account}</span>
                      {o.status === "open" && o.expires_at && <span className="text-faint">expires {ago(o.expires_at, now).replace(" ago", "")}</span>}
                      {o.note && o.status !== "open" && <span className="basis-full text-faint">{o.note}</span>}
                      {o.note && o.status === "open" && <span className="basis-full text-gold">{o.note}</span>}
                      {o.status === "open" && (
                        <button onClick={() => cancel(o)} className="ms-auto rounded-md border border-line px-2 py-1 text-muted hover:border-bear/60 hover:text-bear">
                          Cancel
                        </button>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
      </Panel>
    </div>
  );
}

/** Curve bots: each live-tradeable curve rule with an ON/OFF switch per account and its own entry per trade. */
function CurveBotsPanel({ password }: { password: string }) {
  const [d, setD] = useState<CurveBots | null>(null);
  const [error, setError] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState("");
  const [sizes, setSizes] = useState<Record<string, string>>({});   // "account/rule" -> typed entry
  const box = "num w-20 rounded-md border border-line bg-carbon px-2 py-1 text-xs text-ink outline-none focus:border-gold/70";

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setD(await api.curveBots(password, signal));
      setError("");
    } catch (e) {
      if ((e as Error).name !== "AbortError") setError(e instanceof ApiError ? e.message : "Could not read the curve bots.");
    }
  }, [password]);

  useEffect(() => {
    const ctl = new AbortController();
    load(ctl.signal);
    const id = setInterval(() => !document.hidden && load(ctl.signal), 15_000);
    return () => {
      clearInterval(id);
      ctl.abort();
    };
  }, [load]);

  async function put(account: string, rule: string, enabled: boolean, size: number | null, ask: boolean) {
    if (ask && enabled && !confirm(`Turn ${rule} ON with REAL money on ${account}${size ? `, $${size} per trade` : ""}? `
      + "It buys every time the paper bot enters this rule, within the account's budget and daily loss limit.")) return;
    const key = `${account}/${rule}`;
    setBusy(key);
    setMsg("");
    try {
      const r = await api.setCurveBot(password, account, rule, { enabled, size_usd: size });
      setMsg(r.message);
      setSizes((x) => Object.fromEntries(Object.entries(x).filter(([k]) => k !== key)));
      await load();
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : "Could not save it.");
    } finally {
      setBusy("");
    }
  }

  if (error) return <Panel title="Curve bots"><p className="p-4 text-sm text-bear">{error}</p></Panel>;
  if (!d) return <span className="skeleton block h-40 rounded-2xl" />;
  if (!d.rules.length) return <Panel title="Curve bots"><Empty>No curve rule can trade for real yet (it needs an exit level and a stop).</Empty></Panel>;

  const rule = (r: CurveBots["rules"][number]) => [
    `buy at ${r.level}%`,
    r.exit_at != null ? `sell at ${r.exit_at}%` : null,
    r.sl != null ? `stop ${r.sl}%` : null,
    r.max_hold_s ? `max ${Math.round(r.max_hold_s / 60)} min` : null,
  ].filter(Boolean).join(" · ");

  return (
    <div className="flex flex-col gap-6">
      {msg && <p className="card p-3 text-xs text-muted">{msg}</p>}
      {d.accounts.map((a) => (
        <Panel key={a.name} title={`${a.name} · curve bots`}>
          <div className="flex flex-col divide-y divide-line">
            {d.rules.map((r) => {
              const b = a.bots[r.name] ?? { enabled: false, size_usd: null, live_24h: { trades: 0, pnl_usd: 0 } };
              const key = `${a.name}/${r.name}`;
              const typed = sizes[key];
              const shown = typed ?? (b.size_usd != null ? String(b.size_usd) : "");
              const parsed = shown.trim() === "" ? null : Number(shown.replace(/[$,\s]/g, ""));
              const changed = typed !== undefined && parsed !== b.size_usd;
              const valid = parsed === null || (Number.isFinite(parsed) && parsed >= 1);
              return (
                <div key={r.name} className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3 text-xs">
                  <Toggle on={b.enabled} busy={busy === key} label={`${r.name} on ${a.name}`}
                    onClick={() => put(a.name, r.name, !b.enabled, valid ? parsed : b.size_usd, true)} />
                  <span className="min-w-[9rem]">
                    <b className={b.enabled ? "text-mint" : "text-ink"}>{r.name}</b>
                    <span className="block text-faint">{rule(r)}</span>
                  </span>
                  <span className="flex items-center gap-1.5 text-muted">
                    $<input className={box} inputMode="decimal" placeholder={String(a.size_usd)} value={shown}
                      onChange={(e) => setSizes({ ...sizes, [key]: e.target.value })}
                      onKeyDown={(e) => e.key === "Enter" && changed && valid && put(a.name, r.name, b.enabled, parsed, false)} />
                    per trade
                    {changed && (
                      <button disabled={!valid || busy === key} onClick={() => put(a.name, r.name, b.enabled, parsed, false)}
                        className="rounded-md bg-gold px-2 py-1 font-semibold text-carbon hover:brightness-110 disabled:opacity-50">
                        Save
                      </button>
                    )}
                  </span>
                  <span className="num text-faint">
                    paper 24h: {r.paper_24h.trades} trades · <span className={tone(r.paper_24h.pnl_usd)}>{money(r.paper_24h.pnl_usd, true)}</span>
                  </span>
                  <span className="num text-faint">
                    real 24h: {b.live_24h.trades} trades · <span className={tone(b.live_24h.pnl_usd)}>{money(b.live_24h.pnl_usd, true)}</span>
                  </span>
                </div>
              );
            })}
          </div>
          <p className="border-t border-line px-4 py-2 text-[11px] text-faint">
            Empty entry = the account&apos;s entry per trade (${a.size_usd}). One coin is bought once per account, even
            when two bots pick it. Every bot shares the account&apos;s budget and daily loss limit.
          </p>
        </Panel>
      ))}
    </div>
  );
}
