import type {
  AIAnalysis,
  Chain,
  ChartData,
  ChartInterval,
  PaginatedTokens,
  LiveStatus,
  AccountsList,
  Me,
  MySettings,
  SiteUser,
  AuditEntry,
  TradingAccountCfg,
  PaperSummary,
  Performance,
  ScannerStatus,
  SignalOut,
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
      headers: {
        Accept: "application/json",
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...((init?.headers as Record<string, string>) ?? {}),
      },
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

  performance: (hours: number, signal?: AbortSignal) =>
    request<Performance>(`/api/performance${qs({ hours })}`, { signal }),

  paper: (hours: number, signal?: AbortSignal) =>
    request<PaperSummary>(`/api/paper${qs({ hours })}`, { signal }),

  live: (password: string, signal?: AbortSignal) =>
    request<LiveStatus>("/api/live", { headers: { "X-Live-Password": password }, signal }),

  livePause: (password: string, paused: boolean) =>
    request<{ paused: boolean }>("/api/live/pause", {
      method: "POST",
      headers: { "X-Live-Password": password },
      body: JSON.stringify({ paused }),
    }),

  liveSell: (password: string, id: number, share: number) =>
    request<{ queued: boolean; message: string }>(`/api/live/positions/${id}/sell`, {
      method: "POST",
      headers: { "X-Live-Password": password },
      body: JSON.stringify({ share }),
    }),

  accounts: (password: string, signal?: AbortSignal) =>
    request<AccountsList>("/api/live/accounts", { headers: { "X-Live-Password": password }, signal }),

  createAccount: (password: string, body: Record<string, unknown>) =>
    request<{ account: TradingAccountCfg; secret: string | null; note?: string }>("/api/live/accounts", {
      method: "POST",
      headers: { "X-Live-Password": password },
      body: JSON.stringify(body),
    }),

  editAccount: (password: string, name: string, body: Record<string, unknown>) =>
    request<{ account: TradingAccountCfg }>(`/api/live/accounts/${encodeURIComponent(name)}`, {
      method: "PATCH",
      headers: { "X-Live-Password": password },
      body: JSON.stringify(body),
    }),

  // ---- registered users (session cookie) ----
  register: (email: string, password: string, name: string) =>
    request<{ ok: boolean; status: string }>("/api/me/register", {
      method: "POST", credentials: "include", body: JSON.stringify({ email, password, name }),
    }),

  login: (email: string, password: string) =>
    request<{ ok: boolean; status: string }>("/api/me/login", {
      method: "POST", credentials: "include", body: JSON.stringify({ email, password }),
    }),

  logout: () => request<{ ok: boolean }>("/api/me/logout", { method: "POST", credentials: "include" }),

  me: (signal?: AbortSignal) => request<Me>("/api/me", { credentials: "include", signal }),

  mySettings: (body: Record<string, unknown>) =>
    request<{ settings: MySettings }>("/api/me/account", {
      method: "PATCH", credentials: "include", body: JSON.stringify(body),
    }),

  revealKey: (password: string, code: string | null) =>
    request<{ secret: string; pubkey: string }>("/api/me/account/reveal-key", {
      method: "POST", credentials: "include", body: JSON.stringify({ password, code }),
    }),

  withdraw: (password: string, to: string, sol: number | null, code: string) =>
    request<{ ok: boolean; sol: number; signature: string }>("/api/me/account/withdraw", {
      method: "POST", credentials: "include", body: JSON.stringify({ password, to, sol, all: sol == null, code }),
    }),

  forgot: (email: string) =>
    request<{ ok: boolean; message: string }>("/api/me/forgot", { method: "POST", body: JSON.stringify({ email }) }),

  resetPassword: (token: string, password: string) =>
    request<{ ok: boolean; withdraw_lock_hours: number }>("/api/me/reset", {
      method: "POST", credentials: "include", body: JSON.stringify({ token, password }),
    }),

  totpSetup: (password: string) =>
    request<{ secret: string; uri: string; qr: string }>("/api/me/2fa/setup", {
      method: "POST", credentials: "include", body: JSON.stringify({ password }),
    }),

  totpEnable: (code: string) =>
    request<{ ok: boolean }>("/api/me/2fa/enable", {
      method: "POST", credentials: "include", body: JSON.stringify({ code }),
    }),

  totpDisable: (password: string, code: string) =>
    request<{ ok: boolean }>("/api/me/2fa/disable", {
      method: "POST", credentials: "include", body: JSON.stringify({ password, code }),
    }),

  mySell: (id: number, share: number) =>
    request<{ queued: boolean; message: string }>(`/api/me/positions/${id}/sell`, {
      method: "POST", credentials: "include", body: JSON.stringify({ share }),
    }),

  changePassword: (current: string, next: string) =>
    request<{ ok: boolean }>("/api/me/password", {
      method: "POST", credentials: "include", body: JSON.stringify({ current, new: next }),
    }),

  // ---- admin: users ----
  users: (password: string) =>
    request<{ users: SiteUser[] }>("/api/live/users", { headers: { "X-Live-Password": password } }),

  approveUser: (password: string, id: number, body: Record<string, unknown>) =>
    request<{ status: string }>(`/api/live/users/${id}/approve`, {
      method: "POST", headers: { "X-Live-Password": password }, body: JSON.stringify(body),
    }),

  resetLink: (password: string, id: number) =>
    request<{ link: string; minutes: number }>(`/api/live/users/${id}/reset-link`, {
      method: "POST", headers: { "X-Live-Password": password },
    }),

  reset2fa: (password: string, id: number) =>
    request<{ ok: boolean; withdraw_lock_hours: number }>(`/api/live/users/${id}/reset-2fa`, {
      method: "POST", headers: { "X-Live-Password": password },
    }),

  audit: (password: string, q: string, limit = 200) =>
    request<{ entries: AuditEntry[] }>(`/api/live/audit?limit=${limit}&q=${encodeURIComponent(q)}`, {
      headers: { "X-Live-Password": password },
    }),

  blockUser: (password: string, id: number, blocked: boolean) =>
    request<{ status: string }>(`/api/live/users/${id}/block`, {
      method: "POST", headers: { "X-Live-Password": password }, body: JSON.stringify({ blocked }),
    }),

  signals: (minScore: number, limit = 30, signal?: AbortSignal) =>
    request<SignalOut[]>(`/api/signals${qs({ min_score: minScore, limit })}`, { signal }),

  smartWallets: (signal?: AbortSignal) => request<SmartWalletList>("/api/smart-wallets?limit=50", { signal }),

  analyze: (chain: Chain, address: string, signal?: AbortSignal) =>
    request<AIAnalysis>("/api/ai-analyze", {
      method: "POST",
      body: JSON.stringify({ chain, address }),
      signal,
    }),
};
