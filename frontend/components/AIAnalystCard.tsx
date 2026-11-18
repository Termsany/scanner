"use client";

import { Bot, RefreshCw, Sparkles } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { ApiError, api } from "@/lib/api";
import type { AIAnalysis, Chain } from "@/lib/types";

const VERDICT = {
  BULLISH: "bg-bull-soft text-bull border-bull/30",
  NEUTRAL: "bg-warn-soft text-warn border-warn/30",
  AVOID: "bg-bear-soft text-bear border-bear/30",
};

const BULLET_LABEL = ["Reward", "Risk", "Watch"];

export function AIAnalystCard({ chain, address, scoreKey }: { chain: Chain; address: string; scoreKey: string }) {
  const [data, setData] = useState<AIAnalysis | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const run = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      setError(null);
      try {
        setData(await api.analyze(chain, address, signal));
      } catch (e) {
        if ((e as Error).name === "AbortError") return;
        setError(e instanceof ApiError && e.status === 429 ? "Rate limit reached, try again in a minute." : "Analysis unavailable.");
      } finally {
        setLoading(false);
      }
    },
    [chain, address],
  );

  // Re-run when the token or its latest score changes.
  useEffect(() => {
    setData(null);
    const ctl = new AbortController();
    run(ctl.signal);
    return () => ctl.abort();
  }, [run, scoreKey]);

  return (
    <section className="card relative overflow-hidden p-4" aria-label="AI analyst summary">
      <div
        className="pointer-events-none absolute -top-16 -right-16 size-40 rounded-full bg-accent/10 blur-3xl"
        aria-hidden
      />
      <div className="flex items-center gap-2">
        <Sparkles className="size-4 text-accent" />
        <h3 className="text-sm font-semibold">AI Analyst</h3>
        {data && (
          <span className={`ml-1 rounded-full border px-2 py-0.5 text-[10px] font-bold tracking-wider ${VERDICT[data.verdict]}`}>
            {data.verdict}
          </span>
        )}
        <button
          onClick={() => run()}
          disabled={loading}
          className="ml-auto grid size-7 place-items-center rounded-md text-faint hover:bg-raised hover:text-ink disabled:opacity-40"
          aria-label="Regenerate analysis"
        >
          <RefreshCw className={`size-3.5 ${loading ? "animate-spin" : ""}`} />
        </button>
      </div>

      {error ? (
        <p className="mt-3 text-xs text-bear">{error}</p>
      ) : !data ? (
        <div className="mt-4 space-y-2.5">
          <span className="skeleton block h-3.5 w-11/12" />
          <span className="skeleton block h-3.5 w-10/12" />
          <span className="skeleton block h-3.5 w-9/12" />
        </div>
      ) : (
        <>
          <ul className="mt-3 space-y-2.5">
            {data.bullets.map((b, i) => (
              <li key={i} className="flex gap-3 text-[13px] leading-relaxed text-ink/90">
                <span className="mt-0.5 w-12 shrink-0 text-[10px] font-semibold tracking-wider text-faint uppercase">
                  {BULLET_LABEL[i] ?? "Note"}
                </span>
                <span>{b.replace(/^(Strength|Risk|Watch):\s*/i, "")}</span>
              </li>
            ))}
          </ul>
          <div className="mt-4 flex items-center gap-3 text-[11px] text-faint">
            <span className="flex items-center gap-1">
              <Bot className="size-3" />
              {data.model === "rules-v1" ? "Rule engine (no LLM key)" : data.model}
            </span>
            <span className="flex flex-1 items-center gap-2">
              Confidence
              <span className="h-1 flex-1 overflow-hidden rounded-full bg-white/5">
                <span className="block h-full rounded-full bg-accent/70" style={{ width: `${Math.round(data.confidence * 100)}%` }} />
              </span>
              <span className="num">{Math.round(data.confidence * 100)}%</span>
            </span>
          </div>
          <p className="mt-2 text-[10px] text-faint">{data.disclaimer}</p>
        </>
      )}
    </section>
  );
}
