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

export function PageHeader({ icon, title, subtitle, badges, actions }: {
  icon?: React.ReactNode;
  title: string;
  subtitle?: React.ReactNode;
  badges?: React.ReactNode;
  actions?: React.ReactNode;
}) {
  return (
    <header className="mb-6 flex flex-wrap items-start gap-x-4 gap-y-3">
      {icon && <span className="grid size-10 shrink-0 place-items-center rounded-xl border border-line bg-surface text-mint">{icon}</span>}
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-xl font-semibold tracking-tight sm:text-2xl">{title}</h1>
          {badges}
        </div>
        {subtitle && <div className="mt-1 text-sm text-muted">{subtitle}</div>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}

/** KPI tile: small uppercase label, big mono value, muted sub line. */
export function StatTile({ label, value, sub, tone, icon, className = "" }: {
  label: React.ReactNode;
  value: React.ReactNode;
  sub?: React.ReactNode;
  tone?: string;
  icon?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cls("card flex flex-col gap-1.5 p-5 transition hover:border-line-strong", className)}>
      <div className="flex items-center gap-1.5 text-[11px] font-semibold tracking-[0.1em] text-muted uppercase">
        {icon}{label}
      </div>
      <div className={cls("num text-[26px] leading-tight font-medium", tone ?? "text-ink")}>{value}</div>
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
