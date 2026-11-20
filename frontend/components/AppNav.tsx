"use client";

import { usePathname } from "next/navigation";

import { AdminMenu } from "@/components/AdminAuth";
import { SirbLockup } from "@/components/brand/Logo";

/** Operator pages (password-protected) and the public data pages share one top bar. */
const ADMIN = [
  { href: "/admin", label: "Overview" },
  { href: "/accounts", label: "Accounts" },
  { href: "/live", label: "Live" },
  { href: "/scanner", label: "Scanner" },
  { href: "/performance", label: "Performance" },
  { href: "/paper", label: "Paper" },
];
const PUBLIC = [
  { href: "/scanner", label: "Scanner" },
  { href: "/performance", label: "Performance" },
  { href: "/paper", label: "Paper" },
];

export function AppNav({ kind = "admin", right }: { kind?: "admin" | "public"; right?: React.ReactNode }) {
  const path = usePathname();
  const items = kind === "admin" ? ADMIN : PUBLIC;
  return (
    <nav className="glass sticky top-0 z-40" aria-label="Main">
      <div className="mx-auto flex max-w-[1600px] items-center gap-4 px-4 py-2.5 sm:gap-6 sm:px-6">
        <SirbLockup size={26} href={kind === "admin" ? "/admin" : "/"} sub={false} />
        <div className="-mx-1 flex min-w-0 flex-1 gap-1 overflow-x-auto px-1">
          {items.map((it) => {
            const on = path === it.href || (it.href !== "/" && path?.startsWith(it.href + "/"));
            return (
              <a key={it.href} href={it.href} aria-current={on ? "page" : undefined}
                className={`shrink-0 rounded-lg px-3 py-1.5 text-sm transition-colors ${
                  on ? "bg-raised font-medium text-ink" : "text-muted hover:bg-raised/60 hover:text-ink"}`}>
                {it.label}
              </a>
            );
          })}
        </div>
        {right}
        {kind === "admin" && <AdminMenu />}
        {kind === "public" && (
          <span className="flex shrink-0 items-center gap-2">
            <a href="/login" className="hidden rounded-lg px-3 py-1.5 text-sm text-muted hover:text-ink sm:inline">Sign in</a>
            <a href="/login?signup=1"
              className="rounded-lg bg-mint px-3 py-1.5 text-sm font-semibold text-carbon transition hover:brightness-110">
              Start free
            </a>
          </span>
        )}
      </div>
    </nav>
  );
}
