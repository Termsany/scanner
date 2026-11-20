"use client";

import { LogIn, UserPlus } from "lucide-react";
import { useEffect, useState } from "react";

import { api, ApiError } from "@/lib/api";
import { I18nProvider, LangSwitch, useT } from "@/lib/i18n";
import type { PublicTerms } from "@/lib/types";

const input =
  "w-full rounded-md border border-line bg-carbon px-3 py-2 text-sm outline-none focus:border-accent";

function Login() {
  const { t, srv, lang } = useT();
  const [mode, setMode] = useState<"login" | "register" | "forgot">("login");
  const [info, setInfo] = useState("");
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [pw, setPw] = useState("");
  const [pw2, setPw2] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [terms, setTerms] = useState<PublicTerms | null>(null);
  const [agree, setAgree] = useState(false);
  const [coupon, setCoupon] = useState("");
  useEffect(() => {
    api.terms().then(setTerms).catch(() => undefined);
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr("");
    if (mode === "register" && pw !== pw2) {
      setErr(t("l.mismatch"));
      return;
    }
    if (mode === "register" && terms?.billing && !agree) {
      setErr(t("b.must"));
      return;
    }
    setBusy(true);
    try {
      if (mode === "forgot") {
        await api.forgot(email);
        setInfo(t("l.reset_sent"));
        return;
      }
      if (mode === "login") await api.login(email, pw);
      else await api.register(email, pw, name, lang, agree, coupon.trim());
      window.location.href = "/me";
    } catch (e) {
      setErr(e instanceof ApiError ? srv(e.message) : t("c.error"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto flex min-h-[75vh] max-w-sm flex-col justify-center px-4">
      <div className="mb-4 flex items-center justify-between">
        <div className="flex items-baseline gap-2">
          <span className="text-2xl font-bold tracking-tight">سرب</span>
          <span className="text-sm text-muted">Sirb</span>
        </div>
        <LangSwitch />
      </div>
      <form className="card flex flex-col gap-3 p-6" onSubmit={submit}>
        <h1 className="flex items-center gap-2 text-base font-semibold">
          {mode === "login" ? <LogIn className="size-4 text-accent" /> : <UserPlus className="size-4 text-accent" />}
          {mode === "login" ? t("l.signin") : mode === "forgot" ? t("l.forgot_title") : t("l.signup_title")}
        </h1>
        {mode === "forgot" && <p className="text-xs text-muted">{t("l.forgot_hint")}</p>}
        {mode === "register" && <p className="text-xs text-muted">{t("l.signup_hint")}</p>}
        {mode === "register" && (
          <input className={input} placeholder={t("l.name")} value={name} maxLength={80} autoComplete="name"
            onChange={(e) => setName(e.target.value)} />
        )}
        <input className={input} type="email" dir="ltr" placeholder={t("l.email")} value={email} required
          autoComplete="email" onChange={(e) => setEmail(e.target.value)} />
        {mode !== "forgot" && (
          <input className={input} type="password" placeholder={t("l.password")} value={pw} required
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            minLength={mode === "register" ? 10 : undefined} onChange={(e) => setPw(e.target.value)} />
        )}
        {mode === "register" && (
          <input className={input} type="password" placeholder={t("l.repeat")} value={pw2}
            required autoComplete="new-password" onChange={(e) => setPw2(e.target.value)} />
        )}
        {mode === "register" && terms?.billing && (
          <input className={input} dir="ltr" placeholder={t("b.coupon")} value={coupon} maxLength={32}
            autoComplete="off" onChange={(e) => setCoupon(e.target.value)} />
        )}
        {mode === "register" && terms?.billing && (
          <label className="flex items-start gap-2 rounded-md border border-line p-2.5 text-xs leading-relaxed text-muted">
            <input type="checkbox" className="mt-0.5" checked={agree} onChange={(e) => setAgree(e.target.checked)} />
            <span>{t("b.terms", { fee: terms.fee_usd, pct: terms.profit_pct, trial: terms.trial_days, grace: terms.grace_days })}</span>
          </label>
        )}
        {err && <p className="text-xs text-bear">{err}</p>}
        {info && <p className="text-xs text-bull">{info}</p>}
        <button type="submit" disabled={busy || (mode === "forgot" && !!info)}
          className="rounded-md bg-accent px-3 py-2 text-sm font-semibold text-carbon disabled:opacity-50">
          {mode === "login" ? t("l.signin") : mode === "forgot" ? t("l.send_link") : t("l.signup")}
        </button>
        {mode === "login" && (
          <button type="button" className="text-xs text-muted hover:text-ink"
            onClick={() => { setMode("forgot"); setErr(""); setInfo(""); }}>
            {t("l.forgot")}
          </button>
        )}
        <button type="button" className="text-xs text-muted hover:text-ink"
          onClick={() => {
            setMode(mode === "login" ? "register" : "login");
            setErr("");
            setInfo("");
          }}>
          {mode === "login" ? t("l.to_signup") : t("l.to_signin")}
        </button>
      </form>
    </main>
  );
}

export function LoginView() {
  return (
    <I18nProvider>
      <Login />
    </I18nProvider>
  );
}
