"use client";

import { LogIn, UserPlus } from "lucide-react";
import { useEffect, useState } from "react";

import { api, ApiError } from "@/lib/api";
import { SirbLockup, SirbMark } from "@/components/brand/Logo";
import { I18nProvider, LangSwitch, useT } from "@/lib/i18n";
import type { PublicTerms } from "@/lib/types";

const input =
  "w-full rounded-xl border border-line bg-carbon px-3.5 py-2.5 text-sm outline-none transition focus:border-mint/60 focus:ring-2 focus:ring-mint/15";

function Login() {
  const { t, srv, lang } = useT();
  const [mode, setMode] = useState<"login" | "register" | "forgot">("login");
  useEffect(() => {
    if (new URLSearchParams(window.location.search).get("signup")) setMode("register");
  }, []);
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
    <main className="grid min-h-dvh lg:grid-cols-2">
      <section className="flex flex-col px-4 py-6 sm:px-10">
      <div className="mb-4 flex items-center justify-between">
        <SirbLockup size={28} href="/" />
        <LangSwitch />
      </div>
      <div className="flex flex-1 items-center justify-center py-8">
      <form className="card flex w-full max-w-[420px] animate-rise flex-col gap-3.5 p-7 shadow-[0_24px_80px_-32px_rgb(0_0_0/0.8)]" onSubmit={submit}>
        <h1 className="flex items-center gap-2 text-xl font-semibold">
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
          className="rounded-xl bg-mint px-3 py-2.5 text-sm font-semibold text-carbon transition hover:brightness-110 disabled:opacity-50">
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
      </div>
      </section>
      <BrandPanel />
    </main>
  );
}

function BrandPanel() {
  const { lang } = useT();
  const ar = lang === "ar";
  return (
    <aside className="relative hidden overflow-hidden border-s border-line bg-[radial-gradient(90%_70%_at_50%_30%,rgb(43_228_167/0.14),transparent_65%)] bg-surface lg:flex lg:flex-col lg:justify-end lg:p-14">
      <div className="absolute inset-0 opacity-30 [background-image:radial-gradient(rgb(255_255_255/0.08)_1px,transparent_1px)] [background-size:22px_22px]" />
      <div className="absolute inset-x-0 top-[18%] flex justify-center">
        <SirbMark size={300} animated />
      </div>
      <blockquote className="relative max-w-[460px]">
        <p className="font-display text-2xl leading-snug font-medium text-ink">
          {ar ? "«القائد بيشتري، والسرب بيتبعه في ثواني.»" : "“The leader buys. The flock follows in seconds.”"}
        </p>
        <p className="mt-3 text-muted">
          {ar ? "نسخ صفقات المحافظ الذكية على سولانا — بمحفظتك وبحدودك." : "Copy the smart wallets on Solana — in your own wallet, within your limits."}
        </p>
      </blockquote>
    </aside>
  );
}

export function LoginView() {
  return (
    <I18nProvider>
      <Login />
    </I18nProvider>
  );
}
