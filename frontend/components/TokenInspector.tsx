"use client";

import {
  AlertTriangle,
  Check,
  CircleHelp,
  Copy,
  ExternalLink,
  Minus,
  OctagonAlert,
  ShieldCheck,
  X,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { ApiError, api } from "@/lib/api";
import { CHAIN_LABEL, age, explorerUrl, pct, pctClass, price, scoreTone, shortAddr, usd } from "@/lib/format";
import type { CategoryScore, ScoreComponent, SecurityReport, TokenDetail, TokenRef } from "@/lib/types";

import { AIAnalystCard } from "./AIAnalystCard";
import { AlphaBadge, ChainPill, TokenAvatar } from "./Badges";
import { PriceChart } from "./PriceChart";
import { ScoreGauge } from "./ScoreGauge";

interface Props {
  token: TokenRef | null;
  refreshKey: number; // bumps on scan_complete so the open drawer stays current
  onClose: () => void;
}

export function TokenInspector({ token, refreshKey, onClose }: Props) {
  const [detail, setDetail] = useState<TokenDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const open = token !== null;

  useEffect(() => {
    if (!token) return;
    const ctl = new AbortController();
    api
      .token(token.chain, token.address, ctl.signal)
      .then((d) => {
        setDetail(d);
        setError(null);
      })
      .catch((e) => {
        if (e.name === "AbortError") return;
        setError(e instanceof ApiError && e.status === 404 ? "This token has not been scanned yet." : "Could not load token.");
      });
    return () => ctl.abort();
  }, [token, refreshKey]);

  // Reset when switching tokens so stale data never shows under a new header.
  useEffect(() => {
    setDetail(null);
    setError(null);
  }, [token?.chain, token?.address]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeRef.current?.focus();
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [open, onClose]);

  if (!token) return null;
  const t = detail?.token;
  const m = detail?.metrics;
  const s = detail?.score;

  return (
    <div className="fixed inset-0 z-40" role="dialog" aria-modal="true" aria-label="Token inspector">
      <div className="absolute inset-0 animate-fade-in bg-black/55 backdrop-blur-[2px]" onClick={onClose} />
      <aside className="absolute inset-y-0 right-0 flex w-full max-w-[560px] animate-slide-in flex-col border-l border-line bg-carbon shadow-2xl shadow-black/60">
        {/* Header */}
        <header className="flex flex-wrap items-center gap-3 border-b border-line px-5 py-4">
          {t ? (
            <TokenAvatar symbol={t.symbol} image={t.image_url} size={40} />
          ) : (
            <span className="skeleton size-10 rounded-full" />
          )}
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <h2 className="truncate text-lg font-semibold">{t ? `$${t.symbol}` : <span className="skeleton inline-block h-5 w-24" />}</h2>
              <ChainPill chain={token.chain} />
              {t && <AlphaBadge badge={t.badge} />}
            </div>
            <div className="mt-0.5 flex items-center gap-2 text-xs text-faint">
              <span className="truncate">{t?.name ?? " "}</span>
              <CopyAddress address={token.address} />
            </div>
          </div>
          {t && (
            <div className="order-last flex w-full items-center justify-end gap-1.5 sm:order-none sm:w-auto">
              {t.pair_url && (
                <a href={t.pair_url} target="_blank" rel="noopener noreferrer" title="Open on DexScreener"
                   className="grid h-9 place-items-center rounded-lg border border-line px-2 text-[11px] text-muted hover:bg-raised hover:text-ink">
                  <span className="flex items-center gap-1">DEX <ExternalLink className="size-3" /></span>
                </a>
              )}
              <a href={explorerUrl(token.chain, token.address)} target="_blank" rel="noopener noreferrer"
                 title={`Open on ${CHAIN_LABEL[token.chain]} explorer`}
                 className="grid h-9 place-items-center rounded-lg border border-line px-2 text-[11px] text-muted hover:bg-raised hover:text-ink">
                <span className="flex items-center gap-1">Scan <ExternalLink className="size-3" /></span>
              </a>
              <span className={`num grid h-9 min-w-12 place-items-center rounded-lg border px-2 text-lg font-bold ${SCORE_BOX[scoreTone(t.alpha_score)]}`}
                    title="Alpha Score">
                {t.alpha_score}
              </span>
            </div>
          )}
          <button
            ref={closeRef}
            onClick={onClose}
            aria-label="Close inspector"
            className="grid size-9 place-items-center rounded-lg border border-line text-muted hover:bg-raised hover:text-ink"
          >
            <X className="size-4" />
          </button>
        </header>

        <div className="flex-1 space-y-4 overflow-y-auto px-5 py-5">
          {error ? (
            <div className="card p-6 text-center text-sm text-muted">{error}</div>
          ) : (
            <>
              {/* Score hero */}
              <section className="card flex items-center gap-5 p-4">
                {s ? <ScoreGauge score={s.total} size={112} stroke={9} /> : <span className="skeleton size-28 rounded-full" />}
                <div className="min-w-0 flex-1 space-y-2.5">
                  {s ? (
                    <>
                      <CategoryBar cat={s.security} />
                      <CategoryBar cat={s.momentum} />
                      <CategoryBar cat={s.smart_money} />
                    </>
                  ) : (
                    [0, 1, 2].map((i) => <span key={i} className="skeleton block h-7 w-full" />)
                  )}
                </div>
              </section>

              {s?.hard_fail && (
                <div className="flex items-start gap-2.5 rounded-xl border border-bear/40 bg-bear-soft px-4 py-3 text-sm text-bear">
                  <OctagonAlert className="mt-0.5 size-4 shrink-0" />
                  <div>
                    <div className="font-semibold">Hard fail: {s.hard_fail.replaceAll("_", " ").toLowerCase()}</div>
                    <div className="text-xs text-bear/80">Score forced to 0 regardless of momentum.</div>
                  </div>
                </div>
              )}

              {s && s.data_gaps.length > 0 && (
                <div className="flex items-start gap-2.5 rounded-xl border border-warn/30 bg-warn-soft px-4 py-2.5 text-xs text-warn">
                  <AlertTriangle className="mt-px size-3.5 shrink-0" />
                  <div>
                    <span className="font-semibold">Missing data</span> (scored as 0, never as safe):{" "}
                    {s.data_gaps.map((g) => g.split(".").pop()!.replaceAll("_", " ")).join(", ")}
                  </div>
                </div>
              )}

              {/* Chart */}
              <PriceChart chain={token.chain} address={token.address} />

              {/* Market stats */}
              <section className="card grid grid-cols-2 gap-px overflow-hidden bg-line sm:grid-cols-4" aria-label="Market stats">
                {[
                  ["Price", price(m?.price_usd)],
                  ["Market cap", usd(m?.market_cap ?? m?.fdv)],
                  ["Liquidity", usd(m?.liquidity_usd)],
                  ["Age", age(m?.pair_created_at)],
                  ["Vol 1h", usd(m?.volume_h1)],
                  ["Vol 24h", usd(m?.volume_h24)],
                  ["Buys / Sells 1h", m ? `${m.buys_h1 ?? "—"} / ${m.sells_h1 ?? "—"}` : "—"],
                  ["FDV", usd(m?.fdv)],
                ].map(([k, v]) => (
                  <div key={k} className="bg-surface px-3 py-2.5">
                    <div className="text-[10px] tracking-[0.12em] text-faint uppercase">{k}</div>
                    <div className="num mt-0.5 text-sm">{m ? v : <span className="skeleton inline-block h-4 w-16" />}</div>
                  </div>
                ))}
                {(["m5", "h1", "h6", "h24"] as const).map((w) => {
                  const v = m?.[`price_change_${w}` as const] ?? null;
                  return (
                    <div key={w} className="bg-surface px-3 py-2.5">
                      <div className="text-[10px] tracking-[0.12em] text-faint uppercase">Δ {WINDOW_LABEL[w]}</div>
                      <div className={`num mt-0.5 text-sm ${pctClass(v)}`}>{pct(v)}</div>
                    </div>
                  );
                })}
              </section>

              {/* AI */}
              <AIAnalystCard
                chain={token.chain}
                address={token.address}
                scoreKey={s ? `${s.total}:${s.scored_at}` : "pending"}
              />

              {/* Score breakdown */}
              {s && (
                <section className="card p-4" aria-label="Score breakdown">
                  <h3 className="text-sm font-semibold">Score breakdown</h3>
                  <p className="mt-0.5 text-[11px] text-faint">
                    Deterministic v{s.version}. Missing data never earns points.
                  </p>
                  {[s.security, s.momentum, s.smart_money].map((cat) => (
                    <div key={cat.name} className="mt-4">
                      <div className="flex items-baseline justify-between text-[11px] font-medium tracking-[0.12em] text-muted uppercase">
                        <span>{cat.name}</span>
                        <span className="num">
                          {fmtPts(cat.points)} / {cat.max_points}
                        </span>
                      </div>
                      <ul className="mt-2 divide-y divide-line/70 rounded-lg border border-line">
                        {cat.components.map((c) => (
                          <ComponentRow key={c.key} c={c} />
                        ))}
                      </ul>
                    </div>
                  ))}
                </section>
              )}

              {/* Security */}
              {detail && <SecurityChecklist sec={detail.security} />}

              {/* Links */}
              {t && (
                <div className="flex flex-wrap gap-2 pb-2">
                  {t.pair_url && <LinkBtn href={t.pair_url}>DexScreener</LinkBtn>}
                  <LinkBtn href={explorerUrl(token.chain, token.address)}>{CHAIN_LABEL[token.chain]} explorer</LinkBtn>
                  {token.chain === "solana" && <LinkBtn href={`https://rugcheck.xyz/tokens/${token.address}`}>RugCheck</LinkBtn>}
                </div>
              )}
            </>
          )}
        </div>
      </aside>
    </div>
  );
}

const SCORE_BOX = {
  bull: "border-bull/40 bg-bull-soft text-bull",
  warn: "border-warn/40 bg-warn-soft text-warn",
  bear: "border-bear/40 bg-bear-soft text-bear",
} as const;

const WINDOW_LABEL = { m5: "5m", h1: "1h", h6: "6h", h24: "24h" } as const;

function fmtPts(n: number) {
  return Number.isInteger(n) ? String(n) : n.toFixed(1);
}

function CategoryBar({ cat }: { cat: CategoryScore }) {
  const ratio = cat.max_points ? cat.points / cat.max_points : 0;
  const color = ratio >= 0.75 ? "bg-bull" : ratio >= 0.45 ? "bg-warn" : "bg-bear";
  return (
    <div>
      <div className="flex items-baseline justify-between text-xs">
        <span className="text-muted">{cat.name}</span>
        <span className="num text-ink">
          {fmtPts(cat.points)}
          <span className="text-faint">/{cat.max_points}</span>
        </span>
      </div>
      <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-white/[0.06]">
        <div className={`h-full rounded-full ${color} transition-[width] duration-700`} style={{ width: `${ratio * 100}%` }} />
      </div>
    </div>
  );
}

function ComponentRow({ c }: { c: ScoreComponent }) {
  const partial = !c.passed && c.points > 0;
  const Icon = c.passed ? Check : partial ? Minus : X;
  const tone = c.passed ? "text-bull bg-bull-soft" : partial ? "text-warn bg-warn-soft" : "text-bear bg-bear-soft";
  return (
    <li className="flex items-start gap-3 px-3 py-2.5">
      <span className={`mt-0.5 grid size-5 shrink-0 place-items-center rounded ${tone}`}>
        <Icon className="size-3" strokeWidth={3} />
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-[13px]">{c.label}</div>
        {c.detail && <div className="text-[11.5px] text-faint">{c.detail}</div>}
      </div>
      <span className="num shrink-0 text-xs text-muted">
        +{fmtPts(c.points)}
        <span className="text-faint">/{c.max_points}</span>
      </span>
    </li>
  );
}

type CheckState = "ok" | "bad" | "unknown";

function SecurityChecklist({ sec }: { sec: SecurityReport | null }) {
  if (!sec) {
    return (
      <section className="card p-4">
        <h3 className="flex items-center gap-2 text-sm font-semibold">
          <ShieldCheck className="size-4 text-muted" /> Security audit
        </h3>
        <p className="mt-2 text-xs text-faint">
          Not audited yet. GoPlus/RugCheck results arrive within a few scan cycles.
        </p>
      </section>
    );
  }
  const flag = (v: boolean | null, goodWhen: boolean): CheckState => (v === null ? "unknown" : v === goodWhen ? "ok" : "bad");
  const maxTax = sec.buy_tax === null && sec.sell_tax === null ? null : Math.max(sec.buy_tax ?? 0, sec.sell_tax ?? 0);

  const rows: [string, CheckState, string][] = [
    ["Honeypot", flag(sec.is_honeypot, false), sec.is_honeypot === null ? "Unknown" : sec.is_honeypot ? "Cannot sell" : "Sellable"],
    ["Mint authority", flag(sec.is_mintable, false), sec.is_mintable === null ? "Unknown" : sec.is_mintable ? "Active" : "Renounced"],
    ["Freeze / blacklist", flag(sec.is_freezable, false), sec.is_freezable === null ? "Unknown" : sec.is_freezable ? "Active" : "None"],
    [
      "LP locked / burned",
      sec.lp_locked_pct === null ? "unknown" : sec.lp_locked_pct > 80 ? "ok" : "bad",
      sec.lp_locked_pct === null ? "Unknown" : `${sec.lp_locked_pct.toFixed(1)}%`,
    ],
    [
      "Top 10 holders",
      sec.top10_holders_pct === null ? "unknown" : sec.top10_holders_pct < 30 ? "ok" : "bad",
      sec.top10_holders_pct === null ? "Unknown" : `${sec.top10_holders_pct.toFixed(1)}%`,
    ],
    [
      "Buy / sell tax",
      maxTax === null ? "unknown" : maxTax <= 10 ? "ok" : "bad",
      maxTax === null ? "n/a" : `${sec.buy_tax ?? 0}% / ${sec.sell_tax ?? 0}%`,
    ],
  ];
  const rugged = sec.risks.some((r) => r.name.toLowerCase() === "rugged" && r.level === "danger");
  rows.push([
    "Rugged (RugCheck)",
    !sec.providers.includes("rugcheck") ? "unknown" : rugged ? "bad" : "ok",
    !sec.providers.includes("rugcheck") ? "n/a" : rugged ? "RUGGED" : "No",
  ]);
  if (sec.is_open_source !== null) rows.push(["Verified source", flag(sec.is_open_source, true), sec.is_open_source ? "Yes" : "No"]);
  if (sec.owner_can_change_balance !== null)
    rows.push(["Owner can edit balances", flag(sec.owner_can_change_balance, false), sec.owner_can_change_balance ? "Yes" : "No"]);

  const holders = sec.top_holders.slice(0, 6);
  const notableRisks = sec.risks.filter((r) => r.level !== "info").slice(0, 8);

  return (
    <section className="card p-4" aria-label="Security checklist">
      <div className="flex items-center gap-2">
        <ShieldCheck className="size-4 text-bull" />
        <h3 className="text-sm font-semibold">Security audit</h3>
        <span className="ml-auto text-[10px] tracking-wider text-faint uppercase">{sec.providers.join(" + ")}</span>
      </div>
      <ul className="mt-3 grid gap-x-6 sm:grid-cols-2">
        {rows.map(([label, state, value]) => (
          <li key={label} className="flex items-center gap-2.5 border-b border-line/60 py-2 text-[13px]">
            <StateIcon state={state} />
            <span className="text-muted">{label}</span>
            <span className={`num ml-auto text-xs ${state === "bad" ? "text-bear" : state === "ok" ? "text-ink" : "text-faint"}`}>{value}</span>
          </li>
        ))}
      </ul>

      {notableRisks.length > 0 && (
        <div className="mt-4">
          <h4 className="text-[11px] font-medium tracking-[0.12em] text-muted uppercase">Flags</h4>
          <ul className="mt-2 space-y-1.5">
            {notableRisks.map((r, i) => (
              <li key={i} className="flex items-start gap-2 text-xs">
                <span className={`mt-1 size-1.5 shrink-0 rounded-full ${r.level === "danger" ? "bg-bear" : "bg-warn"}`} />
                <span>
                  <span className="text-ink">{r.name}</span>
                  {r.description && <span className="text-faint"> · {r.description}</span>}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {holders.length > 0 && (
        <div className="mt-4">
          <h4 className="text-[11px] font-medium tracking-[0.12em] text-muted uppercase">Top holders</h4>
          <ul className="mt-2 space-y-1.5">
            {holders.map((h) => (
              <li key={h.address} className="flex items-center gap-2 text-xs">
                <span className="num w-24 shrink-0 text-muted">{shortAddr(h.address)}</span>
                <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-white/[0.05]">
                  <span
                    className={`block h-full rounded-full ${h.is_locked ? "bg-accent/50" : "bg-muted/60"}`}
                    style={{ width: `${Math.min(100, h.pct)}%` }}
                  />
                </span>
                <span className="num w-14 text-right">{h.pct.toFixed(2)}%</span>
                <span className="w-12 text-[10px] text-faint">{h.is_locked ? h.tag ?? "pool" : h.tag ?? ""}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function StateIcon({ state }: { state: CheckState }) {
  if (state === "ok") return <Check className="size-3.5 shrink-0 text-bull" strokeWidth={3} />;
  if (state === "bad") return <X className="size-3.5 shrink-0 text-bear" strokeWidth={3} />;
  return <CircleHelp className="size-3.5 shrink-0 text-faint" />;
}

function CopyAddress({ address }: { address: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(address);
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        } catch {
          /* clipboard blocked */
        }
      }}
      className="num inline-flex items-center gap-1 rounded px-1 text-faint hover:bg-raised hover:text-ink"
      title={address}
    >
      {shortAddr(address)}
      {copied ? <Check className="size-3 text-bull" /> : <Copy className="size-3" />}
    </button>
  );
}

function LinkBtn({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex items-center gap-1.5 rounded-lg border border-line px-3 py-1.5 text-xs text-muted hover:bg-raised hover:text-ink"
    >
      {children}
      <ExternalLink className="size-3" />
    </a>
  );
}
