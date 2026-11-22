"use client";

import { Activity, BarChart3, FlaskConical, LayoutDashboard, Radar, Users } from "lucide-react";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { AdminMenu, adminWho } from "@/components/AdminAuth";
import { SirbLockup } from "@/components/brand/Logo";
import { page } from "@/components/ui";

/** Operator pages (admin sign-in) and the public data pages share one top bar. */
const ADMIN = [
  { href: "/admin", label: "Overview", icon: <LayoutDashboard /> },
  { href: "/accounts", label: "Accounts", icon: <Users /> },
  { href: "/live", label: "Live", icon: <Activity /> },
  { href: "/scanner", label: "Scanner", icon: <Radar /> },
  { href: "/performance", label: "Performance", short: "Stats", icon: <BarChart3 /> },
  { href: "/paper", label: "Paper", icon: <FlaskConical /> },
];
const PUBLIC = ADMIN.slice(3);
const SEEN = "sirb-admin-nav"; // remembered for the tab, so the admin bar does not flicker between pages

function rememberedAdmin(): boolean {
  try {
    return sessionStorage.getItem(SEEN) === "1";
  } catch {
    return false;
  }
}

export function AppNav({ kind = "admin", right }: { kind?: "admin" | "public"; right?: React.ReactNode }) {
  const path = usePathname();
  // a public page opened by a signed-in admin keeps the full admin bar
  const [admin, setAdmin] = useState(kind === "admin");
  useEffect(() => {
    if (kind === "admin") return;
    if (rememberedAdmin()) setAdmin(true);
    let alive = true;
    adminWho()
      .then(() => {
        if (!alive) return;
        setAdmin(true);
        try { sessionStorage.setItem(SEEN, "1"); } catch { /* storage unavailable */ }
      })
      .catch(() => {
        if (!alive) return;
        setAdmin(false);
        try { sessionStorage.removeItem(SEEN); } catch { /* storage unavailable */ }
      });
    return () => {
      alive = false;
    };
  }, [kind]);
  const items = admin ? ADMIN : PUBLIC;
  const isOn = (href: string) => path === href || (href !== "/" && !!path?.startsWith(href + "/"));
  const current = items.find((it) => isOn(it.href));
  return (
    <>
    <nav className="glass sticky top-0 z-40 border-b border-line" aria-label="Main">
      <div className={`${page} flex h-14 items-center gap-4 sm:gap-6`}>
        <SirbLockup size={26} href={admin ? "/admin" : "/"} sub={false} />
        <span className="min-w-0 flex-1 truncate text-sm font-medium text-ink sm:hidden">{current?.label}</span>
        <div className="-mx-1 hidden min-w-0 flex-1 gap-1 overflow-x-auto px-1 [scrollbar-width:none] sm:flex">
          {items.map((it) => {
            const on = isOn(it.href);
            return (
              <a key={it.href} href={it.href} aria-current={on ? "page" : undefined}
                className={`relative inline-flex shrink-0 items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm transition-colors [&>svg]:size-4 ${
                  on ? "bg-raised font-medium text-ink" : "text-muted hover:bg-raised/60 hover:text-ink"}`}>
                <span className={`inline-flex [&>svg]:size-4 ${on ? "text-mint" : ""}`}>{it.icon}</span>
                {it.label}
                {on && <span className="absolute inset-x-3 -bottom-3 h-[2px] rounded-full bg-mint" />}
              </a>
            );
          })}
        </div>
        {right}
        {admin && <AdminMenu />}
        {!admin && (
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
    {/* phones: the pages as a bottom tab bar, like an app */}
    <nav className="glass fixed inset-x-0 bottom-0 z-40 border-t border-line pb-[env(safe-area-inset-bottom)] sm:hidden"
      aria-label="Pages">
      <div className="grid" style={{ gridTemplateColumns: `repeat(${items.length}, minmax(0, 1fr))` }}>
        {items.map((it) => {
          const on = isOn(it.href);
          return (
            <a key={it.href} href={it.href} aria-current={on ? "page" : undefined}
              className={`relative flex flex-col items-center gap-1 py-2 text-[10px] font-medium transition-colors [&>svg]:size-5 ${
                on ? "text-mint" : "text-muted active:text-ink"}`}>
              {on && <span className="absolute inset-x-4 top-0 h-[2px] rounded-full bg-mint" />}
              {it.icon}
              <span className="max-w-full truncate px-0.5">{"short" in it && it.short ? it.short : it.label}</span>
            </a>
          );
        })}
      </div>
    </nav>
    </>
  );
}
