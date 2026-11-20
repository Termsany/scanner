"use client";

import {
  Activity,
  AlertTriangle,
  ArrowLeft,
  BarChart3,
  ExternalLink,
  LayoutDashboard,
  Lock,
  Receipt,
  RefreshCw,
  Ticket,
  UserPlus,
  Users,
  Wallet,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { api, ApiError } from "@/lib/api";
import { pct, pctClass } from "@/lib/format";
import type { AdminOverview, Bill, BillingAdmin, BillingConfig, Coupon, OverviewAccount } from "@/lib/types";

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

function Tile({ icon, label, value, sub, tone }: {
  icon: React.ReactNode;
  label: string;
  value: string;
  sub?: React.ReactNode;
  tone?: string;
}) {
  return (
    <div className="card flex flex-col gap-1 p-4">
      <div className="flex items-center gap-1.5 text-[11px] font-medium tracking-[0.12em] text-muted uppercase">
        {icon} {label}
      </div>
      <div className={`num text-xl font-semibold ${tone ?? ""}`}>{value}</div>
      {sub && <div className="text-xs text-muted">{sub}</div>}
    </div>
  );
}

function DailyBars({ days }: { days: AdminOverview["daily"] }) {
  const max = Math.max(1, ...days.map((d) => Math.abs(d.pnl)));
  const w = 100 / days.length;
  return (
    <svg viewBox="0 0 100 40" preserveAspectRatio="none" className="h-32 w-full" role="img"
      aria-label="Realized P&L per day, all accounts">
      <line x1="0" y1="20" x2="100" y2="20" stroke="currentColor" strokeOpacity="0.2" strokeWidth="0.2" />
      {days.map((d, i) => {
        const h = (Math.abs(d.pnl) / max) * 19;
        return (
          <rect key={d.day} x={i * w + w * 0.15} width={w * 0.7} y={d.pnl >= 0 ? 20 - h : 20}
            height={Math.max(h, d.trades ? 0.4 : 0)} className={d.pnl >= 0 ? "fill-bull" : "fill-bear"}>
            <title>{`${d.day}: ${money(d.pnl, true)} · ${d.trades} trades`}</title>
          </rect>
        );
      })}
    </svg>
  );
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
  else chips.push(["on", "bg-bull-soft text-bull"]);
  if (a.enabled && (beat == null || beat > STALE_MIN)) chips.push(["engine stale", "bg-bear-soft text-bear"]);
  if (a.billing_hold) chips.push(["unpaid: buys held", "bg-bear-soft text-bear"]);
  if (a.owner?.status === "blocked") chips.push(["blocked", "bg-bear-soft text-bear"]);
  return (
    <span className="flex flex-wrap gap-1">
      {chips.map(([t, c]) => (
        <span key={t} className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${c}`}>{t}</span>
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
    <th className={`px-3 py-2 font-medium ${right ? "text-right" : "text-left"}`}>
      <button type="button" className="hover:text-ink"
        onClick={() => (k === sort ? setDesc(!desc) : (setSort(k), setDesc(k !== "name")))}>
        {label}{sort === k ? (desc ? " ↓" : " ↑") : ""}
      </button>
    </th>
  );
  return (
    <section className="card overflow-hidden">
      <header className="flex items-center gap-2 border-b border-line px-4 py-3">
        <Wallet className="size-4 text-accent" />
        <h2 className="text-sm font-semibold">Accounts</h2>
        <span className="text-xs text-faint">{accounts.length} · click a column to sort</span>
      </header>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[980px] text-sm">
          <thead className="text-[11px] tracking-wide text-muted uppercase">
            <tr className="border-b border-line">
              {th("name", "Account", false)}
              <th className="px-3 py-2 text-left font-medium">Status</th>
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
              <tr key={a.name} className="border-b border-line last:border-0 hover:bg-raised/40">
                <td className="px-3 py-2">
                  <div className="font-medium">{a.name}</div>
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
                <td className="px-3 py-2"><Status a={a} now={now} /></td>
                <td className="num px-3 py-2 text-right">
                  <div>{money(a.sol_balance_usd)}</div>
                  <div className="text-xs text-faint">{sol(a.sol_balance)}</div>
                </td>
                <td className="num px-3 py-2 text-right">
                  <div>{a.open_count ? money(a.open_value_usd ?? a.open_cost_usd) : "—"}</div>
                  <div className="text-xs text-faint">
                    {a.open_count} pos · {money(a.open_cost_usd)} in
                  </div>
                </td>
                <td className={`num px-3 py-2 text-right ${pctClass(a.unrealized_usd)}`}>
                  {a.open_count ? money(a.unrealized_usd, true) : "—"}
                </td>
                <td className={`num px-3 py-2 text-right ${pctClass(a.realized_24h)}`}>{money(a.realized_24h, true)}</td>
                <td className={`num px-3 py-2 text-right ${pctClass(a.realized_7d)}`}>{money(a.realized_7d, true)}</td>
                <td className={`num px-3 py-2 text-right font-medium ${pctClass(a.realized_all)}`}>
                  {money(a.realized_all, true)}
                </td>
                <td className="num px-3 py-2 text-right">
                  <div>{a.win_rate == null ? "—" : `${a.win_rate.toFixed(0)}%`}</div>
                  <div className="text-xs text-faint">
                    {a.wins}/{a.trades}
                    {a.failed_24h > 0 && <span className="ms-1 text-bear">· {a.failed_24h} failed</span>}
                  </div>
                </td>
                <td className="px-3 py-2 text-right text-xs text-muted">{since(a.last_trade_at, now)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Recent({ items, now }: { items: AdminOverview["recent"]; now: number }) {
  return (
    <section className="card overflow-hidden">
      <header className="flex items-center gap-2 border-b border-line px-4 py-3">
        <Activity className="size-4 text-accent" />
        <h2 className="text-sm font-semibold">Latest closed and failed trades</h2>
      </header>
      {items.length === 0 ? (
        <p className="px-4 py-3 text-sm text-muted">No trades yet.</p>
      ) : (
        <ul className="divide-y divide-line">
          {items.map((p) => (
            <li key={p.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2 text-sm">
              <span className="w-20 shrink-0 text-xs text-faint">{since(p.at, now)}</span>
              <span className="w-16 shrink-0 text-xs text-muted">{p.account}</span>
              <a className="font-medium hover:text-accent" target="_blank" rel="noreferrer"
                href={`https://dexscreener.com/solana/${p.token_address}`}>
                ${p.symbol || short(p.token_address)}
              </a>
              {p.status === "failed" ? (
                <span className="truncate text-xs text-bear" title={p.error ?? ""}>
                  failed{p.error ? `: ${p.error}` : ""}
                </span>
              ) : (
                <>
                  <span className={`num ${pctClass(p.pnl_usd)}`}>{money(p.pnl_usd, true)}</span>
                  <span className={`num text-xs ${pctClass(p.pnl_pct)}`}>{pct(p.pnl_pct)}</span>
                  {p.exit_reason && <span className="text-xs text-faint">{p.exit_reason.replace(/_/g, " ")}</span>}
                </>
              )}
              <span className="num ms-auto text-xs text-faint">{money(p.cost_usd)} in</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

const STATUS_CLS: Record<Bill["status"], string> = {
  paid: "bg-bull-soft text-bull",
  due: "bg-warn-soft text-warn",
  check: "bg-bear-soft text-bear",
  sending: "bg-bear-soft text-bear",
  waived: "bg-raised text-muted",
};

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
      <header className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3">
        <Receipt className="size-4 text-accent" />
        <h2 className="text-sm font-semibold">Billing</h2>
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
          <table className="w-full min-w-[860px] text-sm">
            <thead className="text-[11px] tracking-wide text-muted uppercase">
              <tr className="border-b border-line">
                <th className="px-3 py-2 text-left font-medium">Bill</th>
                <th className="px-3 py-2 text-left font-medium">Period</th>
                <th className="px-3 py-2 text-right font-medium">Fee</th>
                <th className="px-3 py-2 text-right font-medium">Profit → share</th>
                <th className="px-3 py-2 text-right font-medium">Total</th>
                <th className="px-3 py-2 text-left font-medium">Status</th>
                <th className="px-3 py-2 text-right font-medium" />
              </tr>
            </thead>
            <tbody>
              {data.bills.map((b) => (
                <tr key={b.id} className="border-b border-line align-top last:border-0">
                  <td className="px-3 py-2">
                    <div className="font-medium">#{b.id} · {b.account}</div>
                    <div className="text-xs text-faint">{b.email ?? ""}</div>
                  </td>
                  <td className="px-3 py-2 text-xs text-muted">
                    {b.period_start.slice(0, 10)} → {b.period_end.slice(0, 10)}
                  </td>
                  <td className="num px-3 py-2 text-right">
                    {money(b.fee_usd)}
                    {b.discount_usd > 0 && (
                      <div className="text-xs text-bull">−{money(b.discount_usd)} {b.coupon_code}</div>
                    )}
                  </td>
                  <td className="num px-3 py-2 text-right">
                    {money(b.profit_usd)} → {money(b.share_usd)}
                    <div className="text-xs text-faint">{b.profit_pct}%</div>
                  </td>
                  <td className="num px-3 py-2 text-right font-medium">
                    {money(b.total_usd)}
                    {b.sol_amount != null && <div className="text-xs text-faint">{b.sol_amount.toFixed(4)} SOL</div>}
                  </td>
                  <td className="px-3 py-2">
                    <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${STATUS_CLS[b.status]}`}>
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
                  <td className="px-3 py-2 text-right whitespace-nowrap">
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
      <header className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3">
        <Ticket className="size-4 text-accent" />
        <h2 className="text-sm font-semibold">Discount codes</h2>
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
          <table className="w-full min-w-[760px] text-sm">
            <thead className="text-[11px] tracking-wide text-muted uppercase">
              <tr className="border-b border-line">
                <th className="px-3 py-2 text-left font-medium">Code</th>
                <th className="px-3 py-2 text-right font-medium">Discount</th>
                <th className="px-3 py-2 text-right font-medium">Months</th>
                <th className="px-3 py-2 text-right font-medium">Used</th>
                <th className="px-3 py-2 text-left font-medium">Apply by</th>
                <th className="px-3 py-2 text-left font-medium">Users</th>
                <th className="px-3 py-2 text-right font-medium" />
              </tr>
            </thead>
            <tbody>
              {list.map((c) => {
                const expired = c.expires_at != null && new Date(c.expires_at).getTime() < now;
                const full = c.max_uses != null && c.uses >= c.max_uses;
                return (
                  <tr key={c.id} className={`border-b border-line align-top last:border-0 ${c.active ? "" : "opacity-50"}`}>
                    <td className="px-3 py-2">
                      <div className="num font-medium">{c.code}</div>
                      {c.note && <div className="text-xs text-faint">{c.note}</div>}
                    </td>
                    <td className="num px-3 py-2 text-right">{c.percent}%</td>
                    <td className="num px-3 py-2 text-right">{c.months ?? "always"}</td>
                    <td className="num px-3 py-2 text-right">
                      {c.uses}{c.max_uses != null ? ` / ${c.max_uses}` : ""}
                      {full && <div className="text-xs text-warn">used up</div>}
                    </td>
                    <td className="px-3 py-2 text-xs text-muted">
                      {c.expires_at ? c.expires_at.slice(0, 10) : "—"}
                      {expired && <span className="ms-1 text-warn">expired</span>}
                    </td>
                    <td className="px-3 py-2 text-xs text-muted">{c.users.join(", ") || "—"}</td>
                    <td className="px-3 py-2 text-right">
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

export function AdminView() {
  const [password, setPassword] = useState("");
  const [inputPw, setInputPw] = useState("");
  const [data, setData] = useState<AdminOverview | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const saved = readSaved();
    if (saved) setPassword(saved);
  }, []);

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

  if (!password) {
    return (
      <main className="mx-auto flex min-h-[70vh] max-w-sm flex-col justify-center px-4">
        <form className="card flex flex-col gap-3 p-6"
          onSubmit={(e) => {
            e.preventDefault();
            if (inputPw) setPassword(inputPw);
          }}>
          <h1 className="flex items-center gap-2 text-base font-semibold">
            <Lock className="size-4 text-accent" /> Admin overview
          </h1>
          <p className="text-xs text-muted">Private page. Same password as the Live page.</p>
          <input type="password" autoComplete="current-password" value={inputPw}
            onChange={(e) => setInputPw(e.target.value)} placeholder="Password" className={input} />
          {error && <p className="text-xs text-bear">{error}</p>}
          <button type="submit" className="rounded-md bg-accent px-3 py-2 text-sm font-semibold text-carbon">
            Unlock
          </button>
        </form>
      </main>
    );
  }

  const t = data?.totals;
  const u = data?.users;
  const stale = data?.accounts.filter((a) => {
    const m = minutesSince(a.heartbeat_at, now);
    return a.enabled && (m == null || m > STALE_MIN);
  }) ?? [];

  return (
    <main className="mx-auto max-w-[1400px] px-4 pt-5 pb-12 sm:px-6">
      <header className="mb-5 flex flex-wrap items-center gap-3">
        <a href="/live" className={navBtn}><ArrowLeft className="size-3.5" /> Live</a>
        <a href="/accounts" className={navBtn}>Accounts</a>
        <LayoutDashboard className="size-5 text-accent" />
        <h1 className="text-lg font-semibold tracking-tight">Admin overview</h1>
        {data && (
          <>
            <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase ${
              data.mode === "live" ? "bg-bull-soft text-bull" : "bg-warn-soft text-warn"}`}>
              {data.mode}
            </span>
            {data.paused && (
              <span className="rounded bg-warn-soft px-1.5 py-0.5 text-[10px] font-semibold text-warn uppercase">
                all paused
              </span>
            )}
          </>
        )}
        <button type="button" onClick={() => load(password)}
          className={`${navBtn} ms-auto`} disabled={loading}>
          <RefreshCw className={`size-3.5 ${loading ? "animate-spin" : ""}`} />
          {data ? `Updated ${since(data.updated_at, now)}` : "Refresh"}
        </button>
      </header>

      {error && <p className="mb-4 text-sm text-bear">{error}</p>}
      {!data && !error && <p className="text-sm text-muted">Loading…</p>}

      {data && t && u && (
        <div className="flex flex-col gap-4">
          {(stale.length > 0 || t.failed_24h > 0) && (
            <div className="card flex flex-wrap items-center gap-2 border-bear/40 p-3 text-sm">
              <AlertTriangle className="size-4 text-bear" />
              {stale.length > 0 && (
                <span>Engine not stepping for: <b>{stale.map((a) => a.name).join(", ")}</b>.</span>
              )}
              {t.failed_24h > 0 && (
                <span>{t.failed_24h} failed trade{t.failed_24h > 1 ? "s" : ""} in the last 24h.</span>
              )}
            </div>
          )}

          {u.pending > 0 && (
            <div className="card flex flex-wrap items-center gap-2 border-accent/40 p-3 text-sm">
              <UserPlus className="size-4 text-accent" />
              <span>
                {u.pending} sign-up{u.pending > 1 ? "s" : ""} waiting for approval:{" "}
                <span className="text-muted">{u.pending_list.map((p) => p.email).join(", ")}</span>
              </span>
              <a href="/accounts" className={`${navBtn} ms-auto`}>Review on Accounts</a>
            </div>
          )}

          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Tile icon={<Wallet className="size-3.5" />} label="Wallets (SOL)" value={money(t.sol_balance_usd)}
              sub={<>{sol(t.sol_balance)} · {t.enabled}/{t.accounts} accounts on{t.paused ? `, ${t.paused} paused` : ""}</>} />
            <Tile icon={<Activity className="size-3.5" />} label="Open positions"
              value={money(t.open_value_usd ?? t.open_cost_usd)}
              sub={<>
                {t.open_count} open · {money(t.open_cost_usd)} in ·{" "}
                <span className={pctClass(t.unrealized_usd)}>{money(t.unrealized_usd, true)}</span>
              </>} />
            <Tile icon={<BarChart3 className="size-3.5" />} label="Realized 24h" value={money(t.realized_24h, true)}
              tone={pctClass(t.realized_24h)}
              sub={<>
                7d <span className={pctClass(t.realized_7d)}>{money(t.realized_7d, true)}</span> · 30d{" "}
                <span className={pctClass(t.realized_30d)}>{money(t.realized_30d, true)}</span> · all{" "}
                <span className={pctClass(t.realized_all)}>{money(t.realized_all, true)}</span>
              </>} />
            <Tile icon={<Users className="size-3.5" />} label="Users" value={String(u.active)}
              sub={<>active · {u.pending} pending · {u.blocked} blocked · +{u.new_7d} this week</>} />
          </div>

          <section className="card p-4">
            <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
              <span className="text-[11px] font-medium tracking-[0.12em] uppercase">Realized P&L per day · all accounts</span>
              <span>
                30d: {t.trades_30d} trades · win rate {t.win_rate_30d == null ? "—" : `${t.win_rate_30d.toFixed(0)}%`}
              </span>
              <span className="ms-auto">
                Scanner: {data.signals.last_24h} signals in 24h · last {since(data.signals.last_at, now)}
              </span>
            </div>
            <DailyBars days={data.daily} />
            <div className="mt-1 flex justify-between text-[10px] text-faint">
              <span>{data.daily[0]?.day}</span>
              <span>{data.daily[data.daily.length - 1]?.day}</span>
            </div>
          </section>

          <AccountsTable accounts={data.accounts} now={now} />
          <BillingPanel password={password} now={now} />
          <CouponsPanel password={password} now={now} />
          <Recent items={data.recent} now={now} />
        </div>
      )}
    </main>
  );
}
