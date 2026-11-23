"use client";

import { Fingerprint, KeyRound, LogOut, ShieldCheck, Smartphone, Trash2, UserRound } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { SirbLockup } from "@/components/brand/Logo";
import { button, field } from "@/components/ui";
import { api, ApiError, SESSION } from "@/lib/api";
import { addPasskey, passkeysSupported, signInWithPasskey } from "@/lib/passkey";

const KEY = "alpha-live-password"; // the old shared password, kept only until the admin email login exists
const AUTH_EVENT = "sirb-admin-auth"; // fired after an admin signs in, so the top-bar menu refreshes

function savedShared(): string {
  try {
    const v = localStorage.getItem(KEY) ?? "";
    return v === SESSION ? "" : v;
  } catch {
    return "";
  }
}

/** What admin calls from a public page send: the shared password saved here, or else the session cookie. */
export function adminCredential(): string {
  return savedShared() || SESSION;
}

/** Who is signed in as admin: the session cookie, or else the old shared password saved in this browser. */
export async function adminWho() {
  try {
    return await api.adminMe();
  } catch (e) {
    const saved = savedShared();
    if (saved) return await api.adminMe(saved);
    throw e;
  }
}

/** On load: an admin session cookie signs the page in; otherwise an old saved shared password is tried. */
export function useAdminResume(setPassword: (v: string) => void) {
  useEffect(() => {
    let alive = true;
    api.adminMe()
      .then((r) => alive && r.via === "session" && setPassword(SESSION))
      .catch(() => {
        const saved = savedShared();
        if (alive && saved) setPassword(saved);
      });
    return () => {
      alive = false;
    };
  }, [setPassword]);
}

export async function adminSignOut() {
  try {
    localStorage.removeItem(KEY);
  } catch {
    /* storage unavailable */
  }
  await api.logout().catch(() => undefined);
}

/** Admin sign-in: email + password; the first time, set up that login with the old shared password. */
export function AdminSignIn({ onDone }: { onDone: () => void }) {
  const [setup, setSetup] = useState<boolean | null>(null);
  const [email, setEmail] = useState("");
  const [pw, setPw] = useState("");
  const [pw2, setPw2] = useState("");
  const [old, setOld] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [needCode, setNeedCode] = useState(false);
  const [code, setCode] = useState("");
  const [canPasskey, setCanPasskey] = useState(false);
  useEffect(() => setCanPasskey(passkeysSupported()), []);

  async function passkey() {
    setErr("");
    setBusy(true);
    try {
      const role = await signInWithPasskey();
      if (role !== "admin") {
        await api.logout().catch(() => undefined);
        setErr("This passkey is not for an admin account.");
        return;
      }
      window.dispatchEvent(new Event(AUTH_EVENT));
      onDone();
    } catch (e) {
      if ((e as Error).name === "NotAllowedError") setErr("Passkey sign-in was cancelled.");
      else setErr(e instanceof ApiError ? e.message : (e as Error).message || "Passkey sign-in failed");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    api.adminStatus().then((r) => setSetup(!r.has_admin)).catch(() => setSetup(false));
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr("");
    if (setup && pw !== pw2) {
      setErr("The two new passwords are different.");
      return;
    }
    setBusy(true);
    try {
      if (setup) {
        await api.adminSetup(old, email, pw);
      } else {
        const r = await api.login(email, pw, needCode ? code : undefined);
        if (r.role !== "admin") {
          await api.logout().catch(() => undefined);
          setErr("This is not an admin account.");
          return;
        }
      }
      window.dispatchEvent(new Event(AUTH_EVENT));
      onDone();
    } catch (e) {
      if (e instanceof ApiError && e.message === "Two-step code required") {
        setNeedCode(true);
        setErr("");
      } else {
        setErr(e instanceof ApiError ? e.message : "Something went wrong");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto flex min-h-[78vh] max-w-[400px] flex-col justify-center px-4">
      <div className="mb-5 flex justify-center"><SirbLockup size={30} /></div>
      <form className="card flex animate-rise flex-col gap-3.5 p-7" onSubmit={submit}>
        <h1 className="flex items-center gap-2 text-lg font-semibold">
          <ShieldCheck className="size-5 text-mint" /> {setup ? "Set up your admin login" : "Admin sign in"}
        </h1>
        {setup ? (
          <p className="text-sm text-muted">
            One time: choose the email and password you will sign in with. Confirm with the current shared admin
            password; after this it stops working.
          </p>
        ) : (
          <p className="text-sm text-muted">Operator pages: Overview, Accounts, Live.</p>
        )}
        {setup && (
          <input type="password" className={field} placeholder="Current shared admin password" value={old}
            onChange={(e) => setOld(e.target.value)} required autoComplete="current-password" />
        )}
        <input type="email" className={field} placeholder="Admin email" value={email} dir="ltr"
          onChange={(e) => setEmail(e.target.value)} required autoComplete="username" />
        <input type="password" className={field} placeholder={setup ? "New password (10+ characters)" : "Password"}
          value={pw} onChange={(e) => setPw(e.target.value)} required minLength={setup ? 10 : undefined}
          autoComplete={setup ? "new-password" : "current-password"} />
        {setup && (
          <input type="password" className={field} placeholder="Repeat the new password" value={pw2}
            onChange={(e) => setPw2(e.target.value)} required autoComplete="new-password" />
        )}
        {needCode && (
          <>
            <p className="flex items-center gap-2 text-sm text-muted"><Smartphone className="size-4 text-mint" /> Enter the 6-digit code from your authenticator app.</p>
            <input className={`${field} num text-center tracking-[0.4em]`} inputMode="numeric" autoComplete="one-time-code"
              placeholder="000000" maxLength={6} value={code} autoFocus required
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))} />
          </>
        )}
        {err && <p className="text-xs text-bear">{err}</p>}
        <button type="submit" disabled={busy || setup === null} className={button("primary", "py-2.5")}>
          {setup ? "Create admin login" : needCode ? "Verify and sign in" : "Sign in"}
        </button>
        {!setup && canPasskey && (
          <>
            <div className="flex items-center gap-3 text-[11px] text-faint uppercase"><span className="h-px flex-1 bg-line" />or<span className="h-px flex-1 bg-line" /></div>
            <button type="button" onClick={passkey} disabled={busy} className={button("secondary", "py-2.5")}>
              <Fingerprint className="size-4" /> Sign in with a passkey
            </button>
          </>
        )}
        {!setup && (
          <a href="/login" className="text-center text-xs text-muted hover:text-ink">Forgot the password? Use “Forgot your password?” on the sign-in page</a>
        )}
      </form>
    </main>
  );
}

/** Top-bar account menu for the operator: who is signed in, change password, sign out. */
export function AdminMenu() {
  const [me, setMe] = useState<{ email: string | null; via: string; totp_enabled?: boolean } | null>(null);
  const [open, setOpen] = useState(false);
  const [pwOpen, setPwOpen] = useState(false);
  const [tfOpen, setTfOpen] = useState(false);
  const [pkOpen, setPkOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const load = () => adminWho().then(setMe).catch(() => setMe(null));
    load();
    window.addEventListener(AUTH_EVENT, load);
    return () => window.removeEventListener(AUTH_EVENT, load);
  }, []);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => !ref.current?.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);
  if (!me) return null;
  return (
    <div className="relative shrink-0" ref={ref}>
      <button type="button" onClick={() => setOpen(!open)} aria-expanded={open}
        className="inline-flex items-center gap-2 rounded-lg border border-line bg-surface px-2.5 py-1.5 text-sm text-muted hover:text-ink">
        <UserRound className="size-4" />
        <span className="hidden max-w-[180px] truncate sm:inline">{me.email ?? "Shared password"}</span>
      </button>
      {open && (
        <div className="absolute end-0 top-full z-50 mt-2 w-64 animate-rise rounded-xl border border-line bg-surface p-1.5 shadow-[0_24px_60px_-20px_rgb(0_0_0/0.8)]">
          <div className="px-3 py-2 text-xs text-muted">
            Signed in as<div className="truncate text-sm text-ink">{me.email ?? "shared password (set up an email login)"}</div>
          </div>
          {me.via === "session" && (
            <button type="button" onClick={() => { setPwOpen(true); setOpen(false); }}
              className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-sm hover:bg-raised">
              <KeyRound className="size-4 text-muted" /> Change password
            </button>
          )}
          {me.via === "session" && (
            <button type="button" onClick={() => { setTfOpen(true); setOpen(false); }}
              className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-sm hover:bg-raised">
              <Smartphone className="size-4 text-muted" /> Two-step codes
              <span className={`ms-auto rounded-full px-2 py-0.5 text-[10px] font-semibold ${me.totp_enabled ? "bg-mint-soft text-mint" : "bg-gold-soft text-gold"}`}>
                {me.totp_enabled ? "ON" : "OFF"}
              </span>
            </button>
          )}
          {me.via === "session" && (
            <button type="button" onClick={() => { setPkOpen(true); setOpen(false); }}
              className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-sm hover:bg-raised">
              <Fingerprint className="size-4 text-muted" /> Passkeys
            </button>
          )}
          <button type="button"
            onClick={async () => { await adminSignOut(); window.location.reload(); }}
            className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-sm text-bear hover:bg-bear-soft">
            <LogOut className="size-4" /> Sign out
          </button>
        </div>
      )}
      {pwOpen && createPortal(<ChangePassword onClose={() => setPwOpen(false)} />, document.body)}
      {tfOpen && createPortal(<TwoStep on={!!me.totp_enabled} onClose={() => { setTfOpen(false); window.dispatchEvent(new Event(AUTH_EVENT)); }} />, document.body)}
      {pkOpen && createPortal(<Passkeys onClose={() => setPkOpen(false)} />, document.body)}
    </div>
  );
}

function ChangePassword({ onClose }: { onClose: () => void }) {
  const [cur, setCur] = useState("");
  const [pw, setPw] = useState("");
  const [pw2, setPw2] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  return (
    <div className="fixed inset-0 z-[60] grid place-items-center bg-carbon/70 px-4 backdrop-blur-sm" onClick={onClose}>
      <form className="card w-full max-w-[400px] animate-rise p-6" onClick={(e) => e.stopPropagation()}
        onSubmit={async (e) => {
          e.preventDefault();
          if (pw !== pw2) {
            setMsg({ ok: false, text: "The two new passwords are different." });
            return;
          }
          setBusy(true);
          setMsg(null);
          try {
            await api.changePassword(cur, pw);
            setMsg({ ok: true, text: "Password changed. Other devices are signed out." });
            setCur(""); setPw(""); setPw2("");
          } catch (err) {
            setMsg({ ok: false, text: err instanceof ApiError ? err.message : "Could not change it" });
          } finally {
            setBusy(false);
          }
        }}>
        <h2 className="mb-4 flex items-center gap-2 text-lg font-semibold"><KeyRound className="size-5 text-mint" /> Change password</h2>
        <div className="flex flex-col gap-3">
          <input type="password" className={field} placeholder="Current password" value={cur}
            onChange={(e) => setCur(e.target.value)} required autoComplete="current-password" />
          <input type="password" className={field} placeholder="New password (10+ characters)" value={pw}
            onChange={(e) => setPw(e.target.value)} required minLength={10} autoComplete="new-password" />
          <input type="password" className={field} placeholder="Repeat the new password" value={pw2}
            onChange={(e) => setPw2(e.target.value)} required autoComplete="new-password" />
          {msg && <p className={`text-xs ${msg.ok ? "text-mint" : "text-bear"}`}>{msg.text}</p>}
          <div className="mt-1 flex justify-end gap-2">
            <button type="button" onClick={onClose} className={button("ghost")}>Close</button>
            <button type="submit" disabled={busy} className={button("primary")}>Save new password</button>
          </div>
        </div>
      </form>
    </div>
  );
}

function Modal({ title, icon, onClose, children }: { title: string; icon: React.ReactNode; onClose: () => void; children: React.ReactNode }) {
  return (
    <div className="fixed inset-0 z-[60] grid place-items-center bg-carbon/70 px-4 backdrop-blur-sm" onClick={onClose}>
      <div className="card w-full max-w-[420px] animate-rise p-6" onClick={(e) => e.stopPropagation()}>
        <h2 className="mb-4 flex items-center gap-2 text-lg font-semibold">{icon} {title}</h2>
        {children}
      </div>
    </div>
  );
}

/** Turn two-step codes on (password -> QR -> first code) or off (password + code). */
function TwoStep({ on, onClose }: { on: boolean; onClose: () => void }) {
  const [pw, setPw] = useState("");
  const [code, setCode] = useState("");
  const [qr, setQr] = useState<{ secret: string; qr: string } | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setMsg(null);
    try {
      await fn();
    } catch (e) {
      setMsg({ ok: false, text: e instanceof ApiError ? e.message : "Something went wrong" });
    } finally {
      setBusy(false);
    }
  };
  const codeInput = (
    <input className={`${field} num text-center tracking-[0.4em]`} inputMode="numeric" autoComplete="one-time-code"
      placeholder="000000" maxLength={6} value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))} />
  );
  return (
    <Modal title="Two-step codes" icon={<Smartphone className="size-5 text-mint" />} onClose={onClose}>
      {done ? (
        <p className="text-sm text-mint">{on ? "Two-step codes are off." : "Two-step codes are on. Sign-in now asks for a code (or use a passkey)."}</p>
      ) : on ? (
        <form className="flex flex-col gap-3" onSubmit={(e) => { e.preventDefault(); run(async () => { await api.totpDisable(pw, code); setDone(true); }); }}>
          <p className="text-sm text-muted">They are on. Turning them off makes sign-in password-only.</p>
          <input type="password" className={field} placeholder="Password" value={pw} onChange={(e) => setPw(e.target.value)} required />
          {codeInput}
          <button type="submit" disabled={busy} className={button("danger")}>Turn off</button>
        </form>
      ) : !qr ? (
        <form className="flex flex-col gap-3" onSubmit={(e) => { e.preventDefault(); run(async () => setQr(await api.totpSetup(pw))); }}>
          <p className="text-sm text-muted">Use an authenticator app (Google Authenticator, Authy, 1Password…). Confirm with your password to start.</p>
          <input type="password" className={field} placeholder="Password" value={pw} onChange={(e) => setPw(e.target.value)} required />
          <button type="submit" disabled={busy} className={button("primary")}>Continue</button>
        </form>
      ) : (
        <form className="flex flex-col gap-3" onSubmit={(e) => { e.preventDefault(); run(async () => { await api.totpEnable(code); setDone(true); }); }}>
          <p className="text-sm text-muted">Scan this in your app, then type the code it shows.</p>
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={qr.qr} alt="QR code for the authenticator app" className="mx-auto size-48 rounded-xl bg-white p-2" />
          <p className="num break-all text-center text-xs text-muted">{qr.secret}</p>
          {codeInput}
          <button type="submit" disabled={busy || code.length !== 6} className={button("primary")}>Turn on</button>
        </form>
      )}
      {msg && <p className={`mt-3 text-xs ${msg.ok ? "text-mint" : "text-bear"}`}>{msg.text}</p>}
      <div className="mt-4 flex justify-end"><button type="button" onClick={onClose} className={button("ghost")}>Close</button></div>
    </Modal>
  );
}

/** Add, list and remove passkeys. */
function Passkeys({ onClose }: { onClose: () => void }) {
  const [list, setList] = useState<{ id: number; name: string; created_at: string | null; last_used_at: string | null }[] | null>(null);
  const [pw, setPw] = useState("");
  const [name, setName] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const load = () => api.passkeys().then((r) => setList(r.passkeys)).catch(() => setList([]));
  useEffect(() => {
    load();
  }, []);
  const supported = passkeysSupported();
  return (
    <Modal title="Passkeys" icon={<Fingerprint className="size-5 text-mint" />} onClose={onClose}>
      <p className="text-sm text-muted">
        Sign in with your fingerprint, face or device PIN instead of a password. A passkey also stands in for the
        two-step code.
      </p>
      <ul className="mt-4 divide-y divide-line rounded-xl border border-line">
        {list === null && <li className="px-3 py-3 text-sm text-muted">Loading…</li>}
        {list?.length === 0 && <li className="px-3 py-3 text-sm text-muted">No passkeys yet.</li>}
        {list?.map((p) => (
          <li key={p.id} className="flex items-center gap-3 px-3 py-2.5 text-sm">
            <Fingerprint className="size-4 text-mint" />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-ink">{p.name}</span>
              <span className="text-xs text-faint">
                added {p.created_at?.slice(0, 10) ?? "—"}{p.last_used_at ? ` · last used ${p.last_used_at.slice(0, 10)}` : ""}
              </span>
            </span>
            <button type="button" aria-label={`Remove ${p.name}`} disabled={busy || !pw}
              title={pw ? "Remove" : "Type your password below to remove"}
              className="text-muted hover:text-bear disabled:opacity-40"
              onClick={async () => {
                setBusy(true);
                setMsg(null);
                try {
                  await api.passkeyDelete(p.id, pw);
                  await load();
                  setMsg({ ok: true, text: `Removed ${p.name}.` });
                } catch (e) {
                  setMsg({ ok: false, text: e instanceof ApiError ? e.message : "Could not remove it" });
                } finally {
                  setBusy(false);
                }
              }}>
              <Trash2 className="size-4" />
            </button>
          </li>
        ))}
      </ul>
      {!supported ? (
        <p className="mt-4 text-xs text-gold">This browser does not support passkeys.</p>
      ) : (
        <form className="mt-4 flex flex-col gap-3"
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            setMsg(null);
            try {
              await addPasskey(pw, name || "Passkey");
              setName("");
              await load();
              setMsg({ ok: true, text: "Passkey added. Next time, choose “Sign in with a passkey”." });
            } catch (err) {
              const n = (err as Error).name;
              setMsg({ ok: false, text: n === "NotAllowedError" ? "Cancelled." : n === "InvalidStateError"
                ? "This device already has a passkey here." : err instanceof ApiError ? err.message : (err as Error).message });
            } finally {
              setBusy(false);
            }
          }}>
          <input className={field} placeholder="Name (e.g. MacBook, iPhone)" value={name} maxLength={60}
            onChange={(e) => setName(e.target.value)} />
          <input type="password" className={field} placeholder="Password" value={pw} onChange={(e) => setPw(e.target.value)} required />
          <button type="submit" disabled={busy} className={button("primary")}>
            <Fingerprint className="size-4" /> Add a passkey on this device
          </button>
        </form>
      )}
      {msg && <p className={`mt-3 text-xs ${msg.ok ? "text-mint" : "text-bear"}`}>{msg.text}</p>}
      <div className="mt-4 flex justify-end"><button type="button" onClick={onClose} className={button("ghost")}>Close</button></div>
    </Modal>
  );
}
