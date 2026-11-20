"use client";

import { KeyRound } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { useState } from "react";

import { api, ApiError } from "@/lib/api";

const input =
  "w-full rounded-md border border-line bg-carbon px-3 py-2 text-sm outline-none focus:border-accent";

export function ResetView() {
  const token = useSearchParams().get("token") ?? "";
  const [pw, setPw] = useState("");
  const [pw2, setPw2] = useState("");
  const [err, setErr] = useState("");
  const [done, setDone] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);

  return (
    <main className="mx-auto flex min-h-[75vh] max-w-sm flex-col justify-center px-4">
      <form className="card flex flex-col gap-3 p-6"
        onSubmit={async (e) => {
          e.preventDefault();
          setErr("");
          if (pw !== pw2) return setErr("The two passwords are different.");
          setBusy(true);
          try {
            const r = await api.resetPassword(token, pw);
            setDone(r.withdraw_lock_hours);
          } catch (e) {
            setErr(e instanceof ApiError ? e.message : "Something went wrong");
          } finally {
            setBusy(false);
          }
        }}>
        <h1 className="flex items-center gap-2 text-base font-semibold">
          <KeyRound className="size-4 text-accent" /> Choose a new password
        </h1>
        {done != null ? (
          <>
            <p className="text-sm text-bull">Your password is changed and you are signed in.</p>
            <p className="text-xs text-muted">For your safety, withdrawals stay locked for {done} hours.</p>
            <a href="/me" className="rounded-md bg-accent px-3 py-2 text-center text-sm font-semibold text-carbon">
              Go to my trading
            </a>
          </>
        ) : !token ? (
          <p className="text-sm text-bear">This link has no token. Ask for a new one on the sign-in page.</p>
        ) : (
          <>
            <input className={input} type="password" placeholder="New password (10+ characters)" value={pw}
              autoComplete="new-password" minLength={10} required onChange={(e) => setPw(e.target.value)} />
            <input className={input} type="password" placeholder="Repeat it" value={pw2}
              autoComplete="new-password" required onChange={(e) => setPw2(e.target.value)} />
            {err && <p className="text-xs text-bear">{err}</p>}
            <button type="submit" disabled={busy}
              className="rounded-md bg-accent px-3 py-2 text-sm font-semibold text-carbon disabled:opacity-50">
              Save new password
            </button>
          </>
        )}
      </form>
    </main>
  );
}
