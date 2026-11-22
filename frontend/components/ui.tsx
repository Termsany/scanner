/**
 * Sirb UI kit for the operator and data pages (matches the "Admin overview" screen in Figma):
 * page header, KPI stat tile, panel (card with a header row), badge, and button classes.
 * Tables use the `data-table` utility from globals.css.
 */

export const btn = {
  base: "inline-flex items-center justify-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition disabled:opacity-50",
  primary: "bg-mint text-carbon font-semibold hover:brightness-110",
  secondary: "border border-line bg-raised text-ink hover:border-line-strong",
  ghost: "text-muted hover:bg-raised hover:text-ink",
  danger: "border border-bear/40 text-bear hover:bg-bear-soft",
  good: "border border-mint/40 text-mint hover:bg-mint-soft",
};
export const cls = (...v: (string | false | null | undefined)[]) => v.filter(Boolean).join(" ");
export const button = (kind: keyof typeof btn = "secondary", extra = "") => cls(btn.base, btn[kind], extra);

export const field =
  "w-full rounded-lg border border-line bg-carbon px-3 py-2 text-sm outline-none transition focus:border-mint/60 focus:ring-2 focus:ring-mint/15";

type Tone = "mint" | "gold" | "bear" | "sky" | "muted";
const TONE: Record<Tone, string> = {
  mint: "bg-mint-soft text-mint",
  gold: "bg-gold-soft text-gold",
  bear: "bg-bear-soft text-bear",
  sky: "bg-sky/15 text-sky",
  muted: "bg-raised text-muted",
};

export function Badge({ tone = "muted", children, dot, className = "" }: {
  tone?: Tone;
  children: React.ReactNode;
  dot?: boolean;
  className?: string;
}) {
  return (
    <span className={cls("inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[11px] font-semibold whitespace-nowrap", TONE[tone], className)}>
      {dot && <span className="size-1.5 rounded-full bg-current" />}
      {children}
    </span>
  );
}

/** One width for the top bar and every page, so nothing jumps between pages. */
export const page = "mx-auto w-full max-w-[1440px] px-4 sm:px-6";
/** A page's <main>: shared width and spacing. */
export const pageMain = `${page} pt-6 pb-28 sm:pb-16`;   // room for the phone tab bar

export function PageHeader({ icon, title, subtitle, badges, actions }: {
  icon?: React.ReactNode;
  title: string;
  subtitle?: React.ReactNode;
  badges?: React.ReactNode;
  actions?: React.ReactNode;
}) {
  return (
    <header className="mb-6 flex animate-fade-in flex-wrap items-center gap-x-4 gap-y-3">
      {icon && (
        <span className="grid size-11 shrink-0 place-items-center rounded-2xl border border-mint/25 bg-mint-soft text-mint shadow-[0_0_30px_-8px_rgb(43_228_167/0.6)] [&>svg]:size-5">
          {icon}
        </span>
      )}
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="font-display text-xl font-semibold tracking-tight sm:text-2xl">{title}</h1>
          {badges}
        </div>
        {subtitle && <div className="mt-0.5 text-sm text-muted">{subtitle}</div>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}

/** On/off switch that acts at once (no Save button). */
export function Toggle({ on, busy, onClick, label }: { on: boolean; busy?: boolean; onClick: () => void; label: string }) {
  return (
    <button type="button" role="switch" aria-checked={on} aria-label={label} disabled={busy} onClick={onClick}
      className={cls("relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition disabled:opacity-50",
        on ? "bg-mint" : "bg-raised ring-1 ring-line-strong")}>
      <span className={cls("inline-block size-4 rounded-full bg-ink shadow transition", on ? "translate-x-[18px]" : "translate-x-0.5")} />
    </button>
  );
}

/** Small pill switch (time windows, chart types). */
export function Segmented<T extends string | number>({ value, options, onChange, label, className = "" }: {
  value: T;
  options: { value: T; label: React.ReactNode }[];
  onChange: (v: T) => void;
  label?: string;
  className?: string;
}) {
  return (
    <div role="radiogroup" aria-label={label} className={cls("flex rounded-lg border border-line bg-carbon/60 p-0.5", className)}>
      {options.map((o) => (
        <button key={String(o.value)} type="button" role="radio" aria-checked={value === o.value} onClick={() => onChange(o.value)}
          className={cls("num rounded-md px-2.5 py-1 text-xs font-medium transition",
            value === o.value ? "bg-raised text-ink shadow-sm" : "text-muted hover:text-ink")}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** Sticky section tabs under the page header (same look as the top bar). */
export function SectionTabs<T extends string>({ tabs, value, onChange, label = "Sections" }: {
  tabs: { key: T; label: string; icon?: React.ReactNode; count?: number }[];
  value: T;
  onChange: (v: T) => void;
  label?: string;
}) {
  return (
    <nav className="glass sticky top-[57px] z-30 -mx-4 mb-6 border-y border-line px-4 sm:mx-0 sm:rounded-2xl sm:border sm:px-1.5"
      aria-label={label}>
      <div className="flex gap-1 overflow-x-auto py-1.5 [scrollbar-width:none]">
        {tabs.map((x) => {
          const on = value === x.key;
          return (
            <button key={x.key} type="button" onClick={() => onChange(x.key)} aria-current={on ? "page" : undefined}
              className={cls("relative inline-flex shrink-0 items-center gap-2 rounded-xl px-3.5 py-2 text-sm font-medium transition [&>svg]:size-4",
                on ? "bg-raised text-ink shadow-[inset_0_0_0_1px_rgb(255_255_255/0.08)]" : "text-muted hover:bg-raised/60 hover:text-ink")}>
              {x.icon}{x.label}
              {!!x.count && <span className="num grid min-w-5 place-items-center rounded-full bg-bear px-1 text-[10px] font-bold text-carbon">{x.count}</span>}
              {on && <span className="absolute inset-x-3 -bottom-1.5 h-[2px] rounded-full bg-mint" />}
            </button>
          );
        })}
      </div>
    </nav>
  );
}

/** KPI tile: icon chip and uppercase label, big mono value, muted sub line; lifts and glows on hover. */
export function StatTile({ label, value, sub, tone, icon, className = "" }: {
  label: React.ReactNode;
  value: React.ReactNode;
  sub?: React.ReactNode;
  tone?: string;
  icon?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cls("card group relative flex flex-col gap-1.5 overflow-hidden p-5 transition duration-300 hover:-translate-y-0.5 hover:border-line-strong", className)}>
      <div className="pointer-events-none absolute -top-16 -right-16 size-40 rounded-full bg-mint/10 opacity-0 blur-3xl transition duration-500 group-hover:opacity-100" />
      <div className="flex items-center gap-2 text-[11px] font-semibold tracking-[0.1em] text-muted uppercase">
        {icon && <span className="grid size-6 place-items-center rounded-md bg-raised text-ink [&>svg]:size-3.5">{icon}</span>}
        {label}
      </div>
      <div className={cls("num mt-1 text-[26px] leading-tight font-medium", tone ?? "text-ink")}>{value}</div>
      {sub && <div className="text-xs leading-relaxed text-muted">{sub}</div>}
    </div>
  );
}

/** Card with a header row (icon, title, meta, actions) and a body; `flush` removes body padding for tables. */
export function Panel({ icon, title, meta, actions, children, flush, className = "", id }: {
  icon?: React.ReactNode;
  title: React.ReactNode;
  meta?: React.ReactNode;
  actions?: React.ReactNode;
  children?: React.ReactNode;
  flush?: boolean;
  className?: string;
  id?: string;
}) {
  return (
    <section id={id} className={cls("card overflow-hidden", className)}>
      <header className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-line px-5 py-4">
        {icon && <span className="text-mint [&>svg]:size-4">{icon}</span>}
        <h2 className="text-[15px] font-semibold tracking-tight">{title}</h2>
        {meta && <span className="text-xs text-muted">{meta}</span>}
        {actions && <div className="ms-auto flex flex-wrap items-center gap-2">{actions}</div>}
      </header>
      {children != null && <div className={flush ? "" : "p-5"}>{children}</div>}
    </section>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <p className="px-5 py-8 text-center text-sm text-muted">{children}</p>;
}

/** Password gate shared by the operator pages. */
export function LockScreen({ title, hint, value, onChange, onSubmit, error }: {
  title: string;
  hint: string;
  value: string;
  onChange: (v: string) => void;
  onSubmit: () => void;
  error?: string | null;
}) {
  return (
    <main className="mx-auto flex min-h-[75vh] max-w-sm flex-col justify-center px-4">
      <form className="card flex animate-rise flex-col gap-3.5 p-7"
        onSubmit={(e) => {
          e.preventDefault();
          if (value) onSubmit();
        }}>
        <h1 className="text-lg font-semibold">{title}</h1>
        <p className="text-sm text-muted">{hint}</p>
        <input type="password" autoComplete="current-password" value={value} onChange={(e) => onChange(e.target.value)}
          placeholder="Password" className={field} />
        {error && <p className="text-xs text-bear">{error}</p>}
        <button type="submit" className={button("primary", "py-2.5")}>Unlock</button>
      </form>
    </main>
  );
}
