"use client";

import { Radio } from "lucide-react";

import { ago, pct, pctClass, price, usd } from "@/lib/format";
import type { SocketState } from "@/lib/useLiveSignals";
import type { FeedSignal, TokenRef } from "@/lib/types";

import { ChainPill } from "./Badges";
import { ScoreGauge } from "./ScoreGauge";

interface Props {
  feed: FeedSignal[];
  fresh: Set<number>;
  socket: SocketState;
  minScore: number;
  onMinScore: (n: number) => void;
  onOpen: (t: TokenRef) => void;
  now: number;
  wide?: boolean;      // the Signals tab: full width, cards in a grid
}

const THRESHOLDS = [60, 75, 80, 90];

export function LiveFeed({ feed, fresh, socket, minScore, onMinScore, onOpen, now, wide }: Props) {
  return (
    <section className={`card flex flex-col ${wide ? "" : "max-h-[calc(100dvh-420px)] min-h-[300px]"}`} aria-label="Live signals">
      <div className="flex items-center gap-2 border-b border-line px-4 py-3">
        <Radio className={`size-4 ${socket === "open" ? "text-bull" : "text-faint"}`} />
        <h2 className="text-sm font-semibold">Live signals</h2>
        {wide && <span className="text-xs text-muted">click one to open the token</span>}
        <div role="radiogroup" aria-label="Minimum signal score" className="ml-auto flex rounded-md border border-line p-0.5">
          {THRESHOLDS.map((v) => (
            <button
              key={v}
              role="radio"
              aria-checked={minScore === v}
              onClick={() => onMinScore(v)}
              className={`num rounded px-1.5 py-0.5 text-[10.5px] ${minScore === v ? "bg-raised text-ink" : "text-faint hover:text-ink"}`}
            >
              {v}+
            </button>
          ))}
        </div>
      </div>

      <ol className={wide ? "grid gap-3 p-4 sm:grid-cols-2 xl:grid-cols-3" : "flex-1 divide-y divide-line/70 overflow-y-auto"}
        aria-live="polite">
        {feed.length === 0 ? (
          <li className="px-4 py-10 text-center text-xs text-faint sm:col-span-full">
            {socket === "open" ? `No ${minScore}+ signals yet. Waiting for the next one…` : "Connecting to the signal stream…"}
          </li>
        ) : (
          feed.map((s) => (
            <li
              key={s.signal_id ?? `${s.chain}:${s.address}:${s.created_at}`}
              className={`${s.signal_id && fresh.has(s.signal_id) ? "flash-up" : ""} ${
                wide ? "overflow-hidden rounded-xl border border-line bg-surface-2/60 transition hover:-translate-y-0.5 hover:border-line-strong" : ""}`}
            >
              <button
                onClick={() => onOpen({ chain: s.chain, address: s.address })}
                className="flex w-full items-start gap-3 px-4 py-3 text-left transition-colors hover:bg-white/[0.025]"
              >
                <ScoreGauge score={s.score} size={36} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <span className="truncate font-semibold">${s.symbol}</span>
                    <ChainPill chain={s.chain} />
                    <span className="num ml-auto shrink-0 text-[10.5px] text-faint">{ago(s.created_at, now)}</span>
                  </div>
                  <div className="num mt-0.5 flex flex-wrap gap-x-2 text-[11.5px] text-muted">
                    <span>{price(s.price_usd)}</span>
                    <span>Liq {usd(s.liquidity_usd)}</span>
                    {s.gain_pct != null ? (
                      <span className={pctClass(s.gain_pct)} title="Price change since the signal fired">
                        since {pct(s.gain_pct)}
                      </span>
                    ) : (
                      <span className={pctClass(s.price_change_h1)}>1h {pct(s.price_change_h1)}</span>
                    )}
                  </div>
                  {s.top_reasons?.[0] && <div className="mt-1 truncate text-[11px] text-faint">{s.top_reasons[0]}</div>}
                </div>
              </button>
            </li>
          ))
        )}
      </ol>
    </section>
  );
}
