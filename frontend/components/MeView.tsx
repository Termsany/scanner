"use client";

import {
  AlertTriangle,
  ArrowUpRight,
  BarChart3,
  Clock,
  Copy,
  ExternalLink,
  KeyRound,
  LogOut,
  Pause,
  Play,
  Save,
  Send,
  ShieldCheck,
  Receipt,
  Wallet,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { SirbLockup } from "@/components/brand/Logo";
import { api, ApiError } from "@/lib/api";
import { I18nProvider, LangSwitch, useT } from "@/lib/i18n";
import type { Me, MyAccount, MyBilling, MyPosition, MyStats } from "@/lib/types";

const REFRESH_MS = 5_000;
const input =
  "w-full rounded-md border border-line bg-carbon px-2.5 py-1.5 text-sm outline-none focus:border-accent";
const codeInput =
  "num w-full rounded-md border border-line bg-carbon px-2.5 py-1.5 text-sm tracking-[0.3em] outline-none focus:border-accent";

function money(v: number | null | undefined, signed = false) {
  if (v == null) return "—";
  const s = Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (!signed) return `$${s}`;
  return `${v >= 0 ? "+" : "−"}$${s}`;
}

function tone(v: number | null | undefined) {
  if (v == null || v === 0) return "";
  return v > 0 ? "text-bull" : "text-bear";
}

function errText(e: unknown, srv: (m: string) => string, fallback: string) {
  return e instanceof ApiError ? srv(e.message) : fallback;
}

function DailyBars({ days }: { days: MyStats["daily"] }) {
  const { t } = useT();
  const max = Math.max(1, ...days.map((d) => Math.abs(d.pnl)));
  const w = 100 / days.length;
  return (
    // SVG x coordinates are absolute: the chart runs oldest -> newest left to right in Arabic too
    <svg viewBox="0 0 100 40" preserveAspectRatio="none" className="h-28 w-full" role="img"
      aria-label={t("f.chart")}>
      <line x1="0" y1="20" x2="100" y2="20" stroke="currentColor" strokeOpacity="0.2" strokeWidth="0.2" />
      {days.map((d, i) => {
        const h = (Math.abs(d.pnl) / max) * 19;
        return (
          <rect key={d.day} x={i * w + w * 0.15} width={w * 0.7} y={d.pnl >= 0 ? 20 - h : 20}
            height={Math.max(h, d.trades ? 0.4 : 0)} className={d.pnl >= 0 ? "fill-bull" : "fill-bear"}>
            <title>{`${d.day}: ${money(d.pnl, true)} · ${d.trades} ${t("f.trades")}`}</title>
          </rect>
        );
      })}
    </svg>
  );
}

function day(iso: string | null | undefined) {
  return iso ? iso.slice(0, 10) : "—";
}

function CouponBox({ onDone }: { onDone: () => void }) {
  const { t, srv } = useT();
  const [code, setCode] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <form className="mt-3 flex flex-wrap items-center gap-2"
      onSubmit={async (e) => {
        e.preventDefault();
        if (!code.trim()) return;
        setBusy(true);
        setMsg("");
        try {
          await api.applyCoupon(code.trim());
          setMsg(t("b.coupon_ok"));
          setCode("");
          onDone();
        } catch (err) {
          setMsg(err instanceof ApiError ? srv(err.message) : t("c.error"));
        } finally {
          setBusy(false);
        }
      }}>
      <input className={`${input} max-w-[200px]`} dir="ltr" placeholder={t("b.coupon_ph")} value={code}
        maxLength={32} onChange={(e) => setCode(e.target.value)} />
      <button type="submit" disabled={busy}
        className="rounded-md border border-line px-3 py-1.5 text-sm hover:bg-raised disabled:opacity-50">
        {t("b.coupon_apply")}
      </button>
      {msg && <span className="text-xs text-muted">{msg}</span>}
      <span className="w-full text-xs text-faint">{t("b.coupon_hint")}</span>
    </form>
  );
}

function BillingCard({ b, onChange }: { b: MyBilling; onChange: () => void }) {
  const { t } = useT();
  const c = b.current;
  const row = (label: string, value: string, cls = "") => (
    <div className="flex items-center justify-between gap-3 py-1.5 text-sm">
      <span className="text-muted">{label}</span>
      <span className={`num ${cls}`} dir="ltr">{value}</span>
    </div>
  );
  return (
    <section className="card p-4 sm:p-5">
      <h2 className="mb-2 flex items-center gap-2 text-sm font-semibold">
        <Receipt className="size-4 text-accent" /> {t("b.title")}
      </h2>
      {b.hold && (
        <p className="mb-3 flex items-start gap-2 rounded-md bg-bear-soft p-2.5 text-xs text-bear">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" /> {t("b.hold")}
        </p>
      )}
      {b.coupon && (
        <p className="mb-2 rounded-md bg-bull-soft px-2.5 py-1.5 text-xs text-bull">
          {t("b.coupon_on", { c: b.coupon.code, p: b.coupon.percent })}
          {b.coupon.months_left != null && t("b.coupon_left", { n: b.coupon.months_left })}
        </p>
      )}
      {c.trial ? (
        <p className="text-sm text-bull">{t("b.trial", { d: day(c.trial_ends) })}</p>
      ) : (
        <>
          <p className="mb-1 text-xs text-faint">{t("b.period", { a: day(c.period_start), b: day(c.period_end) })}</p>
          <div className="divide-y divide-line">
            {row(t("b.fee"), money(c.fee_usd))}
            {c.discount_usd > 0 && b.coupon && row(t("b.discount", { c: b.coupon.code }), `−${money(c.discount_usd)}`, "text-bull")}
            {row(t("b.profit"), money(Math.max(c.profit_usd, 0)), c.profit_usd > 0 ? "text-bull" : "")}
            {row(t("b.share", { pct: c.profit_pct }), money(c.share_usd))}
            {row(t("b.est"), money(c.total_usd), "font-semibold")}
          </div>
        </>
      )}
      <p className="mt-2 text-xs text-faint">{t("b.how")}</p>
      {!b.coupon && <CouponBox onDone={onChange} />}
      {b.bills.length > 0 && (
        <div className="mt-3">
          <div className="mb-1 text-[11px] font-medium tracking-[0.12em] text-muted uppercase">{t("b.history")}</div>
          <ul className="divide-y divide-line text-sm">
            {b.bills.map((x) => (
              <li key={x.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-1.5">
                <span className="text-xs text-faint" dir="ltr">{day(x.period_start)} → {day(x.period_end)}</span>
                <span className="num" dir="ltr">{money(x.total_usd)}</span>
                <span className={`rounded px-1.5 py-0.5 text-[10px] ${
                  x.status === "paid" ? "bg-bull-soft text-bull" : x.status === "due" ? "bg-warn-soft text-warn"
                    : "bg-raised text-muted"}`}>
                  {t(`b.s.${x.status}` as Parameters<typeof t>[0])}
                </span>
                {x.signature && (
                  <a className="ms-auto text-xs text-faint hover:text-ink" target="_blank" rel="noreferrer"
                    href={`https://solscan.io/tx/${x.signature}`}>
                    <ExternalLink className="inline size-3" />
                  </a>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function PerformanceCard({ s }: { s: MyStats }) {
  const { t } = useT();
  // mixed = the value has words too ("46% of 24"): let it follow the page direction instead of forcing LTR
  const tile = (label: string, value: string, cls = "", mixed = false) => (
    <div className="rounded-md border border-line px-3 py-2">
      <div className="text-[10px] tracking-[0.12em] text-muted uppercase">{label}</div>
      <div className={`${mixed ? "tabular-nums" : "num"} mt-1 text-base font-semibold ${cls}`}>{value}</div>
    </div>
  );
  return (
    <section className="card p-4 sm:p-5">
      <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold">
        <BarChart3 className="size-4 text-accent" /> {t("f.title")}
      </h2>
      {s.trades === 0 ? (
        <p className="text-sm text-faint">{t("f.none")}</p>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-6">
            {tile(t("f.total"), money(s.total_pnl, true), s.total_pnl >= 0 ? "text-bull" : "text-bear")}
            {tile(t("f.roi"), s.roi_pct == null ? "—" : `${s.roi_pct >= 0 ? "+" : ""}${s.roi_pct.toFixed(1)}%`)}
            {tile(t("f.winrate"), s.win_rate == null ? "—" : t("f.winrate_v", { p: s.win_rate.toFixed(0), n: s.trades }), "", true)}
            {tile(t("f.avgwin"), money(s.avg_win, true), "text-bull")}
            {tile(t("f.avgloss"), money(s.avg_loss, true), "text-bear")}
            {tile(t("f.bestworst"), `${s.best ? `$${s.best.symbol}` : "—"} / ${s.worst ? `$${s.worst.symbol}` : "—"}`)}
          </div>
          <div className="mt-4">
            <div className="mb-1 flex justify-between text-[11px] text-muted">
              <span>{t("f.daily")}</span>
              <span dir="ltr">{s.daily[0]?.day} → {s.daily[s.daily.length - 1]?.day}</span>
            </div>
            <DailyBars days={s.daily} />
          </div>
        </>
      )}
    </section>
  );
}

function TelegramCard({ me, onDone }: { me: Me; onDone: () => void }) {
  const { t, srv } = useT();
  const [msg, setMsg] = useState("");
  const linked = me.telegram.linked;
  return (
    <section className="card p-4 sm:p-5">
      <h2 className="mb-2 flex items-center gap-2 text-sm font-semibold">
        <Send className="size-4 text-accent" /> {t("t.title")}
        <span className={`rounded-full border px-2 py-0.5 text-[10px] font-bold tracking-[0.1em] ${linked ? "border-bull/50 text-bull" : "border-line text-muted"}`}>
          {linked ? t("t.on") : t("t.off")}
        </span>
      </h2>
      <p className="mb-3 text-xs text-muted">{t("t.hint")}</p>
      <div className="flex flex-wrap items-center gap-3">
        {!linked ? (
          <button type="button"
            className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-xs font-semibold text-carbon"
            onClick={async () => {
              setMsg("");
              try {
                const r = await api.telegramLink();
                window.open(r.url, "_blank", "noopener");
                setMsg(t("t.opened"));
              } catch (e) {
                setMsg(errText(e, srv, t("t.link_fail")));
              }
            }}>
            <Send className="size-3.5" /> {t("t.connect")}
          </button>
        ) : (
          <>
            <label className="flex items-center gap-2 text-xs">
              <input type="checkbox" checked={me.telegram.daily}
                onChange={async (e) => { await api.telegramDaily(e.target.checked).catch(() => undefined); onDone(); }} />
              {t("t.daily")}
            </label>
            <button type="button" className="rounded-md border border-line px-3 py-1.5 text-xs hover:bg-raised"
              onClick={async () => { await api.telegramUnlink().catch(() => undefined); onDone(); }}>
              {t("t.disconnect")}
            </button>
          </>
        )}
        {msg && <span className="text-xs text-muted">{msg}</span>}
      </div>
    </section>
  );
}

function SecurityCard({ me, onDone }: { me: Me; onDone: () => void }) {
  const { t, srv } = useT();
  const on = me.security.totp_enabled;
  const [pw, setPw] = useState("");
  const [code, setCode] = useState("");
  const [setup, setSetup] = useState<{ secret: string; qr: string } | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [cur, setCur] = useState("");
  const [next, setNext] = useState("");

  async function run(fn: () => Promise<unknown>, ok: string) {
    setMsg(null);
    try {
      await fn();
      setMsg({ ok: true, text: ok });
      setPw("");
      setCode("");
      onDone();
    } catch (e) {
      setMsg({ ok: false, text: errText(e, srv, t("c.failed")) });
    }
  }

  return (
    <section className="card p-4 sm:p-5">
      <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold">
        <ShieldCheck className="size-4 text-accent" /> {t("z.title")}
        <span className={`rounded-full border px-2 py-0.5 text-[10px] font-bold tracking-[0.1em] ${on ? "border-bull/50 text-bull" : "border-warn/60 text-warn"}`}>
          {on ? t("z.on") : t("z.off")}
        </span>
      </h2>
      {me.security.withdraw_lock_hours > 0 && (
        <p className="mb-3 rounded-md border border-warn/50 px-3 py-2 text-xs text-warn">
          {t("z.locked", { h: Math.ceil(me.security.withdraw_lock_hours) })}
        </p>
      )}
      <div className="grid gap-4 lg:grid-cols-2">
        <div className="flex flex-col gap-2">
          <span className="text-xs font-semibold">{t("z.codes")}</span>
          {!on && !setup && (
            <>
              <p className="text-xs text-muted">{t("z.hint")}</p>
              <input className={input} type="password" placeholder={t("c.password")} autoComplete="current-password"
                value={pw} onChange={(e) => setPw(e.target.value)} />
              <button type="button" disabled={!pw}
                className="self-start rounded-md bg-accent px-3 py-1.5 text-xs font-semibold text-carbon disabled:opacity-40"
                onClick={async () => {
                  setMsg(null);
                  try {
                    const r = await api.totpSetup(pw);
                    setSetup({ secret: r.secret, qr: r.qr });
                    setPw("");
                  } catch (e) {
                    setMsg({ ok: false, text: errText(e, srv, t("c.failed")) });
                  }
                }}>
                {t("z.setup")}
              </button>
            </>
          )}
          {!on && setup && (
            <>
              <p className="text-xs text-muted">{t("z.step1")}</p>
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={setup.qr} alt={t("z.qr_alt")} className="size-44 rounded-md bg-white p-1" />
              <code className="num text-xs break-all text-muted">{setup.secret}</code>
              <p className="text-xs text-muted">{t("z.step2")}</p>
              <input className={codeInput} inputMode="numeric" maxLength={6} placeholder="123456" value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))} />
              <button type="button" disabled={code.length !== 6}
                className="self-start rounded-md bg-accent px-3 py-1.5 text-xs font-semibold text-carbon disabled:opacity-40"
                onClick={() => run(async () => { await api.totpEnable(code); setSetup(null); }, t("z.on_ok"))}>
                {t("z.turn_on")}
              </button>
            </>
          )}
          {on && (
            <>
              <p className="text-xs text-muted">{t("z.on_text")}</p>
              <details className="text-xs">
                <summary className="cursor-pointer text-muted hover:text-ink">{t("z.turn_off")}</summary>
                <div className="mt-2 flex flex-col gap-2">
                  <input className={input} type="password" placeholder={t("c.password")} value={pw}
                    autoComplete="current-password" onChange={(e) => setPw(e.target.value)} />
                  <input className={codeInput} inputMode="numeric" maxLength={6} placeholder={t("c.code")} value={code}
                    onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))} />
                  <button type="button" disabled={!pw || code.length !== 6}
                    className="self-start rounded-md border border-bear/60 px-3 py-1.5 text-xs text-bear disabled:opacity-40"
                    onClick={() => confirm(t("z.off_q")) && run(() => api.totpDisable(pw, code), t("z.off_ok"))}>
                    {t("z.off_btn")}
                  </button>
                </div>
              </details>
            </>
          )}
        </div>
        <div className="flex flex-col gap-2">
          <span className="text-xs font-semibold">{t("z.pw_title")}</span>
          <input className={input} type="password" placeholder={t("z.pw_cur")} autoComplete="current-password"
            value={cur} onChange={(e) => setCur(e.target.value)} />
          <input className={input} type="password" placeholder={t("z.pw_new")} autoComplete="new-password"
            value={next} onChange={(e) => setNext(e.target.value)} />
          <button type="button" disabled={!cur || next.length < 10}
            className="self-start rounded-md border border-line px-3 py-1.5 text-xs hover:bg-raised disabled:opacity-40"
            onClick={() => run(async () => { await api.changePassword(cur, next); setCur(""); setNext(""); }, t("z.pw_ok"))}>
            {t("z.pw_title")}
          </button>
        </div>
      </div>
      {msg && <p className={`mt-3 text-xs ${msg.ok ? "text-bull" : "text-bear"}`}>{msg.text}</p>}
    </section>
  );
}

function Stat({ label, value, sub, cls }: { label: string; value: string; sub?: string; cls?: string }) {
  return (
    <div className="card px-4 py-3.5">
      <div className="text-[11px] font-medium tracking-[0.14em] text-muted uppercase">{label}</div>
      <div className={`num mt-2 text-2xl font-semibold ${cls ?? ""}`}>{value}</div>
      {sub && <div className="mt-1 text-xs text-faint">{sub}</div>}
    </div>
  );
}

function Positions({ rows, live, onSell }: {
  rows: MyPosition[];
  live: boolean;
  onSell?: (p: MyPosition, share: number) => void;
}) {
  const { t, exit } = useT();
  if (!rows.length) return <p className="px-4 py-6 text-sm text-faint">{live ? t("p.none_open") : t("p.none_closed")}</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[560px] text-sm">
        <thead className="text-[11px] text-muted uppercase">
          <tr className="border-b border-line">
            <th className="px-4 py-2 text-start">{t("p.token")}</th>
            <th className="px-4 py-2 text-right">{t("p.cost")}</th>
            <th className="px-4 py-2 text-right">{live ? t("p.value") : t("p.sold_for")}</th>
            <th className="px-4 py-2 text-right">{t("p.pnl")}</th>
            <th className="px-4 py-2 text-start">{live ? t("p.sell") : t("p.exit")}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((p) => {
            const pnl = live ? (p.unrealized_usd ?? null) : p.pnl_usd;
            const pnlPct = live ? (p.unrealized_pct ?? null) : p.cost_usd ? ((p.pnl_usd ?? 0) / p.cost_usd) * 100 : null;
            return (
              <tr key={p.id} className="border-b border-line/60 last:border-0">
                <td className="px-4 py-2">
                  <a className="inline-flex items-center gap-1 font-medium hover:underline" target="_blank" rel="noreferrer"
                    dir="ltr" href={`https://solscan.io/token/${p.token_address}`}>
                    ${p.symbol || p.token_address.slice(0, 6)} <ExternalLink className="size-3" />
                  </a>
                </td>
                <td className="num px-4 py-2 text-right">{money(p.cost_usd)}</td>
                <td className="num px-4 py-2 text-right">{money(live ? p.value_usd : p.proceeds_usd)}</td>
                <td className={`num px-4 py-2 text-right ${tone(pnl)}`}>
                  {money(pnl, true)}
                  {pnlPct != null && <span className="block text-[10px]">{pnlPct >= 0 ? "+" : ""}{pnlPct.toFixed(0)}%</span>}
                </td>
                <td className="px-4 py-2">
                  {live && onSell ? (
                    p.manual_sell_frac ? (
                      <span className="text-xs text-warn">{t("p.selling", { p: p.manual_sell_frac * 100 })}</span>
                    ) : (
                      <span className="flex gap-1" dir="ltr">
                        {[0.25, 0.5, 1].map((sh) => (
                          <button key={sh} type="button"
                            className="rounded border border-line px-1.5 py-0.5 text-[11px] hover:bg-raised"
                            onClick={() => onSell(p, sh)}>
                            {sh * 100}%
                          </button>
                        ))}
                      </span>
                    )
                  ) : (
                    <span className="text-xs text-muted">{exit(p.exit_reason)}</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function SettingsCard({ a, onSaved }: { a: MyAccount; onSaved: () => void }) {
  const { t, srv } = useT();
  const st = a.settings;
  const [size, setSize] = useState(String(st.size_usd));
  const [open, setOpen] = useState(String(st.max_open_usd));
  const [loss, setLoss] = useState(String(st.max_daily_loss_usd));
  const [takeOn, setTakeOn] = useState(st.take_capital_at_pct != null);
  const [take, setTake] = useState(String(st.take_capital_at_pct ?? 100));
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const cap = (v: number | null) => (v != null ? t("s.cap", { v: money(v) }) : undefined);

  async function save(body: Record<string, unknown>, ok: string) {
    setMsg(null);
    try {
      await api.mySettings(body);
      setMsg({ ok: true, text: ok });
      onSaved();
    } catch (e) {
      setMsg({ ok: false, text: errText(e, srv, t("s.save_fail")) });
    }
  }

  const field = (label: string, v: string, set: (s: string) => void, hint?: string) => (
    <label className="flex flex-col gap-1">
      <span className="text-[11px] font-medium tracking-[0.12em] text-muted uppercase">{label}</span>
      <input className={`${input} num`} inputMode="decimal" value={v} onChange={(e) => set(e.target.value)} />
      {hint && <span className="text-[11px] text-faint">{hint}</span>}
    </label>
  );

  return (
    <section className="card p-4 sm:p-5">
      <header className="mb-3 flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold">{t("s.title")}</h2>
        <span className={`rounded-full border px-2 py-0.5 text-[10px] font-bold tracking-[0.1em] ${st.paused ? "border-warn/60 text-warn" : "border-bull/50 text-bull"}`}>
          {st.paused ? t("s.paused") : t("s.active")}
        </span>
        <button type="button"
          className="ms-auto inline-flex items-center gap-1 rounded-md border border-line px-2.5 py-1 text-xs hover:bg-raised"
          onClick={() => save({ paused: !st.paused }, st.paused ? t("s.resumed") : t("s.paused_ok"))}>
          {st.paused ? <Play className="size-3.5" /> : <Pause className="size-3.5" />}
          {st.paused ? t("s.resume") : t("s.pause")}
        </button>
      </header>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {field(t("s.entry"), size, setSize, cap(st.caps.size_usd) ?? t("s.entry_h"))}
        {field(t("s.budget"), open, setOpen, cap(st.caps.max_open_usd) ?? t("s.budget_h"))}
        {field(t("s.loss"), loss, setLoss, cap(st.caps.max_daily_loss_usd) ?? t("s.loss_h"))}
        <label className="flex flex-col gap-1">
          <span className="text-[11px] font-medium tracking-[0.12em] text-muted uppercase">{t("s.take")}</span>
          <span className="flex items-center gap-2">
            <input type="checkbox" checked={takeOn} onChange={(e) => setTakeOn(e.target.checked)} />
            <input className={`${input} num`} inputMode="decimal" disabled={!takeOn} value={take}
              onChange={(e) => setTake(e.target.value)} />
            <span className="text-sm text-muted">%</span>
          </span>
          <span className="text-[11px] text-faint">{t("s.take_h")}</span>
        </label>
      </div>
      <footer className="mt-4 flex flex-wrap items-center gap-3">
        <button type="button"
          className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-carbon"
          onClick={() => save({
            size_usd: Number(size), max_open_usd: Number(open), max_daily_loss_usd: Number(loss),
            ...(takeOn ? { take_capital_at_pct: Number(take) } : { take_capital_off: true }),
          }, t("c.saved"))}>
          <Save className="size-4" /> {t("c.save")}
        </button>
        {msg && <span className={`text-xs ${msg.ok ? "text-bull" : "text-bear"}`}>{msg.text}</span>}
      </footer>
    </section>
  );
}

function WalletCard({ a, onDone, totp }: { a: MyAccount; onDone: () => void; totp: boolean }) {
  const { t, srv } = useT();
  const [pw, setPw] = useState("");
  const [code, setCode] = useState("");
  const [to, setTo] = useState("");
  const [amount, setAmount] = useState("");
  const [secret, setSecret] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState("");

  async function copy(text: string, what: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(what);
    } catch {
      /* select by hand */
    }
  }

  return (
    <section className="card p-4 sm:p-5">
      <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold">
        <Wallet className="size-4 text-accent" /> {t("w.title")}
      </h2>
      {a.pubkey && (
        <div className="mb-3">
          <div className="text-[11px] font-medium tracking-[0.12em] text-muted uppercase">{t("w.deposit")}</div>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-sm">
            <span className="num break-all">{a.pubkey}</span>
            <button type="button" className="inline-flex items-center gap-1 text-xs text-muted hover:text-ink"
              onClick={() => copy(a.pubkey!, "address")}>
              <Copy className="size-3" /> {copied === "address" ? t("c.copied") : t("c.copy")}
            </button>
            <a className="inline-flex items-center gap-1 text-xs text-muted hover:underline" target="_blank" rel="noreferrer"
              href={`https://solscan.io/account/${a.pubkey}`}>Solscan <ExternalLink className="size-3" /></a>
          </div>
          <div className="mt-1 text-xs text-muted">
            {t("w.balance")}{" "}
            <span className="num inline-block">
              {a.sol_balance != null ? `${a.sol_balance.toFixed(4)} SOL` : "—"} {a.sol_balance_usd != null ? `(${money(a.sol_balance_usd)})` : ""}
            </span>
          </div>
        </div>
      )}

      {secret ? (
        <div className="mb-3 rounded-md border border-warn/60 p-3">
          <p className="mb-2 text-xs text-warn">{t("w.key_warn")}</p>
          <div className="num rounded-md border border-line bg-carbon px-3 py-2 text-xs break-all">{secret}</div>
          <div className="mt-2 flex gap-2">
            <button type="button" className="inline-flex items-center gap-1 rounded-md border border-line px-3 py-1 text-xs hover:bg-raised"
              onClick={() => copy(secret, "key")}><Copy className="size-3.5" /> {copied === "key" ? t("c.copied") : t("c.copy")}</button>
            <button type="button" className="rounded-md bg-accent px-3 py-1 text-xs font-semibold text-carbon"
              onClick={() => confirm(t("w.saved_q")) && setSecret(null)}>{t("w.saved")}</button>
          </div>
        </div>
      ) : null}

      <div className="grid gap-3 lg:grid-cols-2">
        <div className="flex flex-col gap-2 rounded-md border border-line p-3">
          <span className="text-xs font-semibold">{t("w.withdraw_t")}</span>
          <input className={`${input} num`} dir="ltr" placeholder={t("w.to")} value={to} onChange={(e) => setTo(e.target.value)} />
          <input className={`${input} num`} placeholder={t("w.amount")} inputMode="decimal" value={amount}
            onChange={(e) => setAmount(e.target.value)} />
          <p className="text-[11px] text-faint">{t("w.keep")}</p>
        </div>
        <div className="flex flex-col gap-2 rounded-md border border-line p-3">
          <span className="text-xs font-semibold">{totp ? t("w.confirm_code") : t("w.confirm")}</span>
          <input className={input} type="password" placeholder={t("w.acct_pw")} autoComplete="current-password"
            value={pw} onChange={(e) => setPw(e.target.value)} />
          {totp ? (
            <input className={codeInput} inputMode="numeric" maxLength={6} placeholder={t("w.code")} value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))} />
          ) : (
            <p className="text-[11px] text-warn">{t("w.need_2fa")}</p>
          )}
          <div className="flex flex-wrap gap-2">
            <button type="button" disabled={busy || !pw || !to || !totp || code.length !== 6}
              className="inline-flex items-center gap-1 rounded-md bg-accent px-3 py-1.5 text-xs font-semibold text-carbon disabled:opacity-40"
              onClick={async () => {
                const sol = amount ? Number(amount) : null;
                if (!confirm(t("w.withdraw_q", { a: sol ?? t("w.all"), to }))) return;
                setBusy(true);
                setMsg(null);
                try {
                  const r = await api.withdraw(pw, to.trim(), sol, code);
                  setMsg({ ok: true, text: t("w.sent", { a: r.sol.toFixed(4), s: r.signature.slice(0, 12) }) });
                  setPw("");
                  setCode("");
                  onDone();
                } catch (e) {
                  setMsg({ ok: false, text: errText(e, srv, t("w.withdraw_fail")) });
                } finally {
                  setBusy(false);
                }
              }}>
              <ArrowUpRight className="size-3.5" /> {t("w.withdraw")}
            </button>
            {!a.key_revealed && (
              <button type="button" disabled={busy || !pw || (totp && code.length !== 6)}
                className="inline-flex items-center gap-1 rounded-md border border-line px-3 py-1.5 text-xs hover:bg-raised disabled:opacity-40"
                onClick={async () => {
                  setMsg(null);
                  try {
                    const r = await api.revealKey(pw, totp ? code : null);
                    setCode("");
                    setSecret(r.secret);
                    setPw("");
                    onDone();
                  } catch (e) {
                    setMsg({ ok: false, text: errText(e, srv, t("w.key_fail")) });
                  }
                }}>
                <KeyRound className="size-3.5" /> {t("w.show_key")}
              </button>
            )}
          </div>
          {msg && <span className={`text-xs ${msg.ok ? "text-bull" : "text-bear"}`}>{msg.text}</span>}
        </div>
      </div>
    </section>
  );
}

function Page() {
  const { t, srv } = useT();
  const [data, setData] = useState<Me | null>(null);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setData(await api.me(signal));
      setError("");
    } catch (e) {
      if ((e as Error).name === "AbortError") return;
      if (e instanceof ApiError && (e.status === 401 || e.status === 403)) {
        window.location.href = "/login";
        return;
      }
      setError(e instanceof ApiError ? e.message : "Error");
    }
  }, []);

  useEffect(() => {
    const ctl = new AbortController();
    load(ctl.signal);
    const id = setInterval(() => !document.hidden && load(ctl.signal), REFRESH_MS);
    return () => {
      ctl.abort();
      clearInterval(id);
    };
  }, [load]);

  const { lang } = useT();
  const serverLang = data?.user.lang;
  useEffect(() => {
    // keep the server (Telegram notes) on the language this browser uses
    if (serverLang && serverLang !== lang) api.setLang(lang).catch(() => undefined);
  }, [serverLang, lang]);

  const a = data?.account;
  const total = a ? a.realized_usd + (a.unrealized_usd ?? 0) : null;

  return (
    <main className="mx-auto max-w-[1100px] px-4 pt-5 pb-12 sm:px-6">
      <header className="mb-6 flex flex-wrap items-center gap-3 border-b border-line pb-4">
        <SirbLockup size={26} href="/" sub={false} />
        <span className="h-5 w-px bg-line" />
        <h1 className="text-lg font-semibold tracking-tight">{t("m.title")}</h1>
        {data && <span className="text-xs text-muted" dir="ltr">{data.user.email}</span>}
        <span className="ms-auto flex gap-2">
          <LangSwitch />
          <button type="button"
            className="inline-flex items-center gap-1 rounded-md border border-line px-2 py-1 text-xs text-muted hover:bg-raised hover:text-ink"
            onClick={async () => {
              await api.logout().catch(() => undefined);
              window.location.href = "/login";
            }}>
            <LogOut className="size-3.5" /> {t("m.signout")}
          </button>
        </span>
      </header>
      {error && <p className="mb-4 text-sm text-bear">{srv(error)}</p>}
      {!data && !error && <p className="text-sm text-muted">{t("c.loading")}</p>}

      {data && data.user.status === "pending" && (
        <section className="card flex items-start gap-3 p-5">
          <Clock className="mt-0.5 size-5 text-warn" />
          <div>
            <h2 className="text-sm font-semibold">{t("m.pending_title")}</h2>
            <p className="mt-1 text-sm text-muted">{t("m.pending")}</p>
          </div>
        </section>
      )}

      {a && (
        <div className="flex flex-col gap-4">
          {note && <p className="text-xs text-muted">{note}</p>}
          <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Stat label={t("m.pnl_now")} value={money(total, true)} cls={tone(total)}
              sub={t("m.pnl_sub", { r: money(a.realized_usd, true), o: money(a.unrealized_usd, true) })} />
            <Stat label={t("m.last24")} value={money(a.realized_24h, true)} cls={tone(a.realized_24h)} sub={t("m.realized")} />
            <Stat label={t("m.in_open")} value={money(a.open_cost_usd)}
              sub={t("m.in_open_sub", { n: a.open.length, b: money(a.settings.max_open_usd) })} />
            <Stat label={t("m.wallet")} value={a.sol_balance != null ? `${a.sol_balance.toFixed(3)} SOL` : "—"}
              sub={a.sol_balance_usd != null ? money(a.sol_balance_usd) : undefined} />
          </section>
          {a.billing?.hold && (
            <p className="flex items-start gap-2 rounded-md border border-bear/40 bg-bear-soft p-3 text-sm text-bear">
              <AlertTriangle className="mt-0.5 size-4 shrink-0" /> {t("b.hold")}
            </p>
          )}
          {a.stats && <PerformanceCard s={a.stats} />}
          {a.billing && <BillingCard b={a.billing} onChange={() => load()} />}
          <section className="card">
            <h2 className="border-b border-line px-4 py-3 text-sm font-semibold">{t("m.open_trades")}</h2>
            <Positions rows={a.open} live onSell={async (p, sh) => {
              if (!confirm(t("m.sell_q", { p: sh * 100, s: p.symbol }))) return;
              try {
                const r = await api.mySell(p.id, sh);
                setNote(srv(r.message));
                load();
              } catch (e) {
                setNote(errText(e, srv, t("m.sell_fail")));
              }
            }} />
          </section>
          <SettingsCard a={a} onSaved={() => load()} />
          <WalletCard a={a} onDone={() => load()} totp={data!.security.totp_enabled} />
          <TelegramCard me={data!} onDone={() => load()} />
          <SecurityCard me={data!} onDone={() => load()} />
          <section className="card">
            <h2 className="border-b border-line px-4 py-3 text-sm font-semibold">{t("m.closed_trades")}</h2>
            <Positions rows={a.closed} live={false} />
          </section>
        </div>
      )}
    </main>
  );
}

export function MeView() {
  // the language also goes to the server, so Telegram notes match the page
  return (
    <I18nProvider onChange={(l) => api.setLang(l).catch(() => undefined)}>
      <Page />
    </I18nProvider>
  );
}
