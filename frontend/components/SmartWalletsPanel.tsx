"use client";

import { ExternalLink, Eye, Users } from "lucide-react";
import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { shortAddr, usd } from "@/lib/format";
import type { Chain, SmartWalletList } from "@/lib/types";

import { ChainPill } from "./Badges";

function walletUrl(chain: Chain, address: string): string {
  switch (chain) {
    case "solana":
      return `https://solscan.io/account/${address}`;
    case "ethereum":
      return `https://etherscan.io/address/${address}`;
    case "base":
      return `https://basescan.org/address/${address}`;
    case "bsc":
      return `https://bscscan.com/address/${address}`;
  }
}

/** Read-only view of tracked wallets. Managed on the server with `python -m app.smart_money`. */
export function SmartWalletsPanel({ refreshKey }: { refreshKey: number }) {
  const [data, setData] = useState<SmartWalletList | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const ctl = new AbortController();
    api
      .smartWallets(ctl.signal)
      .then((d) => {
        setData(d);
        setFailed(false);
      })
      .catch((e) => e.name !== "AbortError" && setFailed(true));
    return () => ctl.abort();
  }, [refreshKey]);

  const total = data ? data.items.length : 0;

  return (
    <section className="card mt-4 flex max-h-[420px] flex-col" aria-label="Smart wallets">
      <div className="flex items-center gap-2 border-b border-line px-4 py-3">
        <Users className="size-4 text-accent" />
        <h2 className="text-sm font-semibold">Smart wallets</h2>
        {data && (
          <span className="num ml-auto text-[11px] text-faint">
            {data.enabled_manual} manual · {data.enabled_auto} auto · {data.enabled_birdeye} birdeye
          </span>
        )}
      </div>

      {failed ? (
        <p className="px-4 py-6 text-center text-xs text-faint">Wallet list unavailable.</p>
      ) : !data ? (
        <div className="space-y-2 p-4">
          {[0, 1, 2].map((i) => (
            <span key={i} className="skeleton block h-8 w-full" />
          ))}
        </div>
      ) : total === 0 ? (
        <p className="px-4 py-6 text-xs leading-relaxed text-faint">
          No tracked wallets yet. Wallets are added automatically: profitable traders verified with Birdeye
          (30-day win rate and realized profit), and holders of several signalled tokens that rallied 50%+.
        </p>
      ) : (
        <ul className="flex-1 divide-y divide-line/70 overflow-y-auto">
          {data.items.map((w) => (
            <li key={`${w.chain}:${w.address}`} className="flex items-center gap-2.5 px-4 py-2.5 text-xs">
              <ChainPill chain={w.chain} />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5">
                  <span className="truncate font-medium text-ink">{w.label || shortAddr(w.address)}</span>
                  <span
                    className={`rounded px-1 text-[9.5px] font-semibold tracking-wider uppercase ${
                      w.source === "manual"
                        ? "bg-accent/15 text-accent"
                        : w.source === "birdeye"
                          ? "bg-cyan/15 text-cyan"
                          : "bg-bull-soft text-bull"
                    }`}
                  >
                    {w.source}
                  </span>
                  {w.track_buys && (
                    <span title="Telegram buy alerts on" className="inline-flex items-center gap-0.5 rounded bg-warn-soft px-1 text-[9.5px] font-semibold tracking-wider text-warn uppercase">
                      <Eye className="size-2.5" /> tracked
                    </span>
                  )}
                </div>
                <a
                  href={walletUrl(w.chain, w.address)}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="num inline-flex items-center gap-1 text-faint hover:text-ink"
                  title={w.address}
                >
                  {shortAddr(w.address)} <ExternalLink className="size-2.5" />
                </a>
              </div>
              {w.stats?.win_rate != null ? (
                <span className="num shrink-0 text-right text-muted" title={`${w.stats.trades_30d ?? "?"} trades in 30 days`}>
                  {Math.round(w.stats.win_rate * 100)}%
                  <span className="block text-[10px] text-faint">
                    {usd(w.stats.realized_usd_30d ?? 0)} 30d
                  </span>
                </span>
              ) : w.appearances > 0 && (
                <span
                  className="num shrink-0 text-right text-muted"
                  title={w.win_tokens.map((t) => `${t.symbol} +${t.gain_pct}%`).join(", ")}
                >
                  {w.wins}/{w.appearances}
                  <span className="block text-[10px] text-faint">wins</span>
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
