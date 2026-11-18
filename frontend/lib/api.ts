import type {
  AIAnalysis,
  Chain,
  ChartData,
  ChartInterval,
  PaginatedTokens,
  ScannerStatus,
  SmartWalletList,
  TokenDetail,
  TokenQuery,
} from "./types";

// Empty string = same origin (production behind nginx: /api and /ws are proxied to the backend).
export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

export function wsUrl(): string {
  if (process.env.NEXT_PUBLIC_WS_URL) return process.env.NEXT_PUBLIC_WS_URL;
  if (API_URL) return `${API_URL.replace(/^http/, "ws")}/ws/live-signals`;
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}/ws/live-signals`;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit & { signal?: AbortSignal }): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, {
      ...init,
      headers: { Accept: "application/json", ...(init?.body ? { "Content-Type": "application/json" } : {}) },
      cache: "no-store",
    });
  } catch (err) {
    if ((err as Error).name === "AbortError") throw err;
    throw new ApiError(0, "Scanner API unreachable");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

function qs(params: Record<string, string | number | undefined>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== "" && v !== null) p.set(k, String(v));
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}

export const api = {
  tokens: (q: TokenQuery, signal?: AbortSignal) =>
    request<PaginatedTokens>(`/api/tokens${qs({ ...q })}`, { signal }),

  token: (chain: Chain, address: string, signal?: AbortSignal) =>
    request<TokenDetail>(`/api/tokens/${chain}/${encodeURIComponent(address)}`, { signal }),

  chart: (chain: Chain, address: string, interval: ChartInterval, signal?: AbortSignal) =>
    request<ChartData>(
      `/api/tokens/${chain}/${encodeURIComponent(address)}/chart${qs({ interval, limit: 300 })}`,
      { signal },
    ),

  status: (signal?: AbortSignal) => request<ScannerStatus>("/api/status", { signal }),

  smartWallets: (signal?: AbortSignal) => request<SmartWalletList>("/api/smart-wallets?limit=50", { signal }),

  analyze: (chain: Chain, address: string, signal?: AbortSignal) =>
    request<AIAnalysis>("/api/ai-analyze", {
      method: "POST",
      body: JSON.stringify({ chain, address }),
      signal,
    }),
};
