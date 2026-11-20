"use client";

import {
  ArrowLeft,
  Copy,
  ExternalLink,
  KeyRound,
  Lock,
  Pause,
  Play,
  Plus,
  Power,
  Save,
  ScrollText,
  Trash2,
  Users,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { AdminSignIn, adminSignOut, useAdminResume } from "@/components/AdminAuth";
import { AppNav } from "@/components/AppNav";
import { Badge, button, PageHeader } from "@/components/ui";
import { api, ApiError, SESSION } from "@/lib/api";
import type { AccountsList, AuditEntry, LeaderCfg, SiteUser, TradingAccountCfg } from "@/lib/types";

const KEY = "alpha-live-password"; // same password as the Live page

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

function short(a: string) {
  return `${a.slice(0, 4)}…${a.slice(-4)}`;
}

/** Form state: numbers kept as strings so fields can be empty while typing. */
interface Draft {
  name: string;
  size_usd: string;
  max_open_usd: string;
  max_daily_loss_usd: string;
  take_on: boolean;
  take_capital_at_pct: string;
  max_hold_hours: string;
  leaders: LeaderCfg[];
  cap_size_usd?: string;
  cap_open_usd?: string;
  cap_loss_usd?: string;
}

function toDraft(a: TradingAccountCfg): Draft {
  return {
    name: a.name,
    size_usd: String(a.size_usd),
    max_open_usd: String(a.max_open_usd),
    max_daily_loss_usd: String(a.max_daily_loss_usd),
    take_on: a.take_capital_at_pct != null,
    take_capital_at_pct: String(a.take_capital_at_pct ?? 100),
    max_hold_hours: a.max_hold_hours != null ? String(a.max_hold_hours) : "",
    leaders: a.leaders.map((l) => ({ ...l })),
    ...(a.user_id != null
      ? {
          cap_size_usd: a.cap_size_usd != null ? String(a.cap_size_usd) : "",
          cap_open_usd: a.cap_open_usd != null ? String(a.cap_open_usd) : "",
          cap_loss_usd: a.cap_loss_usd != null ? String(a.cap_loss_usd) : "",
        }
      : {}),
  };
}

function Caps({ d, set }: { d: Draft; set: (d: Draft) => void }) {
  const f = (label: string, k: "cap_size_usd" | "cap_open_usd" | "cap_loss_usd") => (
    <Field label={label} hint="empty = no limit">
      <input className={`${input} num`} inputMode="decimal" value={d[k] ?? ""}
        onChange={(e) => set({ ...d, [k]: e.target.value })} />
    </Field>
  );
  return (
    <div className="mt-4">
      <div className="mb-2 text-[11px] font-medium tracking-[0.12em] text-muted uppercase">
        User limits (the most the user may set)
      </div>
      <div className="grid gap-3 sm:grid-cols-3">
        {f("Max entry", "cap_size_usd")}
        {f("Max budget", "cap_open_usd")}
        {f("Max loss / 24h", "cap_loss_usd")}
      </div>
    </div>
  );
}

function capsPayload(d: Draft) {
  if (d.cap_size_usd === undefined) return {};
  const v = (x?: string) => (x ? num(x) : -1); // -1 clears a cap
  return { cap_size_usd: v(d.cap_size_usd), cap_open_usd: v(d.cap_open_usd), cap_loss_usd: v(d.cap_loss_usd) };
}

const EMPTY: Draft = {
  name: "",
  size_usd: "20",
  max_open_usd: "200",
  max_daily_loss_usd: "100",
  take_on: true,
  take_capital_at_pct: "100",
  max_hold_hours: "",
  leaders: [],
};

function num(v: string): number {
  return Number(v.replace(/,/g, ""));
}

function payload(d: Draft) {
  return {
    size_usd: num(d.size_usd),
    max_open_usd: num(d.max_open_usd),
    max_daily_loss_usd: num(d.max_daily_loss_usd),
    leaders: d.leaders.map((l) => ({
      ...l,
      wallet: l.wallet.trim(),
      size_usd: l.size_usd ? Number(l.size_usd) : null,
    })),
  };
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[11px] font-medium tracking-[0.12em] text-muted uppercase">{label}</span>
      {children}
      {hint && <span className="text-[11px] text-faint">{hint}</span>}
    </label>
  );
}

function Settings({
  d,
  set,
  defaultHold,
}: {
  d: Draft;
  set: (d: Draft) => void;
  defaultHold: number;
}) {
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
      <Field label="Entry per trade" hint="USD per copied buy">
        <input className={`${input} num`} inputMode="decimal" value={d.size_usd}
          onChange={(e) => set({ ...d, size_usd: e.target.value })} />
      </Field>
      <Field label="Trading budget" hint="max USD open at once">
        <input className={`${input} num`} inputMode="decimal" value={d.max_open_usd}
          onChange={(e) => set({ ...d, max_open_usd: e.target.value })} />
      </Field>
      <Field label="Max loss / 24h" hint="new buys stop after this">
        <input className={`${input} num`} inputMode="decimal" value={d.max_daily_loss_usd}
          onChange={(e) => set({ ...d, max_daily_loss_usd: e.target.value })} />
      </Field>
      <Field label="Take capital at" hint={d.take_on ? "% gain: sell the cost back, rest rides" : "off"}>
        <div className="flex items-center gap-2">
          <input type="checkbox" checked={d.take_on}
            onChange={(e) => set({ ...d, take_on: e.target.checked })} />
          <input className={`${input} num`} inputMode="decimal" disabled={!d.take_on}
            value={d.take_capital_at_pct}
            onChange={(e) => set({ ...d, take_capital_at_pct: e.target.value })} />
          <span className="text-sm text-muted">%</span>
        </div>
      </Field>
      <Field label="Max hold" hint={`hours; empty = ${defaultHold}`}>
        <input className={`${input} num`} inputMode="numeric" placeholder={String(defaultHold)}
          value={d.max_hold_hours} onChange={(e) => set({ ...d, max_hold_hours: e.target.value })} />
      </Field>
    </div>
  );
}

/** On/off switch. */
function Switch({ on, busy, onClick, label }: { on: boolean; busy?: boolean; onClick: () => void; label: string }) {
  return (
    <button type="button" role="switch" aria-checked={on} aria-label={label} disabled={busy} onClick={onClick}
      className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition disabled:opacity-50 ${
        on ? "bg-mint" : "bg-raised ring-1 ring-line-strong"}`}>
      <span className={`inline-block size-4 rounded-full bg-ink shadow transition ${on ? "translate-x-[18px]" : "translate-x-0.5"}`} />
    </button>
  );
}

type Toggle = (wallet: string, enabled: boolean, close: boolean) => Promise<void>;

/** Follow switch for a saved account: turning it off asks what to do with that wallet's open positions. */
function FollowCell({ l, open, onToggle }: { l: LeaderCfg; open: number; onToggle?: Toggle }) {
  const [ask, setAsk] = useState(false);
  const [busy, setBusy] = useState(false);
  const on = l.enabled !== false;
  const run = async (enabled: boolean, close: boolean) => {
    if (!onToggle) return;
    setBusy(true);
    try {
      await onToggle(l.wallet, enabled, close);
    } finally {
      setBusy(false);
      setAsk(false);
    }
  };
  if (ask) {
    return (
      <span className="flex items-center gap-1.5">
        <button type="button" disabled={busy} onClick={() => run(false, false)}
          className="rounded-md border border-line bg-raised px-2 py-0.5 text-xs hover:border-line-strong">
          Stop buys only
        </button>
        <button type="button" disabled={busy} onClick={() => run(false, true)}
          className="rounded-md border border-bear/50 px-2 py-0.5 text-xs text-bear hover:bg-bear-soft">
          Stop + sell {open} open
        </button>
        <button type="button" onClick={() => setAsk(false)} className="px-1 text-xs text-muted hover:text-ink">Cancel</button>
      </span>
    );
  }
  return (
    <span className="flex items-center gap-2">
      <Switch on={on} busy={busy} label={on ? "Stop following" : "Follow"}
        onClick={() => (on ? (open > 0 ? setAsk(true) : run(false, false)) : run(true, false))} />
      <span className={`text-xs ${on ? "text-mint" : "text-muted"}`}>{on ? "Following" : "Off"}</span>
      {open > 0 && <span className="text-xs text-faint">· {open} open</span>}
    </span>
  );
}

function Leaders({ d, set, openBy, onToggle }: {
  d: Draft;
  set: (d: Draft) => void;
  openBy?: Record<string, number>;
  onToggle?: Toggle;           // saved accounts: the switch applies right away
}) {
  const [add, setAdd] = useState("");
  const upd = (i: number, patch: Partial<LeaderCfg>) =>
    set({ ...d, leaders: d.leaders.map((l, j) => (j === i ? { ...l, ...patch } : l)) });
  return (
    <div className="mt-4">
      <div className="mb-2 text-[11px] font-medium tracking-[0.12em] text-muted uppercase">
        Smart wallets to copy ({d.leaders.length})
      </div>
      <div className="overflow-x-auto rounded-xl border border-line">
        <table className="data-table min-w-[640px] text-sm">
          <thead>
            <tr>
              <th className="text-left">Follow</th>
              <th className="text-left">Wallet</th>
              <th className="text-left">Label</th>
              <th className="text-left">Entry</th>
              <th className="text-center" title="Copy with no liquidity, chase or scanner checks">
                No checks
              </th>
              <th className="text-center" title="Only tokens it did not hold before">New tokens only</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {d.leaders.map((l, i) => (
              <tr key={l.wallet} className={l.enabled === false ? "opacity-60" : ""}>
                <td>
                  {onToggle ? (
                    <FollowCell l={l} open={openBy?.[l.wallet] ?? 0} onToggle={onToggle} />
                  ) : (
                    <Switch on={l.enabled !== false} label="Follow"
                      onClick={() => upd(i, { enabled: l.enabled === false })} />
                  )}
                </td>
                <td className="num ">
                  <a className="inline-flex items-center gap-1 hover:underline" target="_blank" rel="noreferrer"
                    href={`https://solscan.io/account/${l.wallet}`}>
                    {short(l.wallet)} <ExternalLink className="size-3" />
                  </a>
                </td>
                <td >
                  <input className={input} value={l.label} maxLength={40}
                    onChange={(e) => upd(i, { label: e.target.value })} />
                </td>
                <td >
                  <input className={`${input} num w-24`} inputMode="decimal" placeholder="account"
                    value={l.size_usd ?? ""}
                    onChange={(e) => upd(i, { size_usd: e.target.value ? Number(e.target.value) : null })} />
                </td>
                <td className="text-center">
                  <input type="checkbox" checked={l.unconditional}
                    onChange={(e) => upd(i, { unconditional: e.target.checked })} />
                </td>
                <td className="text-center">
                  <input type="checkbox" checked={l.new_tokens_only}
                    onChange={(e) => upd(i, { new_tokens_only: e.target.checked })} />
                </td>
                <td className="text-right">
                  <button type="button" aria-label="Remove" className="text-muted hover:text-bear"
                    onClick={() => set({ ...d, leaders: d.leaders.filter((_, j) => j !== i) })}>
                    <Trash2 className="size-4" />
                  </button>
                </td>
              </tr>
            ))}
            {d.leaders.length === 0 && (
              <tr>
                <td colSpan={7} className="text-xs text-faint">
                  No smart wallets yet: this account will not buy anything.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="mt-2 flex gap-2">
        <input className={`${input} num`} placeholder="Smart wallet address to copy" value={add}
          onChange={(e) => setAdd(e.target.value)} />
        <button type="button"
          className={button("secondary", "shrink-0")}
          onClick={() => {
            const w = add.trim();
            if (!w || d.leaders.some((l) => l.wallet === w)) return;
            set({ ...d, leaders: [...d.leaders, { wallet: w, label: "", unconditional: false,
              new_tokens_only: false, size_usd: null, enabled: true }] });
            setAdd("");
          }}>
          <Plus className="size-4" /> Add
        </button>
      </div>
    </div>
  );
}

function AccountCard({
  a,
  password,
  defaultHold,
  onSaved,
  owner,
}: {
  a: TradingAccountCfg;
  password: string;
  defaultHold: number;
  onSaved: (a: TradingAccountCfg) => void;
  owner?: string;
}) {
  const [d, setD] = useState<Draft>(() => toDraft(a));
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  useEffect(() => setD(toDraft(a)), [a]);
  const dirty = JSON.stringify(d) !== JSON.stringify(toDraft(a));

  async function send(body: Record<string, unknown>, okText: string) {
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.editAccount(password, a.name, body);
      onSaved(r.account);
      setMsg({ ok: true, text: okText });
    } catch (e) {
      setMsg({ ok: false, text: e instanceof ApiError ? e.message : "Could not save" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className={`card overflow-hidden ${a.enabled ? "" : "opacity-70"}`}>
      <header className="flex flex-wrap items-center gap-2 border-b border-line px-5 py-4">
        <h2 className="num text-[15px] font-semibold">{a.name}</h2>
        <Badge tone={!a.enabled ? "muted" : a.paused ? "gold" : "mint"} dot>
          {!a.enabled ? "DISABLED" : a.paused ? "PAUSED" : "ACTIVE"}
        </Badge>
        {a.billing_hold && <Badge tone="bear">unpaid bill: buys held</Badge>}
        <Badge tone="muted">
          <KeyRound className="size-3" />
          {a.key_source === "env" ? "server key" : "stored key"}
        </Badge>
        {a.pubkey && (
          <a className="num inline-flex items-center gap-1 text-xs text-muted hover:underline" target="_blank"
            rel="noreferrer" href={`https://solscan.io/account/${a.pubkey}`}>
            {short(a.pubkey)} <ExternalLink className="size-3" />
          </a>
        )}
        <span className="text-xs text-faint">{a.open_positions} open</span>
        {owner && (
          <Badge tone="sky">user: {owner}</Badge>
        )}
        <div className="ms-auto flex gap-2">
          {a.enabled && (
            <button type="button" disabled={busy}
              className={button("secondary")}
              onClick={() => send({ paused: !a.paused }, a.paused ? "Resumed" : "Paused: no new buys, sells continue")}>
              {a.paused ? <Play className="size-3.5" /> : <Pause className="size-3.5" />}
              {a.paused ? "Resume" : "Pause"}
            </button>
          )}
          <button type="button" disabled={busy}
            className={button("secondary")}
            onClick={() => {
              if (a.enabled && !confirm(`Stop account ${a.name} completely?`)) return;
              send({ enabled: !a.enabled }, a.enabled ? "Disabled" : "Enabled");
            }}>
            <Power className="size-3.5" /> {a.enabled ? "Disable" : "Enable"}
          </button>
        </div>
      </header>
      <div className="p-5">
      <Settings d={d} set={setD} defaultHold={defaultHold} />
      <Leaders d={d} set={setD} openBy={a.open_by_leader}
        onToggle={async (wallet, enabled, close) => {
          if (dirty) {
            setMsg({ ok: false, text: "Save or discard your other changes first" });
            return;
          }
          setMsg(null);
          try {
            const r = await api.toggleLeader(password, a.name, wallet, enabled, close);
            onSaved({ ...r.account, open_by_leader: a.open_by_leader });
            setMsg({ ok: true, text: enabled ? `Following ${short(wallet)} again (within 30 s)`
              : close ? `Stopped ${short(wallet)}; selling ${r.closing} open position${r.closing === 1 ? "" : "s"} now`
                : `Stopped following ${short(wallet)}: no new buys; its open positions still follow its sells` });
          } catch (e) {
            setMsg({ ok: false, text: e instanceof ApiError ? e.message : "Could not change it" });
          }
        }} />
      {a.user_id != null && <Caps d={d} set={setD} />}
      </div>
      <footer className="flex flex-wrap items-center gap-3 border-t border-line bg-surface-2/50 px-5 py-3">
        <button type="button" disabled={busy || !dirty}
          className={button("primary")}
          onClick={() =>
            send({
              ...payload(d),
              ...capsPayload(d),
              ...(d.take_on ? { take_capital_at_pct: num(d.take_capital_at_pct) } : { take_capital_off: true }),
              ...(d.max_hold_hours ? { max_hold_hours: num(d.max_hold_hours) } : { max_hold_default: true }),
            }, "Saved: the bot picks it up within 30 s")
          }>
          <Save className="size-4" /> Save
        </button>
        {dirty && (
          <button type="button" className="text-xs text-muted hover:text-ink" onClick={() => setD(toDraft(a))}>
            Discard changes
          </button>
        )}
        {msg && <span className={`text-xs ${msg.ok ? "text-bull" : "text-bear"}`}>{msg.text}</span>}
      </footer>
    </section>
  );
}

function NewAccount({
  password,
  canStore,
  defaultHold,
  onCreated,
}: {
  password: string;
  canStore: boolean;
  defaultHold: number;
  onCreated: (a: TradingAccountCfg, secret: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const [d, setD] = useState<Draft>(EMPTY);
  const [mode, setMode] = useState<"generate" | "import">("generate");
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  if (!open) {
    return (
      <button type="button"
        className="inline-flex items-center justify-center gap-1.5 rounded-2xl border border-dashed border-line px-4 py-5 text-sm text-muted transition hover:border-mint/40 hover:bg-mint-soft/40 hover:text-mint"
        onClick={() => setOpen(true)}>
        <Plus className="size-4" /> New account
      </button>
    );
  }
  return (
    <section className="card border-mint/30 p-5">
      <h2 className="mb-4 text-[15px] font-semibold">New account</h2>
      {!canStore && (
        <p className="mb-3 rounded-md border border-warn/50 px-3 py-2 text-xs text-warn">
          The server has no LIVE_KEY_ENCRYPTION_KEY yet, so it can't keep a wallet key safely. Set it in
          backend/.env first.
        </p>
      )}
      <div className="mb-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <Field label="Name" hint="a-z, 0-9, _">
          <input className={input} value={d.name} maxLength={24}
            onChange={(e) => setD({ ...d, name: e.target.value.toLowerCase() })} />
        </Field>
      </div>
      <Settings d={d} set={setD} defaultHold={defaultHold} />
      <Leaders d={d} set={setD} />
      <div className="mt-4 flex flex-col gap-2">
        <span className="text-[11px] font-medium tracking-[0.12em] text-muted uppercase">Trading wallet</span>
        <label className="flex items-center gap-2 text-sm">
          <input type="radio" checked={mode === "generate"} onChange={() => setMode("generate")} />
          Make a new wallet (recommended): you get its private key once, to import in Phantom
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input type="radio" checked={mode === "import"} onChange={() => setMode("import")} />
          Use a wallet I have: paste its private key (use a wallet only for this bot, never your main one)
        </label>
        {mode === "import" && (
          <input type="password" autoComplete="off" className={`${input} num`} placeholder="Private key (base58)"
            value={key} onChange={(e) => setKey(e.target.value)} />
        )}
      </div>
      <footer className="mt-4 flex flex-wrap items-center gap-3">
        <button type="button" disabled={busy || !canStore || !d.name}
          className={button("primary")}
          onClick={async () => {
            setBusy(true);
            setErr("");
            try {
              const r = await api.createAccount(password, {
                name: d.name,
                ...payload(d),
                take_capital_at_pct: d.take_on ? num(d.take_capital_at_pct) : null,
                max_hold_hours: d.max_hold_hours ? num(d.max_hold_hours) : null,
                key_mode: mode,
                private_key: mode === "import" ? key : null,
              });
              setKey("");
              setD(EMPTY);
              setOpen(false);
              onCreated(r.account, r.secret);
            } catch (e) {
              setErr(e instanceof ApiError ? e.message : "Could not create the account");
            } finally {
              setBusy(false);
            }
          }}>
          <Plus className="size-4" /> Create
        </button>
        <button type="button" className="text-xs text-muted hover:text-ink" onClick={() => setOpen(false)}>
          Cancel
        </button>
        {err && <span className="text-xs text-bear">{err}</span>}
      </footer>
    </section>
  );
}

function SecretOnce({ name, pubkey, secret, onClose }: {
  name: string;
  pubkey: string | null;
  secret: string;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <section className="card border-gold/50 bg-gold-soft/30 p-5">
      <h2 className="mb-2 flex items-center gap-2 text-base font-semibold text-warn">
        <KeyRound className="size-4" /> Save the private key of “{name}” now
      </h2>
      <p className="mb-3 text-sm text-muted">
        This is shown only once. Import it in Phantom (Add wallet → Import private key) to see the tokens and sell
        by hand. Anyone with this key controls the wallet. Then send SOL to the address below to start trading.
      </p>
      <div className="num mb-2 rounded-md border border-line bg-carbon px-3 py-2 text-xs break-all">{secret}</div>
      {pubkey && <p className="num mb-3 text-xs break-all text-muted">Deposit address: {pubkey}</p>}
      <div className="flex gap-2">
        <button type="button"
          className="inline-flex items-center gap-1 rounded-md border border-line px-3 py-1.5 text-sm hover:bg-raised"
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(secret);
              setCopied(true);
            } catch {
              /* clipboard blocked: select the text by hand */
            }
          }}>
          <Copy className="size-4" /> {copied ? "Copied" : "Copy key"}
        </button>
        <button type="button" className="rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-carbon"
          onClick={() => {
            if (confirm("Did you save the private key? It can't be shown again.")) onClose();
          }}>
          I saved it
        </button>
      </div>
    </section>
  );
}

function UsersPanel({ password, onChange }: { password: string; onChange: () => void }) {
  const [users, setUsers] = useState<SiteUser[] | null>(null);
  const [err, setErr] = useState("");
  const [link, setLink] = useState<{ email: string; link: string; minutes: number } | null>(null);
  const load = useCallback(async () => {
    try {
      setUsers((await api.users(password)).users);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "Could not load users");
    }
  }, [password]);
  useEffect(() => {
    load();
  }, [load]);

  async function act(fn: () => Promise<unknown>) {
    setErr("");
    try {
      await fn();
      await load();
      onChange();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "Failed");
    }
  }

  if (!users) return null;
  const pending = users.filter((u) => u.status === "pending").length;
  return (
    <section className="card">
      <header className="flex items-center gap-2 border-b border-line px-5 py-4">
        <Users className="size-4 text-accent" />
        <h2 className="text-[15px] font-semibold tracking-tight">Registered users ({users.length})</h2>
        {pending > 0 && (
          <span className="rounded-full border border-warn/60 px-2 py-0.5 text-[10px] text-warn">{pending} waiting</span>
        )}
        {err && <span className="ml-auto text-xs text-bear">{err}</span>}
      </header>
      {link && (
        <div className="border-b border-line px-4 py-3 text-xs">
          <p className="mb-1 text-muted">
            Reset link for <b className="text-ink">{link.email}</b> (one use, {link.minutes} min). Send it to them
            privately, only after you are sure it is really them:
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <code className="num break-all text-ink">{link.link}</code>
            <button type="button" className="inline-flex items-center gap-1 rounded border border-line px-2 py-0.5 hover:bg-raised"
              onClick={() => navigator.clipboard.writeText(link.link).catch(() => undefined)}>
              <Copy className="size-3" /> copy
            </button>
            <button type="button" className="text-muted hover:text-ink" onClick={() => setLink(null)}>close</button>
          </div>
        </div>
      )}
      {users.length === 0 ? (
        <p className="px-4 py-4 text-xs text-faint">Nobody has signed up yet. The sign-up page is /login.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="data-table min-w-[620px] text-sm">
            <thead>
              <tr>
                <th className="px-4 py-2 text-left">Email</th>
                <th className="px-4 py-2 text-left">Name</th>
                <th className="px-4 py-2 text-left">Status</th>
                <th className="px-4 py-2 text-left">Account</th>
                <th className="px-4 py-2 text-left">2-step</th>
                <th className="px-4 py-2 text-left">Signed up</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id}>
                  <td className="px-4 py-2">{u.email}</td>
                  <td className="px-4 py-2 text-muted">{u.name}</td>
                  <td className={`px-4 py-2 text-xs font-semibold ${u.status === "active" ? "text-bull" : u.status === "pending" ? "text-warn" : "text-bear"}`}>
                    {u.status}
                  </td>
                  <td className="num px-4 py-2 text-xs">{u.account ?? "—"}</td>
                  <td className="px-4 py-2 text-xs">
                    <span className={u.totp_enabled ? "text-bull" : "text-faint"}>{u.totp_enabled ? "on" : "off"}</span>
                    {u.withdraw_locked_until && new Date(u.withdraw_locked_until) > new Date() && (
                      <span className="ml-1 text-warn" title={`Withdrawals locked until ${new Date(u.withdraw_locked_until).toLocaleString()}`}>· locked</span>
                    )}
                  </td>
                  <td className="px-4 py-2 text-xs text-muted">{u.created_at ? new Date(u.created_at).toLocaleDateString() : ""}</td>
                  <td className="px-4 py-2 text-right">
                    <span className="inline-flex flex-wrap justify-end gap-1.5">
                      <button type="button" className="rounded-md border border-line px-2.5 py-1 text-xs hover:bg-raised"
                        title="A one-time link to choose a new password; send it to the user yourself"
                        onClick={() => act(async () => {
                          const r = await api.resetLink(password, u.id);
                          setLink({ email: u.email, link: r.link, minutes: r.minutes });
                        })}>
                        Reset link
                      </button>
                      {u.totp_enabled && (
                        <button type="button" className="rounded-md border border-line px-2.5 py-1 text-xs hover:bg-raised"
                          title="User lost their phone: turn two-step codes off; withdrawals wait 24 h"
                          onClick={() => confirm(`Turn off two-step codes for ${u.email}? Only if you are sure it is them. Withdrawals will wait 24 h.`) &&
                            act(() => api.reset2fa(password, u.id))}>
                          Reset 2-step
                        </button>
                      )}
                      {u.status === "pending" && (
                        <button type="button" className="rounded-md bg-accent px-2.5 py-1 text-xs font-semibold text-carbon"
                          title="Gives a new wallet copying the same smart wallets as main; edit it below afterwards"
                          onClick={() => confirm(`Approve ${u.email}? They get a new wallet copying main's smart wallets.`) &&
                            act(() => api.approveUser(password, u.id, {}))}>
                          Approve
                        </button>
                      )}
                      {u.status !== "blocked" ? (
                        <button type="button" className="rounded-md border border-line px-2.5 py-1 text-xs hover:bg-raised"
                          onClick={() => confirm(`Block ${u.email}? They are signed out and their account stops buying.`) &&
                            act(() => api.blockUser(password, u.id, true))}>
                          Block
                        </button>
                      ) : (
                        <button type="button" className="rounded-md border border-line px-2.5 py-1 text-xs hover:bg-raised"
                          onClick={() => act(() => api.blockUser(password, u.id, false))}>
                          Unblock
                        </button>
                      )}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

const ACTION_WORDS: Record<string, string> = {
  register: "signed up", login: "signed in", login_failed: "wrong password", user_approved: "approved",
  user_blocked: "blocked", user_unblocked: "unblocked", settings: "changed settings", key_revealed: "viewed key",
  withdraw: "withdrew", withdraw_start: "withdrawal started", withdraw_failed: "withdrawal failed",
  sell: "sold from page", password_changed: "changed password", password_reset: "reset password",
  reset_requested: "asked for reset", reset_link: "reset link made", "2fa_enabled": "2-step on",
  "2fa_disabled": "2-step off", "2fa_reset": "2-step reset", account_edit: "edited account",
};
const LOUD = new Set(["withdraw", "withdraw_failed", "key_revealed", "2fa_disabled", "2fa_reset", "password_reset",
  "user_blocked", "login_failed"]);

function details(e: AuditEntry): string {
  const d = e.details ?? {};
  if (e.action === "withdraw" || e.action === "withdraw_start" || e.action === "withdraw_failed")
    return `${String(d.sol ?? "")} SOL → ${String(d.to ?? "").slice(0, 6)}…${d.error ? ` · ${String(d.error).slice(0, 80)}` : ""}`;
  if (e.action === "settings" && d.after) {
    const a = d.after as Record<string, unknown>;
    return `entry $${a.size_usd} · budget $${a.max_open_usd} · loss $${a.max_daily_loss_usd}${a.paused ? " · paused" : ""}`;
  }
  if (e.action === "sell") return `${Number(d.share ?? 0) * 100}% of position #${d.position}`;
  if (e.action === "account_edit") return Object.keys(d).join(", ");
  if (e.action === "user_approved") return `account ${String(d.account ?? "")}`;
  return "";
}

function ActivityPanel({ password }: { password: string }) {
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<AuditEntry[] | null>(null);
  const [open, setOpen] = useState(false);
  const load = useCallback(async (query: string) => {
    try {
      setRows((await api.audit(password, query)).entries);
    } catch {
      setRows([]);
    }
  }, [password]);
  useEffect(() => {
    if (!open) return;
    const t = setTimeout(() => load(q), 300);
    return () => clearTimeout(t);
  }, [q, open, load]);

  return (
    <section className="card">
      <header className="flex flex-wrap items-center gap-2 border-b border-line px-5 py-4">
        <ScrollText className="size-4 text-accent" />
        <h2 className="text-[15px] font-semibold tracking-tight">Activity log</h2>
        <span className="text-xs text-faint">sign-ins, approvals, settings, keys, withdrawals</span>
        <button type="button" className="ml-auto rounded-md border border-line px-2.5 py-1 text-xs hover:bg-raised"
          onClick={() => setOpen(!open)}>
          {open ? "Hide" : "Show"}
        </button>
      </header>
      {open && (
        <>
          <div className="border-b border-line px-4 py-2">
            <input className={input} placeholder="Search: email, user:12, withdraw, an IP…" value={q}
              onChange={(e) => setQ(e.target.value)} />
          </div>
          <div className="max-h-[480px] overflow-auto">
            <table className="data-table min-w-[720px] text-xs">
              <thead className="sticky top-0">
                <tr>
                  <th className="px-4 py-2 text-left">When</th>
                  <th className="px-4 py-2 text-left">Who</th>
                  <th className="px-4 py-2 text-left">What</th>
                  <th className="px-4 py-2 text-left">On</th>
                  <th className="px-4 py-2 text-left">Details</th>
                  <th className="px-4 py-2 text-left">IP</th>
                </tr>
              </thead>
              <tbody>
                {(rows ?? []).map((e, i) => (
                  <tr key={i} className="border-b border-line/50 last:border-0">
                    <td className="num px-4 py-1.5 whitespace-nowrap text-muted">{e.at ? new Date(e.at).toLocaleString() : ""}</td>
                    <td className="num px-4 py-1.5">{e.actor}</td>
                    <td className={`px-4 py-1.5 font-medium ${LOUD.has(e.action) ? "text-warn" : ""}`}>
                      {ACTION_WORDS[e.action] ?? e.action}
                    </td>
                    <td className="px-4 py-1.5">{e.target}</td>
                    <td className="num px-4 py-1.5 text-muted">{details(e)}</td>
                    <td className="num px-4 py-1.5 text-faint">{e.ip}</td>
                  </tr>
                ))}
                {rows && rows.length === 0 && (
                  <tr><td colSpan={6} className="px-4 py-4 text-faint">Nothing yet.</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}

export function AccountsView() {
  const [password, setPassword] = useState("");
  const [inputPw, setInputPw] = useState("");
  const [data, setData] = useState<AccountsList | null>(null);
  const [error, setError] = useState("");
  const [secret, setSecret] = useState<{ name: string; pubkey: string | null; secret: string } | null>(null);

  useAdminResume(setPassword);

  const load = useCallback(async (pw: string) => {
    try {
      const r = await api.accounts(pw);
      setData(r);
      setError("");
      save(pw);
    } catch (e) {
      if (e instanceof ApiError && (e.status === 401 || e.status === 404)) {
        save(null);
        setPassword("");
      }
      setError(e instanceof ApiError ? (e.status === 401 ? "Wrong password." : e.message) : "Error");
    }
  }, []);

  useEffect(() => {
    if (password) load(password);
  }, [password, load]);
  const [owners, setOwners] = useState<Record<number, string>>({});
  useEffect(() => {
    if (!password || !data) return;
    api.users(password).then((r) => setOwners(Object.fromEntries(r.users.map((u) => [u.id, u.email])))).catch(() => undefined);
  }, [password, data]);

  useEffect(() => {
    if (!secret) return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [secret]);

  if (!password) {
    return (
      <>
        <AppNav />
        <AdminSignIn onDone={() => setPassword(SESSION)} />
      </>
    );
  }

  const hold = data?.defaults.max_hold_hours ?? 168;
  const replace = (a: TradingAccountCfg) =>
    setData((d) => d && { ...d, accounts: d.accounts.map((x) => (x.name === a.name ? a : x)) });

  return (
    <>
    <AppNav />
    <main className="mx-auto max-w-[1200px] px-4 pt-6 pb-14 sm:px-6">
      <PageHeader icon={<Users className="size-5" />} title="Trading accounts"
        subtitle={<>Each account: its own wallet, smart wallets, entry, budget and loss limit. Edits apply within {data?.defaults.reload_seconds ?? 30}s, no restart.</>}
        badges={data && <Badge tone="muted">{data.accounts.length} accounts</Badge>} />
      {error && <p className="mb-4 text-sm text-bear">{error}</p>}
      <div className="flex flex-col gap-6">
        {secret && <SecretOnce {...secret} onClose={() => setSecret(null)} />}
        {data && <UsersPanel password={password} onChange={() => load(password)} />}
        {data && <ActivityPanel password={password} />}
        {data?.accounts.map((a) => (
          <AccountCard key={a.name} a={a} password={password} defaultHold={hold} onSaved={replace}
            owner={a.user_id != null ? (owners[a.user_id] ?? `#${a.user_id}`) : undefined} />
        ))}
        {data && (
          <NewAccount password={password} canStore={data.can_store_keys} defaultHold={hold}
            onCreated={(a, s) => {
              setData((d) => d && { ...d, accounts: [...d.accounts, a] });
              if (s) setSecret({ name: a.name, pubkey: a.pubkey, secret: s });
            }} />
        )}
        {!data && !error && <p className="text-sm text-muted">Loading…</p>}
      </div>
    </main>
    </>
  );
}
