"use client";

import { KeyRound, LogOut, ShieldCheck, UserRound } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { SirbLockup } from "@/components/brand/Logo";
import { button, field } from "@/components/ui";
import { api, ApiError, SESSION } from "@/lib/api";

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
        const r = await api.login(email, pw);
        if (r.role !== "admin") {
          await api.logout().catch(() => undefined);
          setErr("This is not an admin account.");
          return;
        }
      }
      window.dispatchEvent(new Event(AUTH_EVENT));
      onDone();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "Something went wrong");
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
        {err && <p className="text-xs text-bear">{err}</p>}
        <button type="submit" disabled={busy || setup === null} className={button("primary", "py-2.5")}>
          {setup ? "Create admin login" : "Sign in"}
        </button>
        {!setup && (
          <a href="/login" className="text-center text-xs text-muted hover:text-ink">Forgot the password? Use “Forgot your password?” on the sign-in page</a>
        )}
      </form>
    </main>
  );
}

/** Top-bar account menu for the operator: who is signed in, change password, sign out. */
export function AdminMenu() {
  const [me, setMe] = useState<{ email: string | null; via: string } | null>(null);
  const [open, setOpen] = useState(false);
  const [pwOpen, setPwOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const load = () => api.adminMe().then(setMe).catch(() => setMe(null));
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
          <button type="button"
            onClick={async () => { await adminSignOut(); window.location.reload(); }}
            className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-sm text-bear hover:bg-bear-soft">
            <LogOut className="size-4" /> Sign out
          </button>
        </div>
      )}
      {pwOpen && createPortal(<ChangePassword onClose={() => setPwOpen(false)} />, document.body)}
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
