"use client";

import {
  ArrowUpRight,
  Clock,
  Copy,
  ExternalLink,
  KeyRound,
  LogOut,
  Pause,
  Play,
  BarChart3,
  Save,
  Send,
  ShieldCheck,
  Wallet,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { api, ApiError } from "@/lib/api";
import type { Me, MyAccount, MyPosition, MyStats } from "@/lib/types";

function DailyBars({ days }: { days: MyStats["daily"] }) {
  const max = Math.max(1, ...days.map((d) => Math.abs(d.pnl)));
  const w = 100 / days.length;
  return (
    <svg viewBox="0 0 100 40" preserveAspectRatio="none" className="h-28 w-full" role="img"
      aria-label="Profit and loss per day, last 30 days">
      <line x1="0" y1="20" x2="100" y2="20" stroke="currentColor" strokeOpacity="0.2" strokeWidth="0.2" />
      {days.map((d, i) => {
        const h = (Math.abs(d.pnl) / max) * 19;
        return (
          <rect key={d.day} x={i * w + w * 0.15} width={w * 0.7} y={d.pnl >= 0 ? 20 - h : 20} height={Math.max(h, d.trades ? 0.4 : 0)}
            className={d.pnl >= 0 ? "fill-bull" : "fill-bear"}>
            <title>{`${d.day}: ${d.pnl >= 0 ? "+" : "−"}$${Math.abs(d.pnl).toFixed(2)} · ${d.trades} trades`}</title>
          </rect>
        );
      })}
    </svg>
  );
}

function PerformanceCard({ s }: { s: MyStats }) {
  const tile = (label: string, value: string, cls = "") => (
    <div className="rounded-md border border-line px-3 py-2">
      <div className="text-[10px] tracking-[0.12em] text-muted uppercase">{label}</div>
      <div className={`num mt-1 text-base font-semibold ${cls}`}>{value}</div>
    </div>
  );
  const sign = (v: number | null | undefined) => (v == null ? "—" : `${v >= 0 ? "+" : "−"}$${Math.abs(v).toFixed(2)}`);
  return (
    <section className="card p-4 sm:p-5">
      <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold"><BarChart3 className="size-4 text-accent" /> Performance</h2>
      {s.trades === 0 ? (
        <p className="text-sm text-faint">No closed trades yet. Results show here after the first one closes.</p>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-6">
            {tile("Total result", sign(s.total_pnl), s.total_pnl >= 0 ? "text-bull" : "text-bear")}
            {tile("Return on trades", s.roi_pct == null ? "—" : `${s.roi_pct >= 0 ? "+" : ""}${s.roi_pct.toFixed(1)}%`)}
            {tile("Win rate", s.win_rate == null ? "—" : `${s.win_rate.toFixed(0)}% of ${s.trades}`)}
            {tile("Average win", sign(s.avg_win), "text-bull")}
            {tile("Average loss", sign(s.avg_loss), "text-bear")}
            {tile("Best / worst", `${s.best ? `$${s.best.symbol}` : "—"} / ${s.worst ? `$${s.worst.symbol}` : "—"}`)}
          </div>
          <div className="mt-4">
            <div className="mb-1 flex justify-between text-[11px] text-muted">
              <span>Result per day, last 30 days</span>
              <span>{s.daily[0]?.day} → {s.daily[s.daily.length - 1]?.day}</span>
            </div>
            <DailyBars days={s.daily} />
          </div>
        </>
      )}
    </section>
  );
}

function TelegramCard({ me, onDone }: { me: Me; onDone: () => void }) {
  const [msg, setMsg] = useState("");
  const linked = me.telegram.linked;
  return (
    <section className="card p-4 sm:p-5">
      <h2 className="mb-2 flex items-center gap-2 text-sm font-semibold">
        <Send className="size-4 text-accent" /> Telegram notifications
        <span className={`rounded-full border px-2 py-0.5 text-[10px] font-bold tracking-[0.1em] ${linked ? "border-bull/50 text-bull" : "border-line text-muted"}`}>
          {linked ? "CONNECTED" : "OFF"}
        </span>
      </h2>
      <p className="mb-3 text-xs text-muted">
        Get a message when a trade opens or closes, and a summary every day.
      </p>
      <div className="flex flex-wrap items-center gap-3">
        {!linked ? (
          <button type="button" className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-xs font-semibold text-carbon"
            onClick={async () => {
              setMsg("");
              try {
                const r = await api.telegramLink();
                window.open(r.url, "_blank", "noopener");
                setMsg("Telegram opened: press Start in the chat with the bot, then come back here.");
              } catch (e) {
                setMsg(e instanceof ApiError ? e.message : "Could not make a link");
              }
            }}>
            <Send className="size-3.5" /> Connect Telegram
          </button>
        ) : (
          <>
            <label className="flex items-center gap-2 text-xs">
              <input type="checkbox" checked={me.telegram.daily}
                onChange={async (e) => { await api.telegramDaily(e.target.checked).catch(() => undefined); onDone(); }} />
              Daily summary
            </label>
            <button type="button" className="rounded-md border border-line px-3 py-1.5 text-xs hover:bg-raised"
              onClick={async () => { await api.telegramUnlink().catch(() => undefined); onDone(); }}>
              Disconnect
            </button>
          </>
        )}
        {msg && <span className="text-xs text-muted">{msg}</span>}
      </div>
    </section>
  );
}

const codeInput =
  "num w-full rounded-md border border-line bg-carbon px-2.5 py-1.5 text-sm tracking-[0.3em] outline-none focus:border-accent";

function SecurityCard({ me, onDone }: { me: Me; onDone: () => void }) {
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
      setMsg({ ok: false, text: e instanceof ApiError ? e.message : "Failed" });
    }
  }

  return (
    <section className="card p-4 sm:p-5">
      <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold">
        <ShieldCheck className="size-4 text-accent" /> Security
        <span className={`rounded-full border px-2 py-0.5 text-[10px] font-bold tracking-[0.1em] ${on ? "border-bull/50 text-bull" : "border-warn/60 text-warn"}`}>
          {on ? "2-STEP ON" : "2-STEP OFF"}
        </span>
      </h2>
      {me.security.withdraw_lock_hours > 0 && (
        <p className="mb-3 rounded-md border border-warn/50 px-3 py-2 text-xs text-warn">
          Withdrawals are locked for {Math.ceil(me.security.withdraw_lock_hours)} more hours after a password or
          two-step reset. Trading keeps working.
        </p>
      )}
      <div className="grid gap-4 lg:grid-cols-2">
        <div className="flex flex-col gap-2">
          <span className="text-xs font-semibold">Two-step codes (needed to withdraw)</span>
          {!on && !setup && (
            <>
              <p className="text-xs text-muted">
                Use an authenticator app (Google Authenticator, Authy, 1Password…). Every withdrawal will ask for the
                6-digit code it shows, so a stolen password alone can&apos;t move your money.
              </p>
              <input className={input} type="password" placeholder="Your password" autoComplete="current-password"
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
                    setMsg({ ok: false, text: e instanceof ApiError ? e.message : "Failed" });
                  }
                }}>
                Set up two-step codes
              </button>
            </>
          )}
          {!on && setup && (
            <>
              <p className="text-xs text-muted">1. Scan this with your authenticator app (or type the key).</p>
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={setup.qr} alt="QR code for your authenticator app" className="size-44 rounded-md bg-white p-1" />
              <code className="num text-xs break-all text-muted">{setup.secret}</code>
              <p className="text-xs text-muted">2. Enter the 6-digit code it shows now.</p>
              <input className={codeInput} inputMode="numeric" maxLength={6} placeholder="123456" value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))} />
              <button type="button" disabled={code.length !== 6}
                className="self-start rounded-md bg-accent px-3 py-1.5 text-xs font-semibold text-carbon disabled:opacity-40"
                onClick={() => run(async () => { await api.totpEnable(code); setSetup(null); }, "Two-step codes are on")}>
                Turn on
              </button>
            </>
          )}
          {on && (
            <>
              <p className="text-xs text-muted">On. Lost your phone? Ask the admin to reset it (withdrawals then wait 24 h).</p>
              <details className="text-xs">
                <summary className="cursor-pointer text-muted hover:text-ink">Turn off</summary>
                <div className="mt-2 flex flex-col gap-2">
                  <input className={input} type="password" placeholder="Your password" value={pw}
                    autoComplete="current-password" onChange={(e) => setPw(e.target.value)} />
                  <input className={codeInput} inputMode="numeric" maxLength={6} placeholder="Code" value={code}
                    onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))} />
                  <button type="button" disabled={!pw || code.length !== 6}
                    className="self-start rounded-md border border-bear/60 px-3 py-1.5 text-xs text-bear disabled:opacity-40"
                    onClick={() => confirm("Turn off two-step codes? Withdrawals will be locked for 24 hours.") &&
                      run(() => api.totpDisable(pw, code), "Two-step codes are off")}>
                    Turn off two-step codes
                  </button>
                </div>
              </details>
            </>
          )}
        </div>
        <div className="flex flex-col gap-2">
          <span className="text-xs font-semibold">Change password</span>
          <input className={input} type="password" placeholder="Current password" autoComplete="current-password"
            value={cur} onChange={(e) => setCur(e.target.value)} />
          <input className={input} type="password" placeholder="New password (10+ characters)" autoComplete="new-password"
            value={next} onChange={(e) => setNext(e.target.value)} />
          <button type="button" disabled={!cur || next.length < 10}
            className="self-start rounded-md border border-line px-3 py-1.5 text-xs hover:bg-raised disabled:opacity-40"
            onClick={() => run(async () => { await api.changePassword(cur, next); setCur(""); setNext(""); },
              "Password changed; other devices are signed out")}>
            Change password
          </button>
        </div>
      </div>
      {msg && <p className={`mt-3 text-xs ${msg.ok ? "text-bull" : "text-bear"}`}>{msg.text}</p>}
    </section>
  );
}

const REFRESH_MS = 5_000;
const input =
  "w-full rounded-md border border-line bg-carbon px-2.5 py-1.5 text-sm outline-none focus:border-accent";

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
  if (!rows.length) return <p className="px-4 py-6 text-sm text-faint">{live ? "No open trades." : "No closed trades yet."}</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[560px] text-sm">
        <thead className="text-[11px] text-muted uppercase">
          <tr className="border-b border-line">
            <th className="px-4 py-2 text-left">Token</th>
            <th className="px-4 py-2 text-right">Cost</th>
            <th className="px-4 py-2 text-right">{live ? "Value now" : "Sold for"}</th>
            <th className="px-4 py-2 text-right">P&amp;L</th>
            <th className="px-4 py-2 text-left">{live ? "Sell" : "Exit"}</th>
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
                    href={`https://solscan.io/token/${p.token_address}`}>
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
                      <span className="text-xs text-warn">selling {p.manual_sell_frac * 100}%…</span>
                    ) : (
                      <span className="flex gap-1">
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
                    <span className="text-xs text-muted">{p.exit_reason ?? ""}</span>
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
  const st = a.settings;
  const [size, setSize] = useState(String(st.size_usd));
  const [open, setOpen] = useState(String(st.max_open_usd));
  const [loss, setLoss] = useState(String(st.max_daily_loss_usd));
  const [takeOn, setTakeOn] = useState(st.take_capital_at_pct != null);
  const [take, setTake] = useState(String(st.take_capital_at_pct ?? 100));
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const cap = (v: number | null) => (v != null ? `max ${money(v)}` : undefined);

  async function save(body: Record<string, unknown>, ok: string) {
    setMsg(null);
    try {
      await api.mySettings(body);
      setMsg({ ok: true, text: ok });
      onSaved();
    } catch (e) {
      setMsg({ ok: false, text: e instanceof ApiError ? e.message : "Could not save" });
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
        <h2 className="text-sm font-semibold">Trading settings</h2>
        <span className={`rounded-full border px-2 py-0.5 text-[10px] font-bold tracking-[0.1em] ${st.paused ? "border-warn/60 text-warn" : "border-bull/50 text-bull"}`}>
          {st.paused ? "PAUSED" : "ACTIVE"}
        </span>
        <button type="button"
          className="ml-auto inline-flex items-center gap-1 rounded-md border border-line px-2.5 py-1 text-xs hover:bg-raised"
          onClick={() => save({ paused: !st.paused }, st.paused ? "Resumed" : "Paused: no new trades, open ones still close")}>
          {st.paused ? <Play className="size-3.5" /> : <Pause className="size-3.5" />}
          {st.paused ? "Resume" : "Pause"}
        </button>
      </header>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {field("Entry per trade", size, setSize, cap(st.caps.size_usd) ?? "USD per trade")}
        {field("Trading budget", open, setOpen, cap(st.caps.max_open_usd) ?? "max USD in open trades")}
        {field("Max loss / 24h", loss, setLoss, cap(st.caps.max_daily_loss_usd) ?? "new trades stop after this")}
        <label className="flex flex-col gap-1">
          <span className="text-[11px] font-medium tracking-[0.12em] text-muted uppercase">Take capital at</span>
          <span className="flex items-center gap-2">
            <input type="checkbox" checked={takeOn} onChange={(e) => setTakeOn(e.target.checked)} />
            <input className={`${input} num`} inputMode="decimal" disabled={!takeOn} value={take}
              onChange={(e) => setTake(e.target.value)} />
            <span className="text-sm text-muted">%</span>
          </span>
          <span className="text-[11px] text-faint">get the cost back at this gain, the rest rides</span>
        </label>
      </div>
      <footer className="mt-4 flex flex-wrap items-center gap-3">
        <button type="button"
          className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-carbon"
          onClick={() => save({
            size_usd: Number(size), max_open_usd: Number(open), max_daily_loss_usd: Number(loss),
            ...(takeOn ? { take_capital_at_pct: Number(take) } : { take_capital_off: true }),
          }, "Saved")}>
          <Save className="size-4" /> Save
        </button>
        {msg && <span className={`text-xs ${msg.ok ? "text-bull" : "text-bear"}`}>{msg.text}</span>}
      </footer>
    </section>
  );
}

function WalletCard({ a, onDone, totp }: { a: MyAccount; onDone: () => void; totp: boolean }) {
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
      <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold"><Wallet className="size-4 text-accent" /> Your trading wallet</h2>
      {a.pubkey && (
        <div className="mb-3">
          <div className="text-[11px] font-medium tracking-[0.12em] text-muted uppercase">Deposit address (Solana, send SOL)</div>
          <div className="num mt-1 flex flex-wrap items-center gap-2 text-sm break-all">
            {a.pubkey}
            <button type="button" className="inline-flex items-center gap-1 text-xs text-muted hover:text-ink"
              onClick={() => copy(a.pubkey!, "address")}>
              <Copy className="size-3" /> {copied === "address" ? "copied" : "copy"}
            </button>
            <a className="inline-flex items-center gap-1 text-xs text-muted hover:underline" target="_blank" rel="noreferrer"
              href={`https://solscan.io/account/${a.pubkey}`}>Solscan <ExternalLink className="size-3" /></a>
          </div>
          <div className="num mt-1 text-xs text-muted">
            Balance {a.sol_balance != null ? `${a.sol_balance.toFixed(4)} SOL` : "—"} {a.sol_balance_usd != null ? `(${money(a.sol_balance_usd)})` : ""}
          </div>
        </div>
      )}

      {secret ? (
        <div className="mb-3 rounded-md border border-warn/60 p-3">
          <p className="mb-2 text-xs text-warn">
            Your private key. Shown only this once: save it now and import it in Phantom to see your tokens. Anyone with it
            controls the wallet.
          </p>
          <div className="num rounded-md border border-line bg-carbon px-3 py-2 text-xs break-all">{secret}</div>
          <div className="mt-2 flex gap-2">
            <button type="button" className="inline-flex items-center gap-1 rounded-md border border-line px-3 py-1 text-xs hover:bg-raised"
              onClick={() => copy(secret, "key")}><Copy className="size-3.5" /> {copied === "key" ? "Copied" : "Copy"}</button>
            <button type="button" className="rounded-md bg-accent px-3 py-1 text-xs font-semibold text-carbon"
              onClick={() => confirm("Did you save it? It can't be shown again.") && setSecret(null)}>I saved it</button>
          </div>
        </div>
      ) : null}

      <div className="grid gap-3 lg:grid-cols-2">
        <div className="flex flex-col gap-2 rounded-md border border-line p-3">
          <span className="text-xs font-semibold">Withdraw SOL</span>
          <input className={`${input} num`} placeholder="Send to (your Solana address)" value={to} onChange={(e) => setTo(e.target.value)} />
          <input className={`${input} num`} placeholder="Amount in SOL (empty = all)" inputMode="decimal" value={amount}
            onChange={(e) => setAmount(e.target.value)} />
          <p className="text-[11px] text-faint">Tokens of open trades stay in the wallet; 0.01 SOL stays for their fees.</p>
        </div>
        <div className="flex flex-col gap-2 rounded-md border border-line p-3">
          <span className="text-xs font-semibold">Confirm with your password{totp ? " and code" : ""}</span>
          <input className={input} type="password" placeholder="Your account password" autoComplete="current-password"
            value={pw} onChange={(e) => setPw(e.target.value)} />
          {totp ? (
            <input className={codeInput} inputMode="numeric" maxLength={6} placeholder="2-step code" value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))} />
          ) : (
            <p className="text-[11px] text-warn">Withdrawals need two-step codes: turn them on in Security below.</p>
          )}
          <div className="flex flex-wrap gap-2">
            <button type="button" disabled={busy || !pw || !to || !totp || code.length !== 6}
              className="inline-flex items-center gap-1 rounded-md bg-accent px-3 py-1.5 text-xs font-semibold text-carbon disabled:opacity-40"
              onClick={async () => {
                const sol = amount ? Number(amount) : null;
                if (!confirm(`Send ${sol ?? "all available"} SOL to ${to}? This can't be undone.`)) return;
                setBusy(true);
                setMsg(null);
                try {
                  const r = await api.withdraw(pw, to.trim(), sol, code);
                  setMsg({ ok: true, text: `Sent ${r.sol.toFixed(4)} SOL · ${r.signature.slice(0, 12)}…` });
                  setPw("");
                  setCode("");
                  onDone();
                } catch (e) {
                  setMsg({ ok: false, text: e instanceof ApiError ? e.message : "Withdrawal failed" });
                } finally {
                  setBusy(false);
                }
              }}>
              <ArrowUpRight className="size-3.5" /> Withdraw
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
                    setMsg({ ok: false, text: e instanceof ApiError ? e.message : "Could not show the key" });
                  }
                }}>
                <KeyRound className="size-3.5" /> Show private key (once)
              </button>
            )}
          </div>
          {msg && <span className={`text-xs ${msg.ok ? "text-bull" : "text-bear"}`}>{msg.text}</span>}
        </div>
      </div>
    </section>
  );
}

export function MeView() {
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
    const t = setInterval(() => !document.hidden && load(ctl.signal), REFRESH_MS);
    return () => {
      ctl.abort();
      clearInterval(t);
    };
  }, [load]);

  const a = data?.account;
  const total = a ? a.realized_usd + (a.unrealized_usd ?? 0) : null;

  return (
    <main className="mx-auto max-w-[1100px] px-4 pt-5 pb-12 sm:px-6">
      <header className="mb-5 flex flex-wrap items-center gap-3">
        <Wallet className="size-5 text-accent" />
        <h1 className="text-lg font-semibold tracking-tight">My trading</h1>
        {data && <span className="text-xs text-muted">{data.user.email}</span>}
        <button type="button"
          className="ml-auto inline-flex items-center gap-1 rounded-md border border-line px-2 py-1 text-xs text-muted hover:bg-raised hover:text-ink"
          onClick={async () => {
            await api.logout().catch(() => undefined);
            window.location.href = "/login";
          }}>
          <LogOut className="size-3.5" /> Sign out
        </button>
      </header>
      {error && <p className="mb-4 text-sm text-bear">{error}</p>}
      {!data && !error && <p className="text-sm text-muted">Loading…</p>}

      {data && data.user.status === "pending" && (
        <section className="card flex items-start gap-3 p-5">
          <Clock className="mt-0.5 size-5 text-warn" />
          <div>
            <h2 className="text-sm font-semibold">Waiting for approval</h2>
            <p className="mt-1 text-sm text-muted">
              Your account is registered. Once the admin approves it, your trading wallet appears here with its deposit
              address.
            </p>
          </div>
        </section>
      )}

      {a && (
        <div className="flex flex-col gap-4">
          {note && <p className="text-xs text-muted">{note}</p>}
          <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Stat label="P&L now" value={money(total, true)} cls={tone(total)}
              sub={`realized ${money(a.realized_usd, true)} · open ${money(a.unrealized_usd, true)}`} />
            <Stat label="Last 24h" value={money(a.realized_24h, true)} cls={tone(a.realized_24h)} sub="realized" />
            <Stat label="In open trades" value={money(a.open_cost_usd)}
              sub={`${a.open.length} open · budget ${money(a.settings.max_open_usd)}`} />
            <Stat label="Wallet" value={a.sol_balance != null ? `${a.sol_balance.toFixed(3)} SOL` : "—"}
              sub={a.sol_balance_usd != null ? money(a.sol_balance_usd) : undefined} />
          </section>
          {a.stats && <PerformanceCard s={a.stats} />}
          <section className="card">
            <h2 className="border-b border-line px-4 py-3 text-sm font-semibold">Open trades</h2>
            <Positions rows={a.open} live onSell={async (p, sh) => {
              if (!confirm(`Sell ${sh * 100}% of $${p.symbol}?`)) return;
              try {
                const r = await api.mySell(p.id, sh);
                setNote(r.message);
                load();
              } catch (e) {
                setNote(e instanceof ApiError ? e.message : "Could not sell");
              }
            }} />
          </section>
          <SettingsCard a={a} onSaved={() => load()} />
          <WalletCard a={a} onDone={() => load()} totp={data!.security.totp_enabled} />
          <TelegramCard me={data!} onDone={() => load()} />
          <SecurityCard me={data!} onDone={() => load()} />
          <section className="card">
            <h2 className="border-b border-line px-4 py-3 text-sm font-semibold">Closed trades</h2>
            <Positions rows={a.closed} live={false} />
          </section>
        </div>
      )}
    </main>
  );
}
