import { scoreTone } from "@/lib/format";

const TONE = {
  bull: { stroke: "var(--color-bull)", text: "text-bull", glow: "drop-shadow(0 0 6px rgb(43 228 167 / 0.45))" },
  warn: { stroke: "var(--color-warn)", text: "text-warn", glow: "drop-shadow(0 0 6px rgb(245 158 11 / 0.35))" },
  bear: { stroke: "var(--color-bear)", text: "text-bear", glow: "none" },
};

interface Props {
  score: number;
  size?: number;
  stroke?: number;
  label?: string;
  className?: string;
}

/** Circular 0-100 gauge. 270° arc so the number sits in an open "dial". */
export function ScoreGauge({ score, size = 40, stroke, label, className = "" }: Props) {
  const s = Math.max(0, Math.min(100, Math.round(score)));
  const tone = TONE[scoreTone(s)];
  const w = stroke ?? Math.max(3, size / 11);
  const r = (size - w) / 2;
  const c = 2 * Math.PI * r;
  const arc = c * 0.75;
  const filled = arc * (s / 100);
  const large = size >= 80;

  return (
    <div
      className={`relative inline-grid place-items-center ${className}`}
      style={{ width: size, height: size }}
      role="meter"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={s}
      aria-label={label ?? `Alpha Score ${s}`}
    >
      <svg width={size} height={size} className="-rotate-[225deg]" style={{ filter: s >= 60 ? tone.glow : "none" }}>
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke="rgb(255 255 255 / 0.08)"
          strokeWidth={w}
          strokeDasharray={`${arc} ${c}`}
          strokeLinecap="round"
        />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={tone.stroke}
          strokeWidth={w}
          strokeDasharray={`${filled} ${c}`}
          strokeLinecap="round"
          className="transition-[stroke-dasharray] duration-700 ease-out"
        />
      </svg>
      <div className="absolute inset-0 grid place-items-center">
        <div className="text-center leading-none">
          <span className={`num font-semibold ${tone.text}`} style={{ fontSize: large ? size * 0.3 : size * 0.34 }}>
            {s}
          </span>
          {large && <div className="mt-1 text-[10px] tracking-[0.18em] text-muted uppercase">/ 100</div>}
        </div>
      </div>
    </div>
  );
}
