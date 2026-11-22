"use client";

import {
  Activity,
  AlertTriangle,
  BarChart3,
  Bell,
  Cpu,
  ExternalLink,
  Layers,
  LayoutDashboard,
  LayoutGrid,
  Radar,
  Receipt,
  RefreshCw,
  Rows3,
  Search,
  Sparkles,
  Target,
  Ticket,
  TrendingDown,
  TrendingUp,
  UserPlus,
  Users,
  Wallet,
  XCircle,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { AdminSignIn, adminSignOut, useAdminResume } from "@/components/AdminAuth";
import { AppNav } from "@/components/AppNav";
import { Badge, button, Empty, PageHeader, pageMain, Panel, SectionTabs, Toggle } from "@/components/ui";
import { api, ApiError, SESSION } from "@/lib/api";
import { pct, pctClass } from "@/lib/format";
import type {
  AdminOverview, Bill, BillingAdmin, BillingConfig, Coupon, OverviewAccount, ScoutCandidate, ScoutList, TelegramPrefs,
} from "@/lib/types";

const KEY = "alpha-live-password"; // same password as the Live and Accounts pages
const REFRESH_MS = 15_000;
const STALE_MIN = 5; // the host watchdog uses the same limit for engine heartbeats

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

const input =
  "w-full rounded-md border border-line bg-carbon px-2.5 py-1.5 text-sm outline-none focus:border-accent";
const navBtn =
  "inline-flex items-center gap-1 rounded-md border border-line px-2 py-1 text-xs text-muted hover:bg-raised hover:text-ink";

/** $1,234.56 with an optional sign; null -> em dash. */
function money(v: number | null | undefined, signed = false): string {
  if (v == null || !Number.isFinite(v)) return "—";
  const s = Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (!signed) return `${v < 0 ? "-" : ""}$${s}`;
  return `${v > 0 ? "+" : v < 0 ? "-" : ""}$${s}`;
}

function sol(v: number | null | undefined): string {
  return v == null ? "—" : `${v.toLocaleString("en-US", { maximumFractionDigits: 3 })} SOL`;
}

function since(iso: string | null | undefined, now: number): string {
  if (!iso) return "never";
  const m = Math.max(0, (now - new Date(iso).getTime()) / 60_000);
  if (m < 1) return "just now";
  if (m < 60) return `${Math.floor(m)}m ago`;
  if (m < 48 * 60) return `${Math.floor(m / 60)}h ago`;
  return `${Math.floor(m / 1440)}d ago`;
}

function minutesSince(iso: string | null, now: number): number | null {
  return iso ? (now - new Date(iso).getTime()) / 60_000 : null;
}

function short(a: string) {
  return `${a.slice(0, 4)}…${a.slice(-4)}`;
}

type SortKey = "name" | "sol" | "open" | "unrealized" | "r24" | "r7" | "all" | "win" | "last";

const SORTS: Record<SortKey, (a: OverviewAccount) => number | string> = {
  name: (a) => a.name,
  sol: (a) => a.sol_balance_usd ?? -Infinity,
  open: (a) => a.open_value_usd ?? a.open_cost_usd,
  unrealized: (a) => a.unrealized_usd ?? -Infinity,
  r24: (a) => a.realized_24h,
  r7: (a) => a.realized_7d,
  all: (a) => a.realized_all,
  win: (a) => a.win_rate ?? -1,
  last: (a) => (a.last_trade_at ? new Date(a.last_trade_at).getTime() : 0),
};

function Status({ a, now }: { a: OverviewAccount; now: number }) {
  const beat = minutesSince(a.heartbeat_at, now);
  const chips: [string, string][] = [];
  if (!a.enabled) chips.push(["off", "bg-raised text-faint"]);
  else if (a.paused) chips.push(["paused", "bg-warn-soft text-warn"]);
  if (a.enabled && (beat == null || beat > STALE_MIN)) chips.push(["engine stale", "bg-bear-soft text-bear"]);
  if (a.billing_hold) chips.push(["unpaid: buys held", "bg-bear-soft text-bear"]);
  if (a.owner?.status === "blocked") chips.push(["blocked", "bg-bear-soft text-bear"]);
  return (
    <span className="flex gap-1">
      {chips.map(([t, c]) => (
        <span key={t} className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${c}`}>{t}</span>
      ))}
    </span>
  );
}

function AccountsTable({ accounts, now }: { accounts: OverviewAccount[]; now: number }) {
  const [sort, setSort] = useState<SortKey>("all");
  const [desc, setDesc] = useState(true);
  const rows = useMemo(() => {
    const f = SORTS[sort];
    return [...accounts].sort((a, b) => {
      const x = f(a), y = f(b);
      const c = x < y ? -1 : x > y ? 1 : 0;
      return desc ? -c : c;
    });
  }, [accounts, sort, desc]);
  const th = (k: SortKey, label: string, right = true) => (
    <th className={right ? "text-right" : "text-left"}>
      <button type="button" className="uppercase hover:text-ink"
        onClick={() => (k === sort ? setDesc(!desc) : (setSort(k), setDesc(k !== "name")))}>
        {label}{sort === k ? (desc ? " ↓" : " ↑") : ""}
      </button>
    </th>
  );
  return (
    <Panel icon={<Wallet />} title="Accounts" meta={`${accounts.length} · click a column to sort`} flush>
      <div className="overflow-x-auto">
        <table className="data-table min-w-[980px] text-sm">
          <thead>
            <tr>
              {th("name", "Account", false)}
              <th className="text-left">Status</th>
              {th("sol", "Wallet")}
              {th("open", "Open")}
              {th("unrealized", "Unrealized")}
              {th("r24", "24h")}
              {th("r7", "7d")}
              {th("all", "All time")}
              {th("win", "Win rate")}
              {th("last", "Last trade")}
            </tr>
          </thead>
          <tbody>
            {rows.map((a) => (
              <tr key={a.name}>
                <td>
                  <div className="num font-semibold">{a.name}</div>
                  <div className="text-xs text-faint">
                    {a.owner ? a.owner.email : "admin"}
                    {a.pubkey && (
                      <a className="ms-1.5 inline-flex items-center gap-0.5 hover:text-ink" target="_blank"
                        rel="noreferrer" href={`https://solscan.io/account/${a.pubkey}`}>
                        {short(a.pubkey)} <ExternalLink className="size-3" />
                      </a>
                    )}
                  </div>
                </td>
                <td><Status a={a} now={now} /></td>
                <td className="num text-right">
                  <div>{money(a.sol_balance_usd)}</div>
                  <div className="text-xs text-faint">{sol(a.sol_balance)}</div>
                </td>
                <td className="num text-right">
                  <div>{a.open_count ? money(a.open_value_usd ?? a.open_cost_usd) : "—"}</div>
                  <div className="text-xs text-faint">
                    {a.open_count} pos · {money(a.open_cost_usd)} in
                  </div>
                </td>
                <td className={`num text-right ${pctClass(a.unrealized_usd)}`}>
                  {a.open_count ? money(a.unrealized_usd, true) : "—"}
                </td>
                <td className={`num text-right ${pctClass(a.realized_24h)}`}>{money(a.realized_24h, true)}</td>
                <td className={`num text-right ${pctClass(a.realized_7d)}`}>{money(a.realized_7d, true)}</td>
                <td className={`num text-right font-medium ${pctClass(a.realized_all)}`}>
                  {money(a.realized_all, true)}
                </td>
                <td className="num text-right">
                  <div>{a.win_rate == null ? "—" : `${a.win_rate.toFixed(0)}%`}</div>
                  <div className="text-xs text-faint">
                    {a.wins}/{a.trades}
                    {a.failed_24h > 0 && <span className="ms-1 text-bear">· {a.failed_24h} failed</span>}
                  </div>
                </td>
                <td className="text-right text-xs text-muted">{since(a.last_trade_at, now)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

const STATUS_CLS: Record<Bill["status"], string> = {
  paid: "bg-bull-soft text-bull",
  due: "bg-warn-soft text-warn",
  check: "bg-bear-soft text-bear",
  sending: "bg-bear-soft text-bear",
  waived: "bg-raised text-muted",
};

const SCOUT_TONE: Record<ScoutCandidate["status"], string> = {
  passed: "text-mint", trial: "text-sky", new: "text-ink", failed: "text-bear", rejected: "text-faint", added: "text-muted",
};

/** Wallet scout: candidates found, filtered and tried on paper. Passed ones can be added (switched off). */
function ScoutPanel({ password }: { password: string }) {
  const [data, setData] = useState<ScoutList | null>(null);
  const [all, setAll] = useState(false);
  const [busy, setBusy] = useState<number | null>(null);
  const [msg, setMsg] = useState("");

  const load = useCallback(() => {
    api.scout(password).then(setData).catch((e) =>
      setMsg(e instanceof ApiError ? e.message : "Could not load the wallet scout."));
  }, [password]);
  useEffect(load, [load]);

  async function act(c: ScoutCandidate, what: "add" | "dismiss") {
    if (what === "add" && !confirm(`Add ${c.address} to account "main" as a smart wallet, switched OFF? `
      + "You turn it on in /accounts when you want to copy it.")) return;
    setBusy(c.id);
    setMsg("");
    try {
      if (what === "add") await api.scoutAdd(password, c.id);
      else await api.scoutDismiss(password, c.id);
      load();
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : "Could not save.");
    } finally {
      setBusy(null);
    }
  }

  const rows = (data?.candidates ?? []).filter((c) => all || ["passed", "trial", "new"].includes(c.status));
  return (
    <Panel title="Wallet scout">
      <p className="mb-3 text-xs text-muted">
        Once a day it finds wallets that bought early in recent winners, keeps only the ones that pass a strict
        filter (several days, profitable without their best trade, copyable pace), then copies them on paper for
        a few days, entering seconds late like the bot. Nothing is copied for real until you add a wallet and
        switch it on. {data && !data.enabled && <span className="text-warn">The scout is off on the server.</span>}
      </p>
      <label className="mb-2 flex items-center gap-2 text-xs text-muted">
        <input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} /> Show rejected and failed too
      </label>
      {!data ? (
        <p className="text-sm text-muted">{msg || "Loading…"}</p>
      ) : rows.length === 0 ? (
        <Empty>No candidates yet.</Empty>
      ) : (
        <div className="overflow-x-auto">
          <table className="data-table w-full text-sm">
            <thead>
              <tr>
                <th className="text-left">Wallet</th>
                <th className="text-left">Status</th>
                <th className="text-right">History</th>
                <th className="text-right">Paper trial</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((c) => {
                const m = c.metrics;
                const t = c.trial;
                const tnet = t ? t.net_usd + t.open_usd : 0;
                return (
                  <tr key={c.id}>
                    <td>
                      <a className="num hover:underline" target="_blank" rel="noreferrer"
                        href={`https://solscan.io/account/${c.address}`}>{c.address.slice(0, 6)}…{c.address.slice(-4)}</a>
                      <div className="text-[11px] text-faint">early in {c.hits} winners</div>
                    </td>
                    <td>
                      <span className={`font-medium ${SCOUT_TONE[c.status]}`}>{c.status}</span>
                      <div className="max-w-[28ch] text-[11px] text-muted">{c.reason}</div>
                    </td>
                    <td className="num text-right text-xs">
                      {m ? (
                        <>
                          <div>{m.closed} trades · {m.win_rate.toFixed(0)}% wins</div>
                          <div className={m.net_usd >= 0 ? "text-mint" : "text-bear"}>
                            net ${m.net_usd.toFixed(0)}
                            <span className="text-faint"> (${m.net_wo_best.toFixed(0)} w/o best)</span>
                          </div>
                          <div className="text-faint">
                            {m.days.toFixed(1)} d · {m.per_day.toFixed(0)}/day · hold {m.hold_min.toFixed(0)}m
                          </div>
                        </>
                      ) : "—"}
                    </td>
                    <td className="num text-right text-xs">
                      {t ? (
                        <>
                          <div>{t.trades} trades · {t.wins} wins</div>
                          <div className={tnet >= 0 ? "text-mint" : "text-bear"}>${tnet.toFixed(2)}</div>
                          {c.status === "trial" && c.trial_end && (
                            <div className="text-faint">ends {new Date(c.trial_end).toLocaleString()}</div>
                          )}
                        </>
                      ) : "—"}
                    </td>
                    <td className="whitespace-nowrap text-right">
                      {c.status === "passed" && (
                        <button type="button" className={button("primary")} disabled={busy === c.id}
                          onClick={() => act(c, "add")}>Add to main</button>
                      )}
                      {["passed", "new", "trial"].includes(c.status) && (
                        <button type="button" className="ml-2 text-xs text-muted hover:text-bear" disabled={busy === c.id}
                          onClick={() => act(c, "dismiss")}>Dismiss</button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {msg && data && <p className="mt-2 text-xs text-bear">{msg}</p>}
    </Panel>
  );
}

/** The operator's Telegram chat: one switch per kind of message. Account and security notes always go out. */
function TelegramPanel({ password }: { password: string }) {
  const [data, setData] = useState<TelegramPrefs | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState("");

  useEffect(() => {
    api.telegramPrefs(password).then(setData).catch((e) =>
      setMsg(e instanceof ApiError ? e.message : "Could not load Telegram settings."));
  }, [password]);

  async function flip(key: string, on: boolean) {
    setBusy(key);
    setMsg("");
    try {
      setData(await api.setTelegramPrefs(password, { [key]: on }));
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : "Could not save.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <Panel title="Telegram alerts">
      <p className="mb-3 text-xs text-muted">
        What your Telegram chat receives. Changes apply within about 10 seconds. Account and security notes
        (sign-ups, withdrawals, key reveals, watchdog) always go out.
      </p>
      {!data ? (
        <p className="text-sm text-muted">{msg || "Loading…"}</p>
      ) : (
        <>
          {!data.configured && (
            <p className="mb-3 text-xs text-warn">No Telegram bot or chat is set on the server, so nothing is sent.</p>
          )}
          <ul className="divide-y divide-line">
            {data.kinds.map((k) => (
              <li key={k.key} className="flex items-center justify-between gap-4 py-2.5">
                <div className="min-w-0">
                  <div className="text-sm text-ink">{k.title}</div>
                  <div className="text-xs text-muted">{k.help}</div>
                </div>
                <button type="button" role="switch" aria-checked={k.on} aria-label={k.title}
                  disabled={busy === k.key} onClick={() => flip(k.key, !k.on)}
                  className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition disabled:opacity-50 ${
                    k.on ? "bg-mint" : "bg-raised ring-1 ring-line-strong"}`}>
                  <span className={`inline-block size-4 rounded-full bg-ink shadow transition ${
                    k.on ? "translate-x-[18px]" : "translate-x-0.5"}`} />
                </button>
              </li>
            ))}
          </ul>
          {msg && <p className="mt-2 text-xs text-bear">{msg}</p>}
        </>
      )}
    </Panel>
  );
}

function BillingPanel({ password, now }: { password: string; now: number }) {
  const [data, setData] = useState<BillingAdmin | null>(null);
  const [draft, setDraft] = useState<Record<keyof BillingConfig, string> | null>(null);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await api.billing(password);
      setData(r);
      setDraft((d) => d ?? {
        enabled: r.config.enabled ? "1" : "", fee_usd: String(r.config.fee_usd),
        profit_pct: String(r.config.profit_pct), trial_days: String(r.config.trial_days),
        grace_days: String(r.config.grace_days), wallet: r.config.wallet,
      });
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : "Could not load billing.");
    }
  }, [password]);

  useEffect(() => {
    load();
    const t = setInterval(() => {
      if (document.visibilityState === "visible") load();
    }, 60_000);
    return () => clearInterval(t);
  }, [load]);

  async function save() {
    if (!draft || !data) return;
    const enabling = !!draft.enabled && !data.config.enabled;
    if (enabling && !confirm(
      `Turn billing on? From now on users' accounts are charged automatically in SOL from their trading wallets `
      + `to ${draft.wallet}: $${draft.fee_usd}/month + ${draft.profit_pct}% of profit above their high-water mark. `
      + `Billing periods start now (no past months are billed).`)) return;
    setBusy(true);
    setMsg("");
    try {
      const r = await api.billingConfig(password, {
        enabled: !!draft.enabled, fee_usd: Number(draft.fee_usd), profit_pct: Number(draft.profit_pct),
        trial_days: Number(draft.trial_days), grace_days: Number(draft.grace_days), wallet: draft.wallet.trim(),
      });
      setMsg(r.config.enabled ? "Saved. Billing is on." : "Saved. Billing is off.");
      await load();
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : "Save failed.");
    } finally {
      setBusy(false);
    }
  }

  async function act(b: Bill, action: "waive" | "paid" | "due") {
    let sig: string | undefined;
    if (action === "paid") {
      const v = prompt(`Bill #${b.id}: transaction signature from Solscan (optional)`, b.signature ?? "");
      if (v === null) return;
      sig = v.trim() || undefined;
    } else if (action === "waive" && !confirm(`Waive bill #${b.id} ($${b.total_usd.toFixed(2)})?`)) {
      return;
    } else if (action === "due" && !confirm(
      `Collect bill #${b.id} again? Only if Solscan shows the earlier transfer did NOT land.`)) {
      return;
    }
    try {
      await api.billAction(password, b.id, action, sig);
      await load();
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : "Failed.");
    }
  }

  const field = (k: keyof BillingConfig, label: string, cls = "") => draft && (
    <label className={`flex flex-col gap-1 text-xs text-muted ${cls}`}>
      {label}
      <input className={`${input} num`} value={draft[k]} onChange={(e) => setDraft({ ...draft, [k]: e.target.value })} />
    </label>
  );

  return (
    <section className="card overflow-hidden">
      <header className="flex flex-wrap items-center gap-2 border-b border-line px-5 py-4">
        <Receipt className="size-4 text-accent" />
        <h2 className="text-[15px] font-semibold tracking-tight">Billing</h2>
        {data && (
          <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase ${
            data.config.enabled ? "bg-bull-soft text-bull" : "bg-raised text-muted"}`}>
            {data.config.enabled ? "on" : "off"}
          </span>
        )}
        {data && !data.live && <span className="text-xs text-warn">Collects only when LIVE_MODE=live.</span>}
        {data && (
          <span className="ms-auto text-xs text-muted">
            Collected 30d <b className="num text-ink">{money(data.totals.paid_30d_usd)}</b>
            {" "}({data.totals.paid_30d_sol.toFixed(3)} SOL) · due <b className="num text-ink">{money(data.totals.due_usd)}</b>
            {data.totals.check > 0 && <span className="text-bear"> · {data.totals.check} to check</span>}
          </span>
        )}
      </header>
      {draft && (
        <div className="grid gap-3 border-b border-line p-4 sm:grid-cols-6">
          {field("fee_usd", "Monthly fee ($)")}
          {field("profit_pct", "Profit share (%)")}
          {field("trial_days", "Free trial (days)")}
          {field("grace_days", "Grace before pause (days)")}
          {field("wallet", "Wallet that receives payments", "sm:col-span-2")}
          <label className="flex items-center gap-2 text-sm sm:col-span-4">
            <input type="checkbox" checked={!!draft.enabled}
              onChange={(e) => setDraft({ ...draft, enabled: e.target.checked ? "1" : "" })} />
            Billing on (charges users&apos; wallets automatically each month)
          </label>
          <div className="flex items-center gap-2 sm:col-span-2 sm:justify-end">
            {msg && <span className="text-xs text-muted">{msg}</span>}
            <button type="button" disabled={busy} onClick={save}
              className="rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-carbon disabled:opacity-50">
              Save
            </button>
          </div>
          {data && data.held.length > 0 && (
            <p className="text-xs text-bear sm:col-span-6">New buys paused for unpaid bills: {data.held.join(", ")}</p>
          )}
        </div>
      )}
      {data && (data.bills.length === 0 ? (
        <p className="px-4 py-3 text-sm text-muted">No bills yet.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="data-table min-w-[860px] text-sm">
            <thead>
              <tr>
                <th className="text-left">Bill</th>
                <th className="text-left">Period</th>
                <th className="text-right">Fee</th>
                <th className="text-right">Profit → share</th>
                <th className="text-right">Total</th>
                <th className="text-left">Status</th>
                <th className="text-right font-medium" />
              </tr>
            </thead>
            <tbody>
              {data.bills.map((b) => (
                <tr key={b.id} className="border-b border-line align-top last:border-0">
                  <td>
                    <div className="font-medium">#{b.id} · {b.account}</div>
                    <div className="text-xs text-faint">{b.email ?? ""}</div>
                  </td>
                  <td className="text-xs text-muted">
                    {b.period_start.slice(0, 10)} → {b.period_end.slice(0, 10)}
                  </td>
                  <td className="num text-right">
                    {money(b.fee_usd)}
                    {b.discount_usd > 0 && (
                      <div className="text-xs text-bull">−{money(b.discount_usd)} {b.coupon_code}</div>
                    )}
                  </td>
                  <td className="num text-right">
                    {money(b.profit_usd)} → {money(b.share_usd)}
                    <div className="text-xs text-faint">{b.profit_pct}%</div>
                  </td>
                  <td className="num text-right font-medium">
                    {money(b.total_usd)}
                    {b.sol_amount != null && <div className="text-xs text-faint">{b.sol_amount.toFixed(4)} SOL</div>}
                  </td>
                  <td>
                    <span className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${STATUS_CLS[b.status]}`}>
                      {b.status === "sending" ? "check" : b.status}
                    </span>
                    {b.signature && (
                      <a className="ms-1.5 inline-flex items-center gap-0.5 text-xs text-faint hover:text-ink"
                        target="_blank" rel="noreferrer" href={`https://solscan.io/tx/${b.signature}`}>
                        tx <ExternalLink className="size-3" />
                      </a>
                    )}
                    <div className="text-xs text-faint">
                      {b.status === "paid" ? since(b.paid_at, now) : b.error ?? ""}
                    </div>
                  </td>
                  <td className="text-right whitespace-nowrap">
                    {(b.status === "check" || b.status === "sending") && (
                      <>
                        <button type="button" className={navBtn} onClick={() => act(b, "paid")}>Landed</button>{" "}
                        <button type="button" className={navBtn} onClick={() => act(b, "due")}>Did not land</button>{" "}
                      </>
                    )}
                    {b.status === "due" && (
                      <>
                        <button type="button" className={navBtn} onClick={() => act(b, "paid")}>Mark paid</button>{" "}
                      </>
                    )}
                    {b.status !== "paid" && b.status !== "waived" && (
                      <button type="button" className={navBtn} onClick={() => act(b, "waive")}>Waive</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </section>
  );
}

function CouponsPanel({ password, now }: { password: string; now: number }) {
  const [list, setList] = useState<Coupon[] | null>(null);
  const [f, setF] = useState({ code: "", percent: "20", months: "", max_uses: "", expires: "", note: "" });
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setList((await api.coupons(password)).coupons);
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : "Could not load coupons.");
    }
  }, [password]);

  useEffect(() => {
    load();
  }, [load]);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMsg("");
    try {
      const r = await api.createCoupon(password, {
        code: f.code.trim(), percent: Number(f.percent),
        months: f.months ? Number(f.months) : null, max_uses: f.max_uses ? Number(f.max_uses) : null,
        expires_at: f.expires ? new Date(`${f.expires}T23:59:59Z`).toISOString() : null, note: f.note,
      });
      setMsg(`Created ${r.coupon.code}.`);
      setF({ code: "", percent: f.percent, months: "", max_uses: "", expires: "", note: "" });
      await load();
    } catch (err) {
      setMsg(err instanceof ApiError ? err.message : "Failed.");
    } finally {
      setBusy(false);
    }
  }

  const box = (k: keyof typeof f, label: string, ph = "", cls = "", type = "text") => (
    <label className={`flex flex-col gap-1 text-xs text-muted ${cls}`}>
      {label}
      <input className={`${input} ${k === "code" ? "uppercase" : "num"}`} type={type} placeholder={ph}
        value={f[k]} onChange={(e) => setF({ ...f, [k]: e.target.value })} />
    </label>
  );

  return (
    <section className="card overflow-hidden">
      <header className="flex flex-wrap items-center gap-2 border-b border-line px-5 py-4">
        <Ticket className="size-4 text-accent" />
        <h2 className="text-[15px] font-semibold tracking-tight">Discount codes</h2>
        <span className="text-xs text-faint">% off the monthly fee only, never the profit share</span>
      </header>
      <form className="grid gap-3 border-b border-line p-4 sm:grid-cols-7" onSubmit={create}>
        {box("code", "Code", "RAMADAN25")}
        {box("percent", "Discount %")}
        {box("months", "Months", "empty = always")}
        {box("max_uses", "Max uses", "empty = no limit")}
        {box("expires", "Apply by", "", "", "date")}
        {box("note", "Note", "who it is for")}
        <div className="flex items-end">
          <button type="submit" disabled={busy || !f.code.trim()}
            className="w-full rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-carbon disabled:opacity-50">
            Create
          </button>
        </div>
        {msg && <p className="text-xs text-muted sm:col-span-7">{msg}</p>}
      </form>
      {list && (list.length === 0 ? (
        <p className="px-4 py-3 text-sm text-muted">No codes yet.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="data-table min-w-[760px] text-sm">
            <thead>
              <tr>
                <th className="text-left">Code</th>
                <th className="text-right">Discount</th>
                <th className="text-right">Months</th>
                <th className="text-right">Used</th>
                <th className="text-left">Apply by</th>
                <th className="text-left">Users</th>
                <th className="text-right font-medium" />
              </tr>
            </thead>
            <tbody>
              {list.map((c) => {
                const expired = c.expires_at != null && new Date(c.expires_at).getTime() < now;
                const full = c.max_uses != null && c.uses >= c.max_uses;
                return (
                  <tr key={c.id} className={`border-b border-line align-top last:border-0 ${c.active ? "" : "opacity-50"}`}>
                    <td>
                      <div className="num font-medium">{c.code}</div>
                      {c.note && <div className="text-xs text-faint">{c.note}</div>}
                    </td>
                    <td className="num text-right">{c.percent}%</td>
                    <td className="num text-right">{c.months ?? "always"}</td>
                    <td className="num text-right">
                      {c.uses}{c.max_uses != null ? ` / ${c.max_uses}` : ""}
                      {full && <div className="text-xs text-warn">used up</div>}
                    </td>
                    <td className="text-xs text-muted">
                      {c.expires_at ? c.expires_at.slice(0, 10) : "—"}
                      {expired && <span className="ms-1 text-warn">expired</span>}
                    </td>
                    <td className="text-xs text-muted">{c.users.join(", ") || "—"}</td>
                    <td className="text-right">
                      <button type="button" className={navBtn}
                        onClick={async () => {
                          await api.editCoupon(password, c.id, !c.active).catch(() => undefined);
                          await load();
                        }}>
                        {c.active ? "Turn off" : "Turn on"}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ))}
    </section>
  );
}

/* ---------- command-center pieces ---------------------------------------------------------- */

/** Counts up/down to a new value instead of jumping (respects reduced motion). */
function useTween(target: number, ms = 700): number {
  const [v, setV] = useState(target);
  const from = useRef(target);
  useEffect(() => {
    const reduce = typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    if (reduce || !Number.isFinite(target)) {
      setV(target);
      from.current = target;
      return;
    }
    const start = performance.now();
    const a = from.current;
    let raf = 0;
    const step = (t: number) => {
      const k = Math.min(1, (t - start) / ms);
      const e = 1 - Math.pow(1 - k, 3);
      setV(a + (target - a) * e);
      if (k < 1) raf = requestAnimationFrame(step);
      else from.current = target;
    };
    raf = requestAnimationFrame(step);
    return () => {
      cancelAnimationFrame(raf);
      from.current = target;
    };
  }, [target, ms]);
  return v;
}

function Money({ v, signed = false, className = "" }: { v: number | null | undefined; signed?: boolean; className?: string }) {
  const t = useTween(v ?? 0);
  return <span className={`num ${className}`}>{v == null ? "—" : money(t, signed)}</span>;
}

type Range = 7 | 14 | 30;
type ChartMode = "cum" | "daily";

/** Realized P&L across all accounts: cumulative area or daily bars, with a hover read-out. */
function PnlChart({ days }: { days: AdminOverview["daily"] }) {
  const [range, setRange] = useState<Range>(30);
  const [mode, setMode] = useState<ChartMode>("cum");
  const [hover, setHover] = useState<number | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(640);
  const H = 220, PAD = 8;

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(260, e.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const rows = days.slice(-range);
  let run = 0;
  const pts = rows.map((d) => ({ ...d, cum: (run += d.pnl) }));
  const vals = mode === "cum" ? pts.map((p) => p.cum) : pts.map((p) => p.pnl);
  const hi = Math.max(0, ...vals), lo = Math.min(0, ...vals);
  const span = hi - lo || 1;
  const y = (v: number) => PAD + (1 - (v - lo) / span) * (H - PAD * 2);
  const step = rows.length > 1 ? w / (rows.length - 1) : w;
  const x = (i: number) => (rows.length > 1 ? i * step : w / 2);
  const zero = y(0);
  const total = pts.length ? pts[pts.length - 1].cum : 0;
  const up = total >= 0;
  const line = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.cum).toFixed(1)}`).join("");
  const area = pts.length ? `${line}L${x(pts.length - 1)},${zero}L${x(0)},${zero}Z` : "";
  const bw = Math.max(3, Math.min(22, (w / Math.max(rows.length, 1)) * 0.6));
  const bx = (i: number) => (w / rows.length) * (i + 0.5);
  const h = hover != null ? pts[hover] : null;
  const hx = hover == null ? 0 : mode === "cum" ? x(hover) : bx(hover);

  function move(e: React.PointerEvent) {
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const px = e.clientX - r.left;
    const i = mode === "cum" ? Math.round(px / step) : Math.floor(px / (w / rows.length));
    setHover(Math.max(0, Math.min(rows.length - 1, i)));
  }

  const seg = (on: boolean) =>
    `rounded-md px-2.5 py-1 text-xs font-medium transition ${on ? "bg-raised text-ink shadow-sm" : "text-muted hover:text-ink"}`;

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="flex rounded-lg border border-line bg-carbon/60 p-0.5" role="tablist" aria-label="Chart type">
          <button type="button" className={seg(mode === "cum")} onClick={() => setMode("cum")}>Cumulative</button>
          <button type="button" className={seg(mode === "daily")} onClick={() => setMode("daily")}>Daily</button>
        </div>
        <div className="ms-auto flex rounded-lg border border-line bg-carbon/60 p-0.5" aria-label="Range">
          {([7, 14, 30] as Range[]).map((r) => (
            <button key={r} type="button" className={seg(range === r)} onClick={() => setRange(r)}>{r}d</button>
          ))}
        </div>
      </div>
      <div ref={box} className="relative touch-none select-none" onPointerMove={move} onPointerLeave={() => setHover(null)}
        onPointerDown={move}>
        {rows.length === 0 ? (
          <div className="grid h-[220px] place-items-center text-sm text-muted">No closed trades in this range.</div>
        ) : (
          <svg width={w} height={H} className="block overflow-visible" role="img"
            aria-label={`Realized P&L, last ${range} days: ${money(total, true)}`}>
            <defs>
              <linearGradient id="pnl-fill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={up ? "#2be4a7" : "#ff5c6c"} stopOpacity="0.32" />
                <stop offset="100%" stopColor={up ? "#2be4a7" : "#ff5c6c"} stopOpacity="0" />
              </linearGradient>
            </defs>
            {[0.25, 0.5, 0.75].map((g) => (
              <line key={g} x1="0" x2={w} y1={PAD + g * (H - PAD * 2)} y2={PAD + g * (H - PAD * 2)}
                stroke="rgb(255 255 255 / 0.04)" />
            ))}
            <line x1="0" x2={w} y1={zero} y2={zero} stroke="rgb(255 255 255 / 0.14)" strokeDasharray="3 4" />
            {mode === "cum" ? (
              <g key={`c${range}`} className="animate-fade-in">
                <path d={area} fill="url(#pnl-fill)" />
                <path d={line} fill="none" stroke={up ? "#2be4a7" : "#ff5c6c"} strokeWidth="2.2"
                  strokeLinejoin="round" strokeLinecap="round" />
              </g>
            ) : (
              <g key={`d${range}`} className="animate-fade-in">
                {pts.map((p, i) => {
                  const top = Math.min(y(p.pnl), zero);
                  return (
                    <rect key={p.day} x={bx(i) - bw / 2} y={top} width={bw} rx={Math.min(3, bw / 3)}
                      height={Math.max(Math.abs(y(p.pnl) - zero), p.trades ? 1.5 : 0)}
                      fill={p.pnl >= 0 ? "#2be4a7" : "#ff5c6c"} opacity={hover == null || hover === i ? 0.9 : 0.35} />
                  );
                })}
              </g>
            )}
            {h && (
              <g pointerEvents="none">
                <line x1={hx} x2={hx} y1={0} y2={H} stroke="rgb(255 255 255 / 0.18)" />
                {mode === "cum" && (
                  <circle cx={hx} cy={y(h.cum)} r="5" fill="#070a12" stroke={up ? "#2be4a7" : "#ff5c6c"} strokeWidth="2.5" />
                )}
              </g>
            )}
          </svg>
        )}
        {h && (
          <div className="pointer-events-none absolute top-1 z-10 min-w-[150px] -translate-x-1/2 rounded-xl border border-line-strong bg-carbon/95 px-3 py-2 text-xs shadow-2xl backdrop-blur"
            style={{ left: Math.max(80, Math.min(w - 80, hx)) }}>
            <div className="num text-faint">{h.day}</div>
            <div className={`num mt-0.5 text-sm font-semibold ${pctClass(h.pnl)}`}>{money(h.pnl, true)}</div>
            <div className="text-muted">{h.trades} trade{h.trades === 1 ? "" : "s"}</div>
            <div className="mt-1 border-t border-line pt-1 text-muted">
              running <span className={`num ${pctClass(h.cum)}`}>{money(h.cum, true)}</span>
            </div>
          </div>
        )}
      </div>
      <div className="mt-2 flex justify-between text-[11px] text-faint">
        <span className="num">{rows[0]?.day}</span>
        <span className="num">{rows[rows.length - 1]?.day}</span>
      </div>
    </div>
  );
}

/** Ring gauge, 0..100. */
function Ring({ value, size = 64, stroke = 7, tone = "#2be4a7", children }: {
  value: number | null;
  size?: number;
  stroke?: number;
  tone?: string;
  children?: React.ReactNode;
}) {
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const v = useTween(value ?? 0, 900);
  return (
    <div className="relative grid shrink-0 place-items-center" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="-rotate-90">
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="rgb(255 255 255 / 0.07)" strokeWidth={stroke} />
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke={tone} strokeWidth={stroke} strokeLinecap="round"
          strokeDasharray={c} strokeDashoffset={c * (1 - Math.max(0, Math.min(100, v)) / 100)} />
      </svg>
      <div className="absolute inset-0 grid place-items-center">{children}</div>
    </div>
  );
}

/** Thin segmented bar: [value, class][] */
function Stack({ parts }: { parts: [number, string, string][] }) {
  const sum = parts.reduce((s, p) => s + Math.max(p[0], 0), 0) || 1;
  return (
    <div className="flex h-1.5 w-full overflow-hidden rounded-full bg-raised">
      {parts.map(([v, c, label]) => v > 0 && (
        <span key={label} title={`${label}: ${v}`} className={`h-full ${c} transition-all duration-700`}
          style={{ width: `${(v / sum) * 100}%` }} />
      ))}
    </div>
  );
}

function Kpi({ icon, label, children, sub, glow }: {
  icon: React.ReactNode;
  label: string;
  children: React.ReactNode;
  sub?: React.ReactNode;
  glow?: string;
}) {
  return (
    <div className="card group relative overflow-hidden p-5 transition duration-300 hover:-translate-y-0.5 hover:border-line-strong">
      <div className={`pointer-events-none absolute -top-16 -right-16 size-40 rounded-full opacity-0 blur-3xl transition duration-500 group-hover:opacity-100 ${glow ?? "bg-mint/10"}`} />
      <div className="flex items-center gap-2 text-[11px] font-semibold tracking-[0.1em] text-muted uppercase">
        <span className="grid size-6 place-items-center rounded-md bg-raised text-ink [&>svg]:size-3.5">{icon}</span>
        {label}
      </div>
      <div className="mt-3">{children}</div>
      {sub && <div className="mt-2 text-xs leading-relaxed text-muted">{sub}</div>}
    </div>
  );
}

function Pulse({ state }: { state: "ok" | "stale" | "off" | "paused" }) {
  const c = { ok: "bg-mint", stale: "bg-bear", off: "bg-faint", paused: "bg-gold" }[state];
  return (
    <span className="relative inline-flex size-2.5 shrink-0">
      {(state === "ok" || state === "stale") && (
        <span className={`absolute inset-0 rounded-full ${c} animate-pulse-ring`} />
      )}
      <span className={`relative inline-flex size-2.5 rounded-full ${c}`} />
    </span>
  );
}

function engineState(a: OverviewAccount, now: number): "ok" | "stale" | "off" | "paused" {
  if (!a.enabled) return "off";
  const m = minutesSince(a.heartbeat_at, now);
  if (m == null || m > STALE_MIN) return "stale";
  return a.paused ? "paused" : "ok";
}

/** One glance per engine: heartbeat, last trade, today. */
type AcctAct = (a: OverviewAccount, change: { paused?: boolean; enabled?: boolean }) => void;

function HealthStrip({ accounts, now, scanner, onAct, busy }: {
  accounts: OverviewAccount[];
  now: number;
  scanner?: string;
  onAct?: AcctAct;
  busy?: string | null;
}) {
  const label = { ok: "running", stale: "not stepping", off: "off", paused: "paused" };
  return (
    <div className="card flex flex-col p-0">
      <div className="flex items-center gap-2 border-b border-line px-5 py-3.5">
        <Cpu className="size-4 text-mint" />
        <h3 className="text-sm font-semibold">Engines</h3>
        <span className="text-xs text-muted">heartbeat · switch = new buys</span>
      </div>
      <ul className="divide-y divide-line">
        {accounts.map((a) => {
          const st = engineState(a, now);
          return (
            <li key={a.name} className="flex items-center gap-3 px-5 py-2.5 text-sm">
              <Pulse state={st} />
              <span className="num min-w-0 flex-1 truncate font-medium">{a.name}</span>
              <span className={`text-xs ${st === "stale" ? "text-bear" : st === "ok" ? "text-mint" : "text-muted"}`}>
                {label[st]}
              </span>
              <span className="w-16 text-right text-[11px] text-faint">{since(a.heartbeat_at, now)}</span>
              {onAct && (
                <Toggle on={a.enabled && !a.paused} busy={busy === a.name || !a.enabled}
                  label={`New buys on ${a.name}`} onClick={() => onAct(a, { paused: !a.paused })} />
              )}
            </li>
          );
        })}
      </ul>
      {scanner && (
        <div className="flex items-center gap-2 border-t border-line px-5 py-3 text-xs text-muted">
          <Radar className="size-3.5 text-sky" /> Scanner: {scanner}
        </div>
      )}
    </div>
  );
}

/** Account as a card: result, today, money at work, hit rate. */
function AccountCard({ a, now, best, onAct, busy }: {
  a: OverviewAccount;
  now: number;
  best: number;
  onAct?: AcctAct;
  busy?: boolean;
}) {
  const st = engineState(a, now);
  const work = a.open_value_usd ?? a.open_cost_usd;
  const cap = a.max_open_usd || Math.max(work, 1);
  const share = best > 0 ? Math.max(0, a.realized_all) / best : 0;
  return (
    <div className="card group relative flex flex-col gap-4 overflow-hidden p-5 transition duration-300 hover:-translate-y-0.5 hover:border-line-strong">
      <div className={`absolute inset-x-0 top-0 h-[2px] ${a.realized_all >= 0 ? "bg-gradient-to-r from-mint/0 via-mint/70 to-mint/0" : "bg-gradient-to-r from-bear/0 via-bear/70 to-bear/0"}`} />
      <div className="flex items-start gap-3">
        <span className={`grid size-10 shrink-0 place-items-center rounded-xl text-sm font-bold uppercase ${a.realized_all >= 0 ? "bg-mint-soft text-mint" : "bg-bear-soft text-bear"}`}>
          {a.name.slice(0, 2)}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="num truncate font-semibold">{a.name}</span>
            <Pulse state={st} />
          </div>
          <div className="truncate text-xs text-faint">
            {a.owner ? a.owner.email : "admin"}
            {a.pubkey && (
              <a className="ms-1.5 inline-flex items-center gap-0.5 hover:text-ink" target="_blank" rel="noreferrer"
                href={`https://solscan.io/account/${a.pubkey}`} title="Wallet on Solscan">
                {short(a.pubkey)} <ExternalLink className="size-3" />
              </a>
            )}
          </div>
        </div>
        <Status a={a} now={now} />
      </div>

      <div className="flex items-end justify-between gap-3">
        <div>
          <div className="text-[11px] tracking-wider text-muted uppercase">All time</div>
          <Money v={a.realized_all} signed className={`text-2xl font-medium ${pctClass(a.realized_all)}`} />
        </div>
        <div className="text-right text-xs">
          <div className="text-muted">24h <span className={`num ${pctClass(a.realized_24h)}`}>{money(a.realized_24h, true)}</span></div>
          <div className="text-muted">7d <span className={`num ${pctClass(a.realized_7d)}`}>{money(a.realized_7d, true)}</span></div>
        </div>
      </div>
      <div className="h-1 overflow-hidden rounded-full bg-raised" title="Share of the best account's result">
        <div className="h-full rounded-full bg-mint/70 transition-all duration-700" style={{ width: `${share * 100}%` }} />
      </div>

      <div className="grid grid-cols-3 gap-2 text-xs">
        <div className="rounded-lg bg-carbon/50 p-2.5">
          <div className="text-faint">Wallet</div>
          <div className="num mt-0.5 text-ink">{money(a.sol_balance_usd)}</div>
        </div>
        <div className="rounded-lg bg-carbon/50 p-2.5">
          <div className="text-faint">At work · {a.open_count}</div>
          <div className="num mt-0.5 text-ink">{a.open_count ? money(work) : "—"}</div>
          <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-raised">
            <div className="h-full rounded-full bg-sky" style={{ width: `${Math.min(100, (work / cap) * 100)}%` }} />
          </div>
        </div>
        <div className="rounded-lg bg-carbon/50 p-2.5">
          <div className="text-faint">Win rate</div>
          <div className="num mt-0.5 text-ink">{a.win_rate == null ? "—" : `${a.win_rate.toFixed(0)}%`}</div>
          <div className="text-[10px] text-faint">{a.wins}/{a.trades}{a.failed_24h > 0 && <span className="text-bear"> · {a.failed_24h} failed</span>}</div>
        </div>
      </div>
      <div className="-mt-1 flex items-center justify-between text-[11px] text-faint">
        <span>last trade {since(a.last_trade_at, now)}</span>
        {a.open_count > 0 && a.unrealized_usd != null && (
          <span>open <span className={`num ${pctClass(a.unrealized_usd)}`}>{money(a.unrealized_usd, true)}</span></span>
        )}
      </div>
      {onAct && (
        <div className="flex flex-wrap items-center gap-x-5 gap-y-2 border-t border-line pt-3 text-xs">
          <label className="flex items-center gap-2" title="Off: no new buys; open positions still sell">
            <Toggle on={a.enabled && !a.paused} busy={busy || !a.enabled} label={`New buys on ${a.name}`}
              onClick={() => onAct(a, { paused: !a.paused })} />
            <span className={a.enabled && !a.paused ? "text-mint" : "text-muted"}>New buys</span>
          </label>
          <label className="flex items-center gap-2" title="Off: the account stops completely">
            <Toggle on={a.enabled} busy={busy} label={`Account ${a.name}`}
              onClick={() => {
                if (a.enabled && !confirm(`Stop account ${a.name} completely?`)) return;
                onAct(a, { enabled: !a.enabled });
              }} />
            <span className={a.enabled ? "text-mint" : "text-muted"}>Account</span>
          </label>
          <a href="/accounts" className="ms-auto text-muted hover:text-ink">Settings →</a>
        </div>
      )}
    </div>
  );
}

type TradeFilter = "all" | "wins" | "losses" | "failed";

/** Closed and failed trades as a feed with filters. */
function TradesFeed({ items, now, limit }: { items: AdminOverview["recent"]; now: number; limit?: number }) {
  const [f, setF] = useState<TradeFilter>("all");
  const [acct, setAcct] = useState("");
  const [q, setQ] = useState("");
  const accounts = useMemo(() => [...new Set(items.map((p) => p.account))].sort(), [items]);
  const rows = items.filter((p) => {
    if (acct && p.account !== acct) return false;
    if (q && !(p.symbol ?? p.token_address).toLowerCase().includes(q.toLowerCase())) return false;
    if (f === "failed") return p.status === "failed";
    if (f === "wins") return p.status !== "failed" && (p.pnl_usd ?? 0) > 0;
    if (f === "losses") return p.status !== "failed" && (p.pnl_usd ?? 0) <= 0;
    return true;
  });
  const shown = limit ? rows.slice(0, limit) : rows;
  const closed = rows.filter((p) => p.status !== "failed");
  const net = closed.reduce((s, p) => s + (p.pnl_usd ?? 0), 0);
  const counts: Record<TradeFilter, number> = {
    all: items.length,
    wins: items.filter((p) => p.status !== "failed" && (p.pnl_usd ?? 0) > 0).length,
    losses: items.filter((p) => p.status !== "failed" && (p.pnl_usd ?? 0) <= 0).length,
    failed: items.filter((p) => p.status === "failed").length,
  };

  return (
    <Panel icon={<Activity />} title="Trades" meta={limit ? "latest" : `${rows.length} shown · net ${money(net, true)}`}
      flush actions={!limit && (
        <>
          <div className="relative">
            <Search className="pointer-events-none absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-faint" />
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Token"
              className="w-28 rounded-lg border border-line bg-carbon py-1 ps-7 pe-2 text-xs outline-none focus:border-mint/60" />
          </div>
          <select value={acct} onChange={(e) => setAcct(e.target.value)} aria-label="Account"
            className="rounded-lg border border-line bg-carbon px-2 py-1 text-xs outline-none focus:border-mint/60">
            <option value="">All accounts</option>
            {accounts.map((a) => <option key={a} value={a}>{a}</option>)}
          </select>
        </>
      )}>
      {!limit && (
        <div className="flex gap-1 overflow-x-auto border-b border-line px-4 py-2">
          {(["all", "wins", "losses", "failed"] as TradeFilter[]).map((k) => (
            <button key={k} type="button" onClick={() => setF(k)}
              className={`rounded-full px-3 py-1 text-xs font-medium whitespace-nowrap capitalize transition ${
                f === k ? "bg-ink text-carbon" : "text-muted hover:bg-raised hover:text-ink"}`}>
              {k} <span className="num opacity-60">{counts[k]}</span>
            </button>
          ))}
        </div>
      )}
      {shown.length === 0 ? <Empty>No trades match.</Empty> : (
        <ul className="divide-y divide-line">
          {shown.map((p, i) => {
            const failed = p.status === "failed";
            const win = !failed && (p.pnl_usd ?? 0) > 0;
            return (
              <li key={p.id} className="group flex animate-rise items-center gap-3 px-4 py-3 transition hover:bg-white/[0.025] sm:px-5"
                style={{ animationDelay: `${Math.min(i, 12) * 25}ms` }}>
                <span className={`grid size-9 shrink-0 place-items-center rounded-full text-xs font-bold ${
                  failed ? "bg-raised text-faint" : win ? "bg-mint-soft text-mint" : "bg-bear-soft text-bear"}`}>
                  {failed ? <XCircle className="size-4" /> : win ? <TrendingUp className="size-4" /> : <TrendingDown className="size-4" />}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <a className="truncate font-medium hover:text-mint" target="_blank" rel="noreferrer"
                      href={`https://dexscreener.com/solana/${p.token_address}`}>${p.symbol || short(p.token_address)}</a>
                    <Badge tone="muted" className="num">{p.account}</Badge>
                  </div>
                  <div className="truncate text-xs text-faint">
                    {failed
                      ? <span className="text-bear/80" title={p.error ?? ""}>failed{p.error ? `: ${p.error}` : ""}</span>
                      : <>{(p.exit_reason ?? "closed").replace(/_/g, " ")} · in {money(p.cost_usd)}</>}
                  </div>
                </div>
                <div className="text-right">
                  {failed ? <span className="text-faint">—</span> : (
                    <>
                      <div className={`num text-sm font-medium ${pctClass(p.pnl_usd)}`}>{money(p.pnl_usd, true)}</div>
                      <div className={`num text-[11px] ${pctClass(p.pnl_pct)}`}>{pct(p.pnl_pct)}</div>
                    </>
                  )}
                </div>
                <div className="hidden w-16 text-right text-[11px] text-faint sm:block">{since(p.at, now)}</div>
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}

type Tab = "overview" | "accounts" | "trades" | "scout" | "billing" | "alerts";
const TABS: { key: Tab; label: string; icon: React.ReactNode }[] = [
  { key: "overview", label: "Overview", icon: <LayoutDashboard /> },
  { key: "accounts", label: "Accounts", icon: <Wallet /> },
  { key: "trades", label: "Trades", icon: <Activity /> },
  { key: "scout", label: "Wallet scout", icon: <Radar /> },
  { key: "billing", label: "Billing", icon: <Receipt /> },
  { key: "alerts", label: "Telegram", icon: <Bell /> },
];

type Period = "24h" | "7d" | "30d" | "all";

export function AdminView() {
  const [password, setPassword] = useState("");
  const [data, setData] = useState<AdminOverview | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [tab, setTabState] = useState<Tab>("overview");
  const [period, setPeriod] = useState<Period>("24h");
  const [acctView, setAcctView] = useState<"cards" | "table">("cards");
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);
  useEffect(() => {
    if (!note?.ok) return;                       // errors stay until closed
    const t = setTimeout(() => setNote(null), 5_000);
    return () => clearTimeout(t);
  }, [note]);

  useAdminResume(setPassword);

  useEffect(() => {
    const read = () => {
      const h = window.location.hash.slice(1) as Tab;
      if (TABS.some((t) => t.key === h)) setTabState(h);
    };
    read();
    window.addEventListener("hashchange", read);
    return () => window.removeEventListener("hashchange", read);
  }, []);
  const setTab = (t: Tab) => {
    setTabState(t);
    history.replaceState(null, "", t === "overview" ? window.location.pathname : `#${t}`);
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const load = useCallback(async (pw: string) => {
    setLoading(true);
    try {
      const r = await api.overview(pw);
      setData(r);
      setError("");
      save(pw);
    } catch (e) {
      if (e instanceof ApiError && (e.status === 401 || e.status === 404)) {
        save(null);
        setPassword("");
      }
      setError(e instanceof ApiError ? (e.status === 401 ? "Wrong password." : e.message) : "Could not load.");
    } finally {
      setLoading(false);
      setNow(Date.now());
    }
  }, []);

  useEffect(() => {
    if (!password) return;
    load(password);
    const t = setInterval(() => {
      if (document.visibilityState === "visible") load(password);
    }, REFRESH_MS);
    return () => clearInterval(t);
  }, [password, load]);

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(t);
  }, []);

  /** Run a change, show what happened, reload the overview. */
  async function act(key: string, run: () => Promise<unknown>, okText: string) {
    setBusy(key);
    setNote(null);
    try {
      await run();
      setNote({ ok: true, text: okText });
      await load(password);
    } catch (e) {
      setNote({ ok: false, text: e instanceof ApiError ? e.message : "Could not change it." });
    } finally {
      setBusy(null);
    }
  }
  const onAct: AcctAct = (a, change) => act(a.name, () => api.editAccount(password, a.name, change),
    change.enabled === false ? `${a.name} is off` : change.enabled ? `${a.name} is on`
      : change.paused ? `${a.name}: new buys off · open positions still sell` : `${a.name}: new buys on`);

  if (!password) {
    return (
      <>
        <AppNav />
        <AdminSignIn onDone={() => setPassword(SESSION)} />
      </>
    );
  }

  const t = data?.totals;
  const u = data?.users;
  const stale = data?.accounts.filter((a) => engineState(a, now) === "stale") ?? [];
  const realized = t ? { "24h": t.realized_24h, "7d": t.realized_7d, "30d": t.realized_30d, all: t.realized_all }[period] : 0;
  const atWork = t ? t.open_value_usd ?? t.open_cost_usd : 0;
  const equity = t ? (t.sol_balance_usd ?? 0) + atWork : 0;
  const best = Math.max(0, ...(data?.accounts.map((a) => a.realized_all) ?? [0]));
  const sorted = [...(data?.accounts ?? [])].sort((a, b) => Number(b.enabled) - Number(a.enabled) || b.realized_all - a.realized_all);
  const badgeCount: Partial<Record<Tab, number>> = {
    accounts: u?.pending ?? 0,
    trades: t?.failed_24h ?? 0,
  };

  return (
    <>
    <AppNav />
    <div>
      <main className={pageMain}>
        <PageHeader icon={<LayoutDashboard />} title="Command center"
          subtitle="Every account, engine, user and trade in one place."
          badges={data && (
            <>
              <Badge tone={data.mode === "live" ? "mint" : "gold"} dot>{data.mode.toUpperCase()}</Badge>
            </>
          )}
          actions={
            <>
            {data && data.mode !== "off" && (
              <label className="card inline-flex items-center gap-2.5 px-3.5 py-2 text-sm" title="Off: no new buys on any account; open positions still sell">
                <Toggle on={!data.paused} busy={busy === "all"} label="New buys, all accounts"
                  onClick={() => act("all", () => api.livePause(password, !data.paused),
                    data.paused ? "Buying again on every account" : "All new buys paused · open positions still sell")} />
                <span className={data.paused ? "text-gold" : "text-ink"}>{data.paused ? "All buys paused" : "Buying"}</span>
              </label>
            )}
            <button type="button" onClick={() => load(password)} className={button("secondary", "relative overflow-hidden")} disabled={loading}>
              <RefreshCw className={`size-3.5 ${loading ? "animate-spin" : ""}`} />
              {data ? since(data.updated_at, now) : "Refresh"}
              {data && !loading && (
                <span key={data.updated_at} className="absolute inset-x-0 bottom-0 h-[2px] origin-left bg-mint/60"
                  style={{ animation: `admin-progress ${REFRESH_MS}ms linear both` }} />
              )}
            </button>
            </>
          } />

        <SectionTabs label="Admin sections" value={tab} onChange={setTab}
          tabs={TABS.map((x) => ({ ...x, count: badgeCount[x.key] }))} />

        {error && <p className="mb-4 rounded-xl border border-bear/30 bg-bear-soft px-4 py-3 text-sm text-bear">{error}</p>}
        {note && (
          <div className={`fixed right-4 bottom-20 z-50 flex animate-toast-in items-center gap-2 rounded-xl border px-4 py-2.5 text-sm shadow-2xl sm:bottom-6 ${
            note.ok ? "border-mint/40 bg-carbon text-mint" : "border-bear/40 bg-carbon text-bear"}`}>
            {note.text}
            <button type="button" className="ms-2 text-muted hover:text-ink" onClick={() => setNote(null)} aria-label="Close">×</button>
          </div>
        )}
        {!data && !error && (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {[0, 1, 2, 3].map((i) => <div key={i} className="card h-32 animate-pulse" />)}
          </div>
        )}

        {data && t && u && (
          <div key={tab} className="flex animate-rise flex-col gap-6">
            {/* alerts are shown on every section */}
            {(stale.length > 0 || t.failed_24h > 0 || u.pending > 0) && (
              <div className="flex flex-wrap gap-2">
                {stale.length > 0 && (
                  <div className="flex flex-wrap items-center gap-2 rounded-full border border-bear/30 bg-bear-soft px-3.5 py-1.5 text-xs">
                    <AlertTriangle className="size-4 text-bear" />
                    <span>Engine not stepping for <b>{stale.map((a) => a.name).join(", ")}</b>.</span>
                  </div>
                )}
                {t.failed_24h > 0 && (
                  <button type="button" onClick={() => setTab("trades")}
                    className="flex flex-wrap items-center gap-2 rounded-full border border-gold/30 bg-gold-soft px-3.5 py-1.5 text-left text-xs hover:border-gold/50">
                    <AlertTriangle className="size-4 text-gold" />
                    <span>{t.failed_24h} failed trade{t.failed_24h > 1 ? "s" : ""} in the last 24h.</span>
                    <span className="text-gold">See trades →</span>
                  </button>
                )}
                {u.pending_list.map((p) => (
                  <div key={p.id} className="flex flex-wrap items-center gap-2 rounded-full border border-mint/30 bg-mint-soft/60 py-1 ps-3.5 pe-1 text-xs">
                    <UserPlus className="size-4 text-accent" />
                    <span>Sign-up waiting: <b className="font-medium">{p.email}</b>{p.name ? ` (${p.name})` : ""}</span>
                    <button type="button" disabled={busy === `u${p.id}`}
                      className="rounded-full bg-mint px-2.5 py-0.5 font-semibold text-carbon hover:brightness-110 disabled:opacity-50"
                      onClick={() => {
                        if (!confirm(`Approve ${p.email}? They get a trading account with a new wallet.`)) return;
                        act(`u${p.id}`, () => api.approveUser(password, p.id, {}), `${p.email} approved`);
                      }}>
                      Approve
                    </button>
                    <button type="button" disabled={busy === `u${p.id}`}
                      className="rounded-full border border-bear/40 px-2.5 py-0.5 text-bear hover:bg-bear-soft disabled:opacity-50"
                      onClick={() => {
                        if (!confirm(`Block ${p.email}?`)) return;
                        act(`u${p.id}`, () => api.blockUser(password, p.id, true), `${p.email} blocked`);
                      }}>
                      Block
                    </button>
                  </div>
                ))}
              </div>
            )}

            {tab === "overview" && (
              <>
                {/* hero */}
                <section className="card relative overflow-hidden">
                  <div className="pointer-events-none absolute -top-24 -left-24 size-72 rounded-full bg-mint/10 blur-3xl" />
                  <div className="relative grid gap-6 p-5 sm:p-6 lg:grid-cols-[minmax(260px,340px)_1fr]">
                    <div className="flex flex-col gap-5">
                      <div>
                        <div className="flex items-center gap-2 text-[11px] font-semibold tracking-[0.12em] text-muted uppercase">
                          <Sparkles className="size-3.5 text-mint" /> Total equity
                        </div>
                        <Money v={equity} className="mt-1 block text-4xl font-medium tracking-tight sm:text-[44px]" />
                        <div className="mt-1 text-xs text-muted">
                          {money(t.sol_balance_usd)} in wallets · {money(atWork)} in {t.open_count} positions
                          {data.sol_usd ? <> · SOL {money(data.sol_usd)}</> : null}
                        </div>
                      </div>
                      <div>
                        <div className="mb-2 flex items-center justify-between">
                          <span className="text-[11px] font-semibold tracking-[0.12em] text-muted uppercase">Realized</span>
                          <div className="flex rounded-lg border border-line bg-carbon/60 p-0.5">
                            {(["24h", "7d", "30d", "all"] as Period[]).map((p) => (
                              <button key={p} type="button" onClick={() => setPeriod(p)}
                                className={`rounded-md px-2 py-0.5 text-[11px] font-medium transition ${period === p ? "bg-raised text-ink" : "text-muted hover:text-ink"}`}>
                                {p}
                              </button>
                            ))}
                          </div>
                        </div>
                        <Money v={realized} signed className={`text-3xl font-medium ${pctClass(realized)}`} />
                        <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
                          <div className="rounded-xl border border-line bg-carbon/40 p-3">
                            <div className="text-faint">Open P&L</div>
                            <div className={`num mt-0.5 text-sm ${pctClass(t.unrealized_usd)}`}>{money(t.unrealized_usd, true)}</div>
                          </div>
                          <div className="rounded-xl border border-line bg-carbon/40 p-3">
                            <div className="text-faint">Trades 30d</div>
                            <div className="num mt-0.5 text-sm text-ink">{t.trades_30d}</div>
                          </div>
                        </div>
                      </div>
                    </div>
                    <div className="min-w-0 lg:border-s lg:border-line lg:ps-6">
                      <div className="mb-1 flex items-center gap-2">
                        <BarChart3 className="size-4 text-mint" />
                        <h2 className="text-sm font-semibold">Realized P&L · all accounts</h2>
                      </div>
                      <PnlChart days={data.daily} />
                    </div>
                  </div>
                </section>

                {/* KPIs */}
                <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
                  <Kpi icon={<Wallet />} label="Wallets" sub={<>{sol(t.sol_balance)} · {t.enabled}/{t.accounts} accounts on{t.paused ? `, ${t.paused} paused` : ""}</>}>
                    <Money v={t.sol_balance_usd} className="text-[26px] font-medium" />
                  </Kpi>
                  <Kpi icon={<Layers />} label="Open positions" glow="bg-sky/15"
                    sub={<>{t.open_count} open · {money(t.open_cost_usd)} in</>}>
                    <div className="flex items-baseline gap-2">
                      <Money v={atWork} className="text-[26px] font-medium" />
                      <span className={`num text-sm ${pctClass(t.unrealized_usd)}`}>{money(t.unrealized_usd, true)}</span>
                    </div>
                  </Kpi>
                  <Kpi icon={<Target />} label="Win rate · 30d">
                    <div className="flex items-center gap-4">
                      <Ring value={t.win_rate_30d}>
                        <span className="num text-sm font-semibold">{t.win_rate_30d == null ? "—" : `${t.win_rate_30d.toFixed(0)}%`}</span>
                      </Ring>
                      <div className="text-xs text-muted">
                        <div><span className="num text-mint">{t.wins_30d}</span> wins</div>
                        <div><span className="num text-bear">{Math.max(0, t.trades_30d - t.wins_30d)}</span> losses</div>
                        <div className="text-faint">{t.trades_30d} closed</div>
                      </div>
                    </div>
                  </Kpi>
                  <Kpi icon={<Users />} label="Users" glow="bg-gold/10"
                    sub={<>+{u.new_7d} this week · {u.pending} pending · {u.blocked} blocked</>}>
                    <div className="num text-[26px] font-medium">{u.active}<span className="ms-1 text-sm text-muted">active</span></div>
                    <div className="mt-2">
                      <Stack parts={[[u.active, "bg-mint", "active"], [u.pending, "bg-gold", "pending"], [u.blocked, "bg-bear", "blocked"]]} />
                    </div>
                  </Kpi>
                </div>

                <div className="grid gap-6 lg:grid-cols-[1fr_360px]">
                  <TradesFeed items={data.recent} now={now} limit={8} />
                  <HealthStrip accounts={sorted} now={now} onAct={onAct} busy={busy}
                    scanner={`${data.signals.last_24h} signals in 24h · last ${since(data.signals.last_at, now)}`} />
                </div>

              </>
            )}

            {tab === "accounts" && (
              <>
                <div className="flex items-center gap-2">
                  <span className="text-sm text-muted">{data.accounts.length} accounts</span>
                  <div className="ms-auto flex rounded-lg border border-line bg-carbon/60 p-0.5">
                    {(["cards", "table"] as const).map((v) => (
                      <button key={v} type="button" onClick={() => setAcctView(v)}
                        className={`inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium capitalize transition ${acctView === v ? "bg-raised text-ink" : "text-muted hover:text-ink"}`}>
                        {v === "cards" ? <LayoutGrid className="size-3.5" /> : <Rows3 className="size-3.5" />}{v}
                      </button>
                    ))}
                  </div>
                </div>
                {acctView === "cards" ? (
                  <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                    {sorted.map((a) => <AccountCard key={a.name} a={a} now={now} best={best} onAct={onAct} busy={busy === a.name} />)}
                  </div>
                ) : (
                  <AccountsTable accounts={data.accounts} now={now} />
                )}
              </>
            )}

            {tab === "trades" && <TradesFeed items={data.recent} now={now} />}
            {tab === "scout" && <ScoutPanel password={password} />}
            {tab === "billing" && (
              <>
                <BillingPanel password={password} now={now} />
                <CouponsPanel password={password} now={now} />
              </>
            )}
            {tab === "alerts" && <TelegramPanel password={password} />}
          </div>
        )}
      </main>
    </div>
    </>
  );
}
