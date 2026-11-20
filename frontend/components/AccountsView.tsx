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
  Trash2,
  Users,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { api, ApiError } from "@/lib/api";
import type { AccountsList, LeaderCfg, SiteUser, TradingAccountCfg } from "@/lib/types";

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

function Leaders({ d, set }: { d: Draft; set: (d: Draft) => void }) {
  const [add, setAdd] = useState("");
  const upd = (i: number, patch: Partial<LeaderCfg>) =>
    set({ ...d, leaders: d.leaders.map((l, j) => (j === i ? { ...l, ...patch } : l)) });
  return (
    <div className="mt-4">
      <div className="mb-2 text-[11px] font-medium tracking-[0.12em] text-muted uppercase">
        Smart wallets to copy ({d.leaders.length})
      </div>
      <div className="overflow-x-auto rounded-md border border-line">
        <table className="w-full min-w-[640px] text-sm">
          <thead className="text-[11px] text-muted uppercase">
            <tr className="border-b border-line">
              <th className="px-3 py-2 text-left">Wallet</th>
              <th className="px-3 py-2 text-left">Label</th>
              <th className="px-3 py-2 text-left">Entry</th>
              <th className="px-3 py-2 text-center" title="Copy with no liquidity, chase or scanner checks">
                No checks
              </th>
              <th className="px-3 py-2 text-center" title="Only tokens it did not hold before">New tokens only</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {d.leaders.map((l, i) => (
              <tr key={l.wallet} className="border-b border-line/60 last:border-0">
                <td className="num px-3 py-1.5">
                  <a className="inline-flex items-center gap-1 hover:underline" target="_blank" rel="noreferrer"
                    href={`https://solscan.io/account/${l.wallet}`}>
                    {short(l.wallet)} <ExternalLink className="size-3" />
                  </a>
                </td>
                <td className="px-3 py-1.5">
                  <input className={input} value={l.label} maxLength={40}
                    onChange={(e) => upd(i, { label: e.target.value })} />
                </td>
                <td className="px-3 py-1.5">
                  <input className={`${input} num w-24`} inputMode="decimal" placeholder="account"
                    value={l.size_usd ?? ""}
                    onChange={(e) => upd(i, { size_usd: e.target.value ? Number(e.target.value) : null })} />
                </td>
                <td className="px-3 py-1.5 text-center">
                  <input type="checkbox" checked={l.unconditional}
                    onChange={(e) => upd(i, { unconditional: e.target.checked })} />
                </td>
                <td className="px-3 py-1.5 text-center">
                  <input type="checkbox" checked={l.new_tokens_only}
                    onChange={(e) => upd(i, { new_tokens_only: e.target.checked })} />
                </td>
                <td className="px-3 py-1.5 text-right">
                  <button type="button" aria-label="Remove" className="text-muted hover:text-bear"
                    onClick={() => set({ ...d, leaders: d.leaders.filter((_, j) => j !== i) })}>
                    <Trash2 className="size-4" />
                  </button>
                </td>
              </tr>
            ))}
            {d.leaders.length === 0 && (
              <tr>
                <td colSpan={6} className="px-3 py-3 text-xs text-faint">
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
          className="inline-flex shrink-0 items-center gap-1 rounded-md border border-line px-3 text-sm hover:bg-raised"
          onClick={() => {
            const w = add.trim();
            if (!w || d.leaders.some((l) => l.wallet === w)) return;
            set({ ...d, leaders: [...d.leaders, { wallet: w, label: "", unconditional: false,
              new_tokens_only: false, size_usd: null }] });
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
    <section className={`card p-4 sm:p-5 ${a.enabled ? "" : "opacity-70"}`}>
      <header className="mb-4 flex flex-wrap items-center gap-2">
        <h2 className="text-base font-semibold">{a.name}</h2>
        <span className={`rounded-full border px-2 py-0.5 text-[10px] font-bold tracking-[0.1em] ${
          !a.enabled ? "border-line text-muted" : a.paused ? "border-warn/60 text-warn" : "border-bull/50 text-bull"}`}>
          {!a.enabled ? "DISABLED" : a.paused ? "PAUSED" : "ACTIVE"}
        </span>
        <span className="rounded-full border border-line px-2 py-0.5 text-[10px] text-muted"
          title={a.key_source === "env" ? "Key in the server environment" : "Key stored encrypted"}>
          <KeyRound className="mr-1 inline size-3" />
          {a.key_source === "env" ? "server key" : "stored key"}
        </span>
        {a.pubkey && (
          <a className="num inline-flex items-center gap-1 text-xs text-muted hover:underline" target="_blank"
            rel="noreferrer" href={`https://solscan.io/account/${a.pubkey}`}>
            {short(a.pubkey)} <ExternalLink className="size-3" />
          </a>
        )}
        <span className="text-xs text-faint">{a.open_positions} open</span>
        {owner && (
          <span className="rounded-full border border-accent/50 px-2 py-0.5 text-[10px] text-accent">user: {owner}</span>
        )}
        <div className="ml-auto flex gap-2">
          {a.enabled && (
            <button type="button" disabled={busy}
              className="inline-flex items-center gap-1 rounded-md border border-line px-2.5 py-1 text-xs hover:bg-raised"
              onClick={() => send({ paused: !a.paused }, a.paused ? "Resumed" : "Paused: no new buys, sells continue")}>
              {a.paused ? <Play className="size-3.5" /> : <Pause className="size-3.5" />}
              {a.paused ? "Resume" : "Pause"}
            </button>
          )}
          <button type="button" disabled={busy}
            className="inline-flex items-center gap-1 rounded-md border border-line px-2.5 py-1 text-xs hover:bg-raised"
            onClick={() => {
              if (a.enabled && !confirm(`Stop account ${a.name} completely?`)) return;
              send({ enabled: !a.enabled }, a.enabled ? "Disabled" : "Enabled");
            }}>
            <Power className="size-3.5" /> {a.enabled ? "Disable" : "Enable"}
          </button>
        </div>
      </header>
      <Settings d={d} set={setD} defaultHold={defaultHold} />
      <Leaders d={d} set={setD} />
      {a.user_id != null && <Caps d={d} set={setD} />}
      <footer className="mt-4 flex flex-wrap items-center gap-3">
        <button type="button" disabled={busy || !dirty}
          className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-carbon disabled:opacity-40"
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
        className="inline-flex items-center gap-1.5 rounded-md border border-dashed border-line px-3 py-2 text-sm text-muted hover:bg-raised hover:text-ink"
        onClick={() => setOpen(true)}>
        <Plus className="size-4" /> New account
      </button>
    );
  }
  return (
    <section className="card border-accent/40 p-4 sm:p-5">
      <h2 className="mb-3 text-base font-semibold">New account</h2>
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
          className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-carbon disabled:opacity-40"
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
    <section className="card border-warn/60 p-4 sm:p-5">
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
      <header className="flex items-center gap-2 border-b border-line px-4 py-3">
        <Users className="size-4 text-accent" />
        <h2 className="text-sm font-semibold">Registered users ({users.length})</h2>
        {pending > 0 && (
          <span className="rounded-full border border-warn/60 px-2 py-0.5 text-[10px] text-warn">{pending} waiting</span>
        )}
        {err && <span className="ml-auto text-xs text-bear">{err}</span>}
      </header>
      {users.length === 0 ? (
        <p className="px-4 py-4 text-xs text-faint">Nobody has signed up yet. The sign-up page is /login.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[620px] text-sm">
            <thead className="text-[11px] text-muted uppercase">
              <tr className="border-b border-line">
                <th className="px-4 py-2 text-left">Email</th>
                <th className="px-4 py-2 text-left">Name</th>
                <th className="px-4 py-2 text-left">Status</th>
                <th className="px-4 py-2 text-left">Account</th>
                <th className="px-4 py-2 text-left">Signed up</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id} className="border-b border-line/60 last:border-0">
                  <td className="px-4 py-2">{u.email}</td>
                  <td className="px-4 py-2 text-muted">{u.name}</td>
                  <td className={`px-4 py-2 text-xs font-semibold ${u.status === "active" ? "text-bull" : u.status === "pending" ? "text-warn" : "text-bear"}`}>
                    {u.status}
                  </td>
                  <td className="num px-4 py-2 text-xs">{u.account ?? "—"}</td>
                  <td className="px-4 py-2 text-xs text-muted">{u.created_at ? new Date(u.created_at).toLocaleDateString() : ""}</td>
                  <td className="px-4 py-2 text-right">
                    <span className="inline-flex gap-1.5">
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

export function AccountsView() {
  const [password, setPassword] = useState("");
  const [inputPw, setInputPw] = useState("");
  const [data, setData] = useState<AccountsList | null>(null);
  const [error, setError] = useState("");
  const [secret, setSecret] = useState<{ name: string; pubkey: string | null; secret: string } | null>(null);

  useEffect(() => {
    const saved = readSaved();
    if (saved) setPassword(saved);
  }, []);

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
      <main className="mx-auto flex min-h-[70vh] max-w-sm flex-col justify-center px-4">
        <form className="card flex flex-col gap-3 p-6"
          onSubmit={(e) => {
            e.preventDefault();
            if (inputPw) setPassword(inputPw);
          }}>
          <h1 className="flex items-center gap-2 text-base font-semibold">
            <Lock className="size-4 text-accent" /> Trading accounts
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

  const hold = data?.defaults.max_hold_hours ?? 168;
  const replace = (a: TradingAccountCfg) =>
    setData((d) => d && { ...d, accounts: d.accounts.map((x) => (x.name === a.name ? a : x)) });

  return (
    <main className="mx-auto max-w-[1200px] px-4 pt-5 pb-12 sm:px-6">
      <header className="mb-5 flex flex-wrap items-center gap-3">
        <a href="/live"
          className="inline-flex items-center gap-1 rounded-md border border-line px-2 py-1 text-xs text-muted hover:bg-raised hover:text-ink">
          <ArrowLeft className="size-3.5" /> Live
        </a>
        <Users className="size-5 text-accent" />
        <h1 className="text-lg font-semibold tracking-tight">Trading accounts</h1>
        <span className="text-xs text-faint">
          Each account: its own wallet, smart wallets, entry, budget and loss limit. Edits apply within
          {" "}{data?.defaults.reload_seconds ?? 30} s, no restart.
        </span>
      </header>
      {error && <p className="mb-4 text-sm text-bear">{error}</p>}
      <div className="flex flex-col gap-4">
        {secret && <SecretOnce {...secret} onClose={() => setSecret(null)} />}
        {data && <UsersPanel password={password} onChange={() => load(password)} />}
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
  );
}
