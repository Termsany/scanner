"use client";

import { Radio } from "lucide-react";

import { ago, pct, pctClass, price, usd } from "@/lib/format";
import type { SocketState } from "@/lib/useLiveSignals";
import type { LiveSignal, TokenRef } from "@/lib/types";

import { ChainPill } from "./Badges";
import { ScoreGauge } from "./ScoreGauge";

interface Props {
  feed: LiveSignal[];
  socket: SocketState;
  minScore: number;
  onMinScore: (n: number) => void;
  onOpen: (t: TokenRef) => void;
  now: number;
}

export function LiveFeed({ feed, socket, minScore, onMinScore, onOpen, now }: Props) {
  return (
    <section className="card flex max-h-[calc(100dvh-220px)] min-h-[320px] flex-col" aria-label="Live signals">
      <div className="flex items-center gap-2 border-b border-line px-4 py-3">
        <Radio className={`size-4 ${socket === "open" ? "text-bull" : "text-faint"}`} />
        <h2 className="text-sm font-semibold">Live signals</h2>
        <label className="ml-auto flex items-center gap-1.5 text-[11px] text-muted">
          ≥
          <select
            value={minScore}
            onChange={(e) => onMinScore(Number(e.target.value))}
            className="num cursor-pointer rounded border border-line bg-carbon/60 px-1 py-0.5 text-ink outline-none"
            aria-label="Minimum signal score"
          >
            {[60, 75, 80, 90].map((v) => (
              <option key={v} value={v} className="bg-surface">
                {v}
              </option>
            ))}
          </select>
        </label>
      </div>

      <ol className="flex-1 divide-y divide-line/70 overflow-y-auto" aria-live="polite">
        {feed.length === 0 ? (
          <li className="px-4 py-10 text-center text-xs text-faint">
            {socket === "open" ? "Waiting for the next high-score token…" : "Connecting to the signal stream…"}
          </li>
        ) : (
          feed.map((s) => (
            <li key={s.signal_id ?? `${s.chain}:${s.address}:${s.created_at}`}>
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
                  <div className="num mt-0.5 flex gap-2 text-[11.5px] text-muted">
                    <span>{price(s.price_usd)}</span>
                    <span className={pctClass(s.price_change_h1)}>1h {pct(s.price_change_h1)}</span>
                    <span>Liq {usd(s.liquidity_usd)}</span>
                  </div>
                  {s.top_reasons[0] && <div className="mt-1 truncate text-[11px] text-faint">{s.top_reasons[0]}</div>}
                </div>
              </button>
            </li>
          ))
        )}
      </ol>
    </section>
  );
}
