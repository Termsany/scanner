"use client";

import { Bot, Cpu, RefreshCw, Sparkles } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { ApiError, api } from "@/lib/api";
import type { AIAnalysis, Chain } from "@/lib/types";

const VERDICT = {
  BULLISH: "bg-bull-soft text-bull border-bull/30",
  NEUTRAL: "bg-warn-soft text-warn border-warn/30",
  AVOID: "bg-bear-soft text-bear border-bear/30",
};
const BULLET_LABEL = ["Reward", "Risk", "Watch"];

/** On-demand analysis: nothing is requested until the user asks (LLM calls cost money and quota). */
export function AIAnalystCard({ chain, address, scoreKey }: { chain: Chain; address: string; scoreKey: string }) {
  const [data, setData] = useState<AIAnalysis | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [analysedKey, setAnalysedKey] = useState<string | null>(null);
  const ctl = useRef<AbortController | null>(null);

  // New token: clear the previous analysis.
  useEffect(() => {
    ctl.current?.abort();
    setData(null);
    setError(null);
    setLoading(false);
    setAnalysedKey(null);
  }, [chain, address]);

  useEffect(() => () => ctl.current?.abort(), []);

  const run = async () => {
    ctl.current?.abort();
    const c = new AbortController();
    ctl.current = c;
    setLoading(true);
    setError(null);
    try {
      setData(await api.analyze(chain, address, c.signal));
      setAnalysedKey(scoreKey);
    } catch (e) {
      if ((e as Error).name === "AbortError") return;
      setError(e instanceof ApiError && e.status === 429 ? "Rate limit reached, try again in a minute." : "Analysis unavailable.");
    } finally {
      if (ctl.current === c) setLoading(false);
    }
  };

  const isRules = data?.model.startsWith("rules");
  const stale = data && analysedKey !== null && analysedKey !== scoreKey;

  return (
    <section className="card relative overflow-clip p-4" aria-label="AI analyst summary" aria-busy={loading}>
      <div className="pointer-events-none absolute -top-16 -right-16 size-40 rounded-full bg-accent/15 blur-3xl" aria-hidden />
      <div className="flex items-center gap-2">
        <Sparkles className="size-4 text-accent" />
        <h3 className="text-sm font-semibold">AI Token Analyst</h3>
        {data && (
          <span className={`rounded-full border px-2 py-0.5 text-[10px] font-bold tracking-wider ${VERDICT[data.verdict]}`}>
            {data.verdict}
          </span>
        )}
        {data && (
          <button
            onClick={run}
            disabled={loading}
            className="ml-auto grid size-7 place-items-center rounded-md text-faint hover:bg-raised hover:text-ink disabled:opacity-40"
            aria-label="Run analysis again"
            title="Run again"
          >
            <RefreshCw className={`size-3.5 ${loading ? "animate-spin" : ""}`} />
          </button>
        )}
      </div>

      {!data && !loading && !error && (
        <div className="mt-3 flex items-center justify-between gap-3">
          <p className="text-xs text-faint">Reward, main risk and invalidation level from this token&apos;s metrics.</p>
          <button
            onClick={run}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-accent/40 bg-accent-soft px-3 py-1.5 text-xs font-semibold text-accent hover:bg-accent/25"
          >
            <Sparkles className="size-3.5" /> Analyze
          </button>
        </div>
      )}

      {loading && !data && (
        <div className="mt-4 space-y-2.5" aria-label="Analysing">
          <span className="skeleton block h-3.5 w-11/12" />
          <span className="skeleton block h-3.5 w-10/12" />
          <span className="skeleton block h-3.5 w-9/12" />
        </div>
      )}

      {error && (
        <div className="mt-3 flex items-center gap-3 text-xs">
          <span className="text-bear">{error}</span>
          <button onClick={run} className="text-muted underline underline-offset-2 hover:text-ink">
            Retry
          </button>
        </div>
      )}

      {data && (
        <div className={loading ? "opacity-50 transition-opacity" : ""}>
          <ul className="mt-3 space-y-2.5">
            {data.bullets.map((b, i) => (
              <li key={i} className="flex gap-3 text-[13px] leading-relaxed text-ink/90">
                <span
                  className={`mt-0.5 w-12 shrink-0 text-[10px] font-semibold tracking-wider uppercase ${
                    i === 1 ? "text-bear/80" : i === 0 ? "text-bull/80" : "text-faint"
                  }`}
                >
                  {BULLET_LABEL[i] ?? "Note"}
                </span>
                <span>{b.replace(/^(Strength|Risk|Watch):\s*/i, "")}</span>
              </li>
            ))}
          </ul>
          <div className="mt-4 flex flex-wrap items-center gap-3 text-[11px] text-faint">
            <span
              className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-semibold ${
                isRules ? "border-line text-muted" : "border-accent/40 bg-accent-soft text-accent"
              }`}
              title={data.model}
            >
              {isRules ? <Cpu className="size-3" /> : <Bot className="size-3" />}
              {isRules ? "rules-v1" : `LLM · ${data.model.replace(/ \+ rules$/, "").split("/").pop()}`}
              {data.model.endsWith("+ rules") && " + rules"}
            </span>
            <span className="flex flex-1 items-center gap-2">
              Confidence
              <span className="h-1 min-w-12 flex-1 overflow-hidden rounded-full bg-white/5">
                <span className="block h-full rounded-full bg-accent/70" style={{ width: `${Math.round(data.confidence * 100)}%` }} />
              </span>
              <span className="num">{Math.round(data.confidence * 100)}%</span>
            </span>
          </div>
          {stale && <p className="mt-2 text-[11px] text-warn">Score changed since this analysis; run it again for fresh numbers.</p>}
          <p className="mt-2 text-[10px] text-faint">{data.disclaimer}</p>
        </div>
      )}
    </section>
  );
}
