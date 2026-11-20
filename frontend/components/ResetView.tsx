"use client";

import { KeyRound } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { useState } from "react";

import { api, ApiError } from "@/lib/api";
import { I18nProvider, LangSwitch, useT } from "@/lib/i18n";

const input =
  "w-full rounded-md border border-line bg-carbon px-3 py-2 text-sm outline-none focus:border-accent";

function Reset() {
  const { t, srv } = useT();
  const token = useSearchParams().get("token") ?? "";
  const [pw, setPw] = useState("");
  const [pw2, setPw2] = useState("");
  const [err, setErr] = useState("");
  const [done, setDone] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);

  return (
    <main className="mx-auto flex min-h-[75vh] max-w-sm flex-col justify-center px-4">
      <div className="mb-3 flex justify-end"><LangSwitch /></div>
      <form className="card flex flex-col gap-3 p-6"
        onSubmit={async (e) => {
          e.preventDefault();
          setErr("");
          if (pw !== pw2) return setErr(t("l.mismatch"));
          setBusy(true);
          try {
            const r = await api.resetPassword(token, pw);
            setDone(r.withdraw_lock_hours);
          } catch (e) {
            setErr(e instanceof ApiError ? srv(e.message) : t("c.error"));
          } finally {
            setBusy(false);
          }
        }}>
        <h1 className="flex items-center gap-2 text-base font-semibold">
          <KeyRound className="size-4 text-accent" /> {t("r.title")}
        </h1>
        {done != null ? (
          <>
            <p className="text-sm text-bull">{t("r.done")}</p>
            <p className="text-xs text-muted">{t("r.locked", { h: done })}</p>
            <a href="/me" className="rounded-md bg-accent px-3 py-2 text-center text-sm font-semibold text-carbon">
              {t("r.go")}
            </a>
          </>
        ) : !token ? (
          <p className="text-sm text-bear">{t("r.no_token")}</p>
        ) : (
          <>
            <input className={input} type="password" placeholder={t("r.new")} value={pw}
              autoComplete="new-password" minLength={10} required onChange={(e) => setPw(e.target.value)} />
            <input className={input} type="password" placeholder={t("r.repeat")} value={pw2}
              autoComplete="new-password" required onChange={(e) => setPw2(e.target.value)} />
            {err && <p className="text-xs text-bear">{err}</p>}
            <button type="submit" disabled={busy}
              className="rounded-md bg-accent px-3 py-2 text-sm font-semibold text-carbon disabled:opacity-50">
              {t("r.save")}
            </button>
          </>
        )}
      </form>
    </main>
  );
}

export function ResetView() {
  return (
    <I18nProvider>
      <Reset />
    </I18nProvider>
  );
}
