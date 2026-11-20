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

import { AdminSignIn, adminSignOut, useAdminResume } from "@/components/AdminAuth";
import { AppNav } from "@/components/AppNav";
import { Badge, button, Empty, PageHeader, Panel, StatTile } from "@/components/ui";
import { api, ApiError, SESSION } from "@/lib/api";
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

function Tile({ icon, label, value, sub, tone }: {
  icon: React.ReactNode;
  label: string;
  value: string;
  sub?: React.ReactNode;
  tone?: string;
}) {
  return <StatTile icon={icon} label={label} value={value} sub={sub} tone={tone || "text-ink"} />;
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

function Recent({ items, now }: { items: AdminOverview["recent"]; now: number }) {
  return (
    <Panel icon={<Activity />} title="Latest closed and failed trades" meta={`${items.length} most recent`} flush>
      {items.length === 0 ? (
        <Empty>No trades yet.</Empty>
      ) : (
        <div className="overflow-x-auto">
          <table className="data-table compact min-w-[720px]">
            <thead>
              <tr>
                <th className="text-left">When</th>
                <th className="text-left">Account</th>
                <th className="text-left">Token</th>
                <th className="text-right">Cost</th>
                <th className="text-right">Result</th>
                <th className="text-left">Exit</th>
              </tr>
            </thead>
            <tbody>
              {items.map((p) => (
                <tr key={p.id}>
                  <td className="text-xs text-muted">{since(p.at, now)}</td>
                  <td><Badge tone="muted" className="num text-ink">{p.account}</Badge></td>
                  <td>
                    <a className="font-medium hover:text-mint" target="_blank" rel="noreferrer"
                      href={`https://dexscreener.com/solana/${p.token_address}`}>
                      ${p.symbol || short(p.token_address)}
                    </a>
                  </td>
                  <td className="num text-right text-muted">{money(p.cost_usd)}</td>
                  <td className="num text-right">
                    {p.status === "failed" ? <span className="text-faint">—</span> : (
                      <>
                        <span className={pctClass(p.pnl_usd)}>{money(p.pnl_usd, true)}</span>
                        <span className={`ms-2 text-xs ${pctClass(p.pnl_pct)}`}>{pct(p.pnl_pct)}</span>
                      </>
                    )}
                  </td>
                  <td className="max-w-[320px] truncate text-xs">
                    {p.status === "failed"
                      ? <span className="text-bear" title={p.error ?? ""}>failed{p.error ? `: ${p.error}` : ""}</span>
                      : <span className="text-muted">{(p.exit_reason ?? "").replace(/_/g, " ")}</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
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

export function AdminView() {
  const [password, setPassword] = useState("");
  const [inputPw, setInputPw] = useState("");
  const [data, setData] = useState<AdminOverview | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [now, setNow] = useState(() => Date.now());

  useAdminResume(setPassword);

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
      <>
        <AppNav />
        <AdminSignIn onDone={() => setPassword(SESSION)} />
      </>
    );
  }

  const t = data?.totals;
  const u = data?.users;
  const stale = data?.accounts.filter((a) => {
    const m = minutesSince(a.heartbeat_at, now);
    return a.enabled && (m == null || m > STALE_MIN);
  }) ?? [];

  return (
    <>
    <AppNav />
    <main className="mx-auto max-w-[1400px] px-4 pt-5 pb-12 sm:px-6">
      <PageHeader icon={<LayoutDashboard className="size-5" />} title="Admin overview"
        subtitle="Every account, user and trade at a glance. Read-only; changes happen on Accounts and Live."
        badges={data && (
          <>
            <Badge tone={data.mode === "live" ? "mint" : "gold"} dot>{data.mode.toUpperCase()}</Badge>
            {data.paused && <Badge tone="gold">ALL PAUSED</Badge>}
          </>
        )}
        actions={
          <button type="button" onClick={() => load(password)} className={button("secondary")} disabled={loading}>
            <RefreshCw className={`size-3.5 ${loading ? "animate-spin" : ""}`} />
            {data ? `Updated ${since(data.updated_at, now)}` : "Refresh"}
          </button>
        } />

      {error && <p className="mb-4 text-sm text-bear">{error}</p>}
      {!data && !error && <p className="text-sm text-muted">Loading…</p>}

      {data && t && u && (
        <div className="flex flex-col gap-6">
          {(stale.length > 0 || t.failed_24h > 0) && (
            <div className="flex flex-wrap items-center gap-2 rounded-2xl border border-bear/30 bg-bear-soft px-4 py-3 text-sm">
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
            <div className="flex flex-wrap items-center gap-2 rounded-2xl border border-mint/30 bg-mint-soft/60 px-4 py-3 text-sm">
              <UserPlus className="size-4 text-accent" />
              <span>
                {u.pending} sign-up{u.pending > 1 ? "s" : ""} waiting for approval:{" "}
                <span className="text-muted">{u.pending_list.map((p) => p.email).join(", ")}</span>
              </span>
              <a href="/accounts" className={`${navBtn} ms-auto`}>Review on Accounts</a>
            </div>
          )}

          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
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

          <Panel icon={<BarChart3 />} title="Realized P&L per day · all accounts"
            meta={`30d: ${t.trades_30d} trades · win rate ${t.win_rate_30d == null ? "—" : `${t.win_rate_30d.toFixed(0)}%`}`}
            actions={<span className="text-xs text-muted">Scanner: {data.signals.last_24h} signals in 24h · last {since(data.signals.last_at, now)}</span>}>
            <DailyBars days={data.daily} />
            <div className="mt-2 flex justify-between text-[11px] text-faint">
              <span className="num">{data.daily[0]?.day}</span>
              <span className="num">{data.daily[data.daily.length - 1]?.day}</span>
            </div>
          </Panel>

          <AccountsTable accounts={data.accounts} now={now} />
          <BillingPanel password={password} now={now} />
          <CouponsPanel password={password} now={now} />
          <Recent items={data.recent} now={now} />
        </div>
      )}
    </main>
    </>
  );
}
