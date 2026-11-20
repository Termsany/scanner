/**
 * Sirb (سرب) brand mark: a flock in V-formation. The gold chevron is the leader (the smart wallet),
 * the mint ones are the followers; together they read as an upward arrow. Gold is for the leader only.
 */
export function SirbMark({ size = 32, animated = false, className = "" }: {
  size?: number;
  animated?: boolean;
  className?: string;
}) {
  const h = Math.round((size * 52) / 64);
  const bird = (d: string, color: string, i: number, w = 5, op = 1) => (
    <path d={d} stroke={color} strokeOpacity={op} strokeWidth={w} strokeLinecap="round" strokeLinejoin="round"
      className={animated ? "sirb-bird" : undefined} style={animated ? { animationDelay: `${i * 0.18}s` } : undefined} />
  );
  return (
    <svg width={size} height={h} viewBox="0 0 64 52" fill="none" role="img" aria-label="Sirb" className={className}>
      {bird("M24 14 L32 7 L40 14", "var(--color-gold)", 0)}
      {bird("M12 26 L19 20 L26 26", "var(--color-mint)", 1)}
      {bird("M38 26 L45 20 L52 26", "var(--color-mint)", 1)}
      {bird("M2 38 L8 33 L14 38", "var(--color-mint)", 2, 4.5, 0.45)}
      {bird("M50 38 L56 33 L62 38", "var(--color-mint)", 2, 4.5, 0.45)}
    </svg>
  );
}

/** Mark + "سرب" wordmark (+ small SIRB). `href` makes the whole lockup a link. */
export function SirbLockup({ size = 28, href, sub = true, className = "" }: {
  size?: number;
  href?: string;
  sub?: boolean;
  className?: string;
}) {
  const inner = (
    <span className={`inline-flex items-center gap-2 ${className}`} dir="ltr">
      <SirbMark size={size} />
      <span className="font-display text-[1.35em] leading-none font-bold tracking-tight text-ink" dir="rtl">سرب</span>
      {sub && <span className="text-[0.62em] font-semibold tracking-[0.3em] text-muted">SIRB</span>}
    </span>
  );
  return href ? <a href={href} aria-label="Sirb home" className="rounded-md">{inner}</a> : inner;
}
