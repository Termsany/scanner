"use client";

import { LogIn, UserPlus } from "lucide-react";
import { useState } from "react";

import { api, ApiError } from "@/lib/api";

const input =
  "w-full rounded-md border border-line bg-carbon px-3 py-2 text-sm outline-none focus:border-accent";

export function LoginView() {
  const [mode, setMode] = useState<"login" | "register" | "forgot">("login");
  const [info, setInfo] = useState("");
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [pw, setPw] = useState("");
  const [pw2, setPw2] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr("");
    if (mode === "register" && pw !== pw2) {
      setErr("The two passwords are different.");
      return;
    }
    setBusy(true);
    try {
      if (mode === "forgot") {
        const r = await api.forgot(email);
        setInfo(r.message);
        return;
      }
      if (mode === "login") await api.login(email, pw);
      else await api.register(email, pw, name);
      window.location.href = "/me";
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto flex min-h-[75vh] max-w-sm flex-col justify-center px-4">
      <form className="card flex flex-col gap-3 p-6" onSubmit={submit}>
        <h1 className="flex items-center gap-2 text-base font-semibold">
          {mode === "login" ? <LogIn className="size-4 text-accent" /> : <UserPlus className="size-4 text-accent" />}
          {mode === "login" ? "Sign in" : mode === "forgot" ? "Forgot your password?" : "Create an account"}
        </h1>
        {mode === "forgot" && (
          <p className="text-xs text-muted">Enter your email. If it is registered, you get a link to choose a new
            password (it works once, for one hour).</p>
        )}
        {mode === "register" && (
          <p className="text-xs text-muted">
            After you sign up, the admin reviews your account. Once approved you get your own trading wallet.
          </p>
        )}
        {mode === "register" && (
          <input className={input} placeholder="Name" value={name} maxLength={80} autoComplete="name"
            onChange={(e) => setName(e.target.value)} />
        )}
        <input className={input} type="email" placeholder="Email" value={email} required autoComplete="email"
          onChange={(e) => setEmail(e.target.value)} />
        {mode !== "forgot" && (
          <input className={input} type="password" placeholder="Password" value={pw} required
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            minLength={mode === "register" ? 10 : undefined} onChange={(e) => setPw(e.target.value)} />
        )}
        {mode === "register" && (
          <input className={input} type="password" placeholder="Repeat password (10+ characters)" value={pw2}
            required autoComplete="new-password" onChange={(e) => setPw2(e.target.value)} />
        )}
        {err && <p className="text-xs text-bear">{err}</p>}
        {info && <p className="text-xs text-bull">{info}</p>}
        <button type="submit" disabled={busy || (mode === "forgot" && !!info)}
          className="rounded-md bg-accent px-3 py-2 text-sm font-semibold text-carbon disabled:opacity-50">
          {mode === "login" ? "Sign in" : mode === "forgot" ? "Send me a reset link" : "Sign up"}
        </button>
        {mode === "login" && (
          <button type="button" className="text-xs text-muted hover:text-ink"
            onClick={() => { setMode("forgot"); setErr(""); setInfo(""); }}>
            Forgot your password?
          </button>
        )}
        <button type="button" className="text-xs text-muted hover:text-ink"
          onClick={() => {
            setMode(mode === "login" ? "register" : "login");
            setErr("");
            setInfo("");
          }}>
          {mode === "login" ? "No account yet? Sign up" : "Already have an account? Sign in"}
        </button>
      </form>
    </main>
  );
}
