"""Live copy trading on Solana: buy when a leader wallet buys, sell the same share when it sells.

REAL MONEY when LIVE_MODE=live. Safety rails, all enforced here:
  * LIVE_SIZE_USD per buy, and the cost of all open positions never exceeds LIVE_MAX_OPEN_USD
  * new buys pause after LIVE_MAX_DAILY_LOSS_USD of realized loss in 24h, or with /live off in Telegram
  * a leader buy older than MAX_TRADE_AGE_SECONDS when we see it is skipped (we would buy far too late)
  * tokens below LIVE_MIN_LIQUIDITY_USD are skipped; one position per token
  * sells keep mirroring even while buys are paused, so positions are never stranded
LIVE_MODE=dry runs the same logic against real Jupiter quotes but never signs or sends a transaction.

Swaps go through Jupiter (https://dev.jup.ag/docs/swap-api): quote -> swap transaction -> sign -> send.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

import httpx
from sqlalchemy import Numeric, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models import Chain, LivePosition, TrackerCursor, WalletTrade
from app.repository import utc

log = logging.getLogger("live")

SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS = 1_000_000_000
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
PUMPPORTAL_TRADE = "https://pumpportal.fun/api/trade-local"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
PUMPPORTAL_FEE = 0.005      # PumpPortal's own fee on each trade (included in the simulated SOL spent)
MAX_TRADE_AGE_SECONDS = 180
MAX_SELL_ATTEMPTS = 8
CURSOR = "live:wallet_trades"
MANUAL = "manual"                # leader_wallet of a position the owner opened by hand (no leader to follow)
PAUSED = "live:paused"
PUBKEY = "live:pubkey"
HEARTBEAT = "live:heartbeat"     # + ":<account>": ISO time of the last engine step (see deploy/monitor)


class SwapError(Exception):
    pass


class Dropped(SwapError):
    """The transaction's blockhash expired without it landing: nothing happened on-chain, safe to retry."""


class SlippageError(Dropped):
    """Price moved past the allowed slippage (Jupiter error 6001 / 0x1771): nothing was swapped, retry."""


def _is_slippage(err: object) -> bool:
    s = str(err)
    return "0x1771" in s or "'Custom': 6001" in s or "Custom(6001)" in s


# mints PumpPortal can't buy (non-SOL quote, unsupported pool): Jupiter straight away for a while
_NO_DIRECT: dict[str, float] = {}
NO_DIRECT_SECONDS = 1800.0


class DirectUnavailable(Exception):
    """The direct pump.fun / PumpSwap buy could not even be prepared (no price, no transaction): nothing was
    sent, so the buy falls back to Jupiter."""


class JupiterBetter(Exception):
    """Best route: Jupiter's quote beats the prepared PumpPortal buy after fees. Nothing was sent."""

    def __init__(self, quote: dict[str, Any], edge: float) -> None:
        super().__init__(f"Jupiter {edge * 100:+.2f}% after fees")
        self.quote, self.edge = quote, edge


ATA_RENT_SOL = 0.00203928       # a new token account's rent: both routes pay it on a first buy


def route_edge_buy(pp_tokens: int, pp_sol: float, jup_tokens: int, jup_sol: float) -> float:
    """How much more Jupiter gives per SOL than PumpPortal, all costs included (+0.02 = 2% more tokens)."""
    if pp_tokens <= 0 or pp_sol <= 0 or jup_sol <= 0:
        return 0.0
    return (jup_tokens / jup_sol) / (pp_tokens / pp_sol) - 1


def route_edge_sell(pp_sol_out: float, jup_sol_out: float) -> float:
    """How much more SOL Jupiter brings than PumpPortal for the same tokens, fees included (+ = Jupiter)."""
    if pp_sol_out <= 0:
        return 1.0 if jup_sol_out > 0 else 0.0
    return jup_sol_out / pp_sol_out - 1


class ChaseError(Exception):
    """The price already ran too far past the leader's: skip instead of buying the top."""

    def __init__(self, pay: float, limit: float) -> None:
        super().__init__(f"price ran: would pay {pay:.4g} vs limit {limit:.4g}")
        self.pay, self.limit = pay, limit


# Seconds spent in each step of the buy being made in this task (see JupiterExecutor.buy); None = not timing.
_TIMING: ContextVar[dict[str, float] | None] = ContextVar("_TIMING", default=None)


def _took(step: str, seconds: float) -> None:
    d = _TIMING.get()
    if d is not None:
        d[step] = round(d.get(step, 0.0) + seconds, 2)


@dataclass
class Fill:
    signature: str
    sol: float           # SOL spent (buy) or received (sell)
    tokens_raw: int      # tokens received (buy) or sold (sell)
    sol_price: float     # USD per SOL at the time
    timing: dict[str, float] | None = None   # buy: seconds per step (quote, swap build, confirm, ...)

    @property
    def usd(self) -> float:
        return self.sol * self.sol_price


class Executor(Protocol):
    mode: str
    pubkey: str | None

    async def sol_balance(self) -> float: ...
    async def sol_price(self) -> float: ...
    async def buy(self, mint: str, usd: float, max_price: float | None = None,
                  slippage_bps: int | None = None) -> Fill: ...
    async def sell(self, mint: str, tokens_raw: int) -> Fill: ...
    async def token_balance(self, mint: str) -> int: ...
    async def close(self) -> None: ...
    # optional (live only): token decreases in the wallet's own history, see JupiterExecutor.wallet_sales


def load_keypair(secret: str):
    from solders.keypair import Keypair

    secret = secret.strip()
    if secret.startswith("["):
        return Keypair.from_bytes(bytes(json.loads(secret)))
    return Keypair.from_base58_string(secret)


@dataclass
class Leader:
    """A smart wallet an account copies, with its own options."""
    wallet: str
    label: str = ""
    unconditional: bool = False      # no liquidity / chase / scanner checks
    new_tokens_only: bool = False    # copy only tokens it did not hold before
    size_usd: float | None = None    # entry size for this leader; None = the account's
    enabled: bool = True             # off: no new buys from it; its open positions still follow its sells
    max_chase_pct: float | None = None   # never pay more than this % above its price; None = account default
    quick_exit: bool = False         # its positions sell all at +TP / -SL / time / its first sell (no 2x rule)
    ignore_sells: bool = False       # never sell because it sells (too fast to follow); exits come from the rules
    quick_tp_pct: float | None = None    # quick exit for this wallet: take profit at +%  (None = LIVE_QUICK_TP_PCT)
    quick_sl_pct: float | None = None    # stop loss at -% (a negative number; None = LIVE_QUICK_SL_PCT)
    quick_hours: float | None = None     # time limit in hours (None = LIVE_QUICK_MAX_HOLD_MINUTES)


_FROM_SETTINGS = object()


@dataclass
class Account:
    """One trading wallet with its own budget. "main" uses the LIVE_* settings."""
    name: str
    size_usd: float
    max_open_usd: float
    max_daily_loss_usd: float
    wallets: list[str]
    private_key: str | None = None
    # Per-leader options. Empty (accounts from the environment): LIVE_UNCONDITIONAL_WALLETS etc. apply
    leaders: dict[str, Leader] = field(default_factory=dict)
    paused: bool = False                       # no new buys for this account; sells keep working
    take_capital_at_pct: Any = _FROM_SETTINGS  # None = off
    max_hold_hours: int | None = None          # None = LIVE_MAX_HOLD_HOURS
    user_chat_id: str | None = None            # owner's own Telegram chat (registered users), never told the leader
    user_lang: str = "ar"                      # language of those notes
    curve_strategy: str | None = None          # curve rule traded for real (app/curve_live.py); None = none


# What a user is told about an exit: never which wallet, only what happened
USER_REASON = {"leader_sold": "exit signal", "leader_trim": "partial exit", "take_capital": "capital taken back",
               "sold_by_hand": "sold by hand", "manual": "sold from your page", "max_hold": "max hold time",
               "time_stop": "time limit, no profit", "take_profit": "profit target", "stop_loss": "stop loss",
               "quick_time": "time limit", "your_target": "your target", "your_stop": "your stop loss"}
USER_REASON_AR = {"leader_sold": "إشارة خروج", "leader_trim": "خروج جزئي", "take_capital": "سحب رأس المال",
                  "sold_by_hand": "اتباعت يدوي", "manual": "اتباعت من صفحتك", "max_hold": "انتهت مدة الاحتفاظ",
                  "time_stop": "انتهى الوقت من غير ربح", "take_profit": "وصل هدف الربح", "stop_loss": "وقف الخسارة",
                  "quick_time": "انتهى الوقت", "your_target": "وصل هدفك", "your_stop": "وقف الخسارة بتاعك"}


def user_note(lang: str, key: str, **v) -> str:
    """Texts sent to a registered user's own chat, in their language. Never mention the leader."""
    ar = lang != "en"
    if key == "bought":
        return (f"✅ صفقة جديدة: اشترينا ${v['sym']} بـ ${v['usd']:,.2f}\n{v['tx']}" if ar else
                f"✅ New trade: bought ${v['sym']} for ${v['usd']:,.2f}\n{v['tx']}")
    if key == "sold":
        reason = (USER_REASON_AR if ar else USER_REASON).get(v["reason"], v["reason"].replace("_", " "))
        if v["done"]:
            p = v["pnl"]
            tail = (f"\nالصفقة اتقفلت: {'+' if p >= 0 else '−'}${abs(p):,.2f}" if ar else
                    f"\nClosed: P&L {'+' if p >= 0 else '−'}${abs(p):,.2f}")
        else:
            left = 100 - v["share"] * 100
            tail = f"\nلسه معاك {left:.0f}% من الصفقة" if ar else f"\nStill holding {left:.0f}% of the position"
        icon = "🏁" if v["done"] else "💰"
        return (f"{icon} بعنا {v['share'] * 100:.0f}% من ${v['sym']} بـ ${v['usd']:,.2f} · {reason}{tail}\n{v['tx']}" if ar
                else f"{icon} Sold {v['share'] * 100:.0f}% of ${v['sym']} for ${v['usd']:,.2f} · {reason}{tail}\n{v['tx']}")
    if key == "low_sol":
        return ("⏸ صفقة اتعدت: مفيش SOL كفاية في محفظة التداول. حوّل SOL عشان التداول يكمل." if ar else
                "⏸ A trade was skipped: not enough SOL in your trading wallet. Deposit SOL to keep trading.")
    if key == "loss_limit":
        return ("⏸ وصلت لأقصى خسارة في 24 ساعة: مفيش صفقات جديدة لحد ما الحد يفضى. الصفقات المفتوحة بتتقفل عادي."
                if ar else "⏸ Your max loss for 24 h is reached: no new trades until it clears. "
                           "Open trades still close as usual.")
    if key == "hand":
        return ("🧾 البيع اللي عملته من محفظتك اتحسب:\n" if ar else
                "🧾 Sales you made in your wallet are now counted:\n") + v["lines"]
    raise KeyError(key)


def accounts_from_settings(s: Settings) -> list[Account]:
    out = [Account("main", s.live_size_usd, s.live_max_open_usd, s.live_max_daily_loss_usd,
                   list(s.live_wallets), s.live_private_key)]
    for cfg in s.live_accounts:
        name = str(cfg.get("name", "")).strip().lower()
        if not re.fullmatch(r"[a-z0-9_]{1,24}", name) or name == "main" or any(a.name == name for a in out):
            log.error("live: bad or duplicate account name %r in LIVE_ACCOUNTS", name)
            continue
        size = float(cfg.get("size_usd", s.live_size_usd))
        max_open = float(cfg.get("max_open_usd", size * 5))
        out.append(Account(
            name, size, max_open, float(cfg.get("max_daily_loss_usd", max_open / 2)),
            list(cfg.get("wallets") or s.live_wallets),
            os.environ.get(f"LIVE_PRIVATE_KEY_{name.upper()}") or cfg.get("private_key"),
        ))
    return out


def _acct(name: str):
    """SQL filter for one account's positions (rows from before accounts existed belong to main)."""
    if name == "main":
        return or_(LivePosition.account.is_(None), LivePosition.account == "main")
    return LivePosition.account == name


def _key(base: str, account: str) -> str:
    return base if account == "main" else f"{base}:{account}"


class JupiterGate:
    """One gate for every Jupiter call in the process: all accounts share the free API's per-IP limit.

    Calls are spaced at least `interval` seconds apart; a 429 is retried with backoff (Retry-After honoured)
    instead of failing the trade.
    """

    def __init__(self, interval: float = 1.1, retries: int = 4) -> None:
        self.interval, self.retries = interval, retries
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._waiting = 0           # calls queued for a slot right now
        self._cool_until = 0.0      # after a 429, background calls stay away for a while

    async def _slot(self) -> None:
        t0 = time.monotonic()
        self._waiting += 1
        try:
            await self._lock.acquire()
        finally:
            self._waiting -= 1
        try:
            wait = self._last + self.interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()
        finally:
            self._lock.release()
        _took("jup_queue", time.monotonic() - t0)     # waiting for our turn in the shared Jupiter gate

    async def request(self, http: httpx.AsyncClient, method: str, url: str, background: bool = False,
                      retries: int | None = None, **kw) -> httpx.Response | None:
        """background: price checks and paper (never trades): skipped (None) while a trade is queued or for a while
        after a 429, and never retried, so they can't hold up a buy or sell."""
        if background and (self._waiting or self._lock.locked() or time.monotonic() < self._cool_until):
            return None
        tries = (0 if background else self.retries) if retries is None else retries
        r: httpx.Response | None = None
        for attempt in range(tries + 1):
            await self._slot()
            r = await http.request(method, url, **kw)
            if r.status_code == 429:
                self._cool_until = time.monotonic() + 15.0
            if r.status_code != 429 or attempt == tries:
                return r
            try:
                pause = min(float(r.headers.get("retry-after") or 0), 10.0)
            except ValueError:
                pause = 0.0
            pause = pause or 1.5 * (attempt + 1)
            log.warning("jupiter 429 on %s, retry %d in %.1fs", url.rsplit("/", 2)[-1], attempt + 1, pause)
            await asyncio.sleep(pause)
        return r  # type: ignore[return-value]


JUPITER = JupiterGate()
_sol_px: tuple[float, float] = (0.0, 0.0)   # (monotonic time, USD per SOL), shared by all accounts
SOL_PRICE_TTL = 60.0        # SOL's price only sizes a buy: a minute old is fine and spares a Jupiter call
PUMP_DECIMALS = 6                 # pump.fun mints (address ends in "pump") are always 6 decimals
_DECIMALS: dict[str, int] = {}   # mint -> decimals (never change), read from the RPC, not Jupiter
STABLES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": 6,   # USDC
           "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCeUBenwNYB": 6}   # USDT
HISTORY_PAGES = 10          # getSignaturesForAddress pages (1000 each) walked back at most


class JupiterExecutor:
    """Quotes real routes. In dry mode stops there; in live mode signs and sends."""

    def __init__(self, s: Settings, live: bool, private_key: str | None = None) -> None:
        self.s = s
        self.mode = "live" if live else "dry"
        self.kp = load_keypair(private_key) if private_key else None
        if live and self.kp is None:
            raise SwapError("live mode needs a private key")
        self.pubkey = str(self.kp.pubkey()) if self.kp else None
        self.rpc_url = s.live_rpc_url or s.solana_rpc_url
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(20.0), headers={"User-Agent": "alpha-scanner/0.1"})
        self._dry_n = 0

    async def close(self) -> None:
        await self.http.aclose()

    # ---- plumbing ----------------------------------------------------------
    async def _rpc(self, method: str, params: list[Any]) -> Any:
        r = await self.http.post(self.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        r.raise_for_status()
        body = r.json()
        if body.get("error"):
            raise SwapError(f"{method}: {body['error'].get('message', body['error'])}"[:250])
        return body.get("result")

    async def _prices(self, mints: list[str]) -> dict[str, Any]:
        r = await JUPITER.request(self.http, "GET", f"{self.s.jupiter_api_url}/price/v3",
                                  params={"ids": ",".join(mints)})
        if r.status_code != 200:
            raise SwapError(f"price API {r.status_code}: {r.text[:120]}")
        data = r.json() or {}
        sol = (data.get(SOL_MINT) or {}).get("usdPrice")
        if sol:
            global _sol_px
            _sol_px = (time.monotonic(), float(sol))
        return data

    async def decimals(self, mint: str) -> int:
        """Token decimals from the RPC (getTokenSupply), cached forever: keeps the Jupiter queue for swaps."""
        if mint not in _DECIMALS:
            # a coin made seconds ago is not "finalized" yet (the RPC default): ask at "confirmed", then "processed"
            err: Exception | None = None
            for commitment in ("confirmed", "processed"):
                try:
                    res = await self._rpc("getTokenSupply", [mint, {"commitment": commitment}])
                    _DECIMALS[mint] = int(((res or {}).get("value") or {})["decimals"])
                    break
                except Exception as e:  # noqa: BLE001
                    err = e
            else:
                if mint.endswith("pump"):
                    return PUMP_DECIMALS      # every pump.fun coin has 6; not cached, the RPC answers once it lands
                raise err or SwapError("token decimals unknown")
        return _DECIMALS[mint]

    async def sol_price(self) -> float:
        if time.monotonic() - _sol_px[0] < SOL_PRICE_TTL:
            return _sol_px[1]
        await self._prices([SOL_MINT])
        if _sol_px[1] <= 0:
            raise SwapError("no SOL price")
        return _sol_px[1]

    async def quote(self, input_mint: str, output_mint: str, amount: int,
                    slippage_bps: int | None = None) -> dict[str, Any]:
        r = await JUPITER.request(self.http, "GET", f"{self.s.jupiter_api_url}/swap/v1/quote", params={
            "inputMint": input_mint, "outputMint": output_mint, "amount": str(amount),
            "slippageBps": slippage_bps or self.s.live_slippage_bps, "restrictIntermediateTokens": "true",
        })
        if r.status_code != 200:
            raise SwapError(f"no route ({r.status_code}): {r.text[:150]}")
        q = r.json()
        if not q.get("outAmount"):
            raise SwapError("no route")
        return q

    async def sol_balance(self) -> float:
        if not self.pubkey:
            return 0.0
        res = await self._rpc("getBalance", [self.pubkey, {"commitment": "confirmed"}])
        return (res or {}).get("value", 0) / LAMPORTS

    async def token_balance(self, mint: str) -> int:
        try:
            res = await self._rpc("getTokenAccountsByOwner", [self.pubkey, {"mint": mint},
                                                              {"encoding": "jsonParsed", "commitment": "confirmed"}])
        except SwapError as e:
            # a coin made seconds ago the RPC hasn't seen yet: the wallet holds none of it (and after our buy the
            # balance check simply waits for it to show up instead of failing a swap that went through)
            if "could not find mint" in str(e):
                return 0
            raise
        total = 0
        for acc in (res or {}).get("value", []):
            info = acc["account"]["data"]["parsed"]["info"]
            total += int(info["tokenAmount"]["amount"])
        return total

    async def _pump_prices(self, mints: list[str]) -> dict[str, dict[str, float]]:
        """Live prices of pump.fun / PumpSwap coins: on-chain pool / bonding-curve state (app/pool_prices.py)
        first, then PumpPortal's trade stream (app/pump_prices.py); {} when both feeds are off. Asking also keeps
        those coins watched for the next checks."""
        from app import pool_prices, pump_prices

        if not mints:
            return {}
        found: dict[str, float] = {}
        for feed in (pool_prices.POOL, pump_prices.PUMP):
            if feed is None:
                continue
            feed.request(mints)
            for m in mints:
                if m not in found and (s := feed.price_sol(m)):
                    found[m] = s
        if not found:
            return {}
        try:
            sol_usd = await self.sol_price()
        except Exception:
            return {}
        out: dict[str, dict[str, float]] = {}
        for m, s in found.items():
            try:
                out[m] = {"usd": s * sol_usd, "decimals": await self.decimals(m)}
            except Exception:
                continue
        return out

    async def token_prices_fast(self, mints: list[str]) -> dict[str, dict[str, float]]:
        """{mint: {"usd", "decimals"}}: PumpPortal's trade stream first (pump coins), then Jupiter's price API
        (fresher than DexScreener on new pump.fun tokens). Jupiter goes through the shared gate, so use this only
        for positions near a target."""
        out = await self._pump_prices(mints)
        mints = [m for m in mints if m not in out]
        if not mints:
            return out
        r = await JUPITER.request(self.http, "GET", f"{self.s.jupiter_api_url}/price/v3", background=True,
                                  params={"ids": ",".join(mints[:50])}, timeout=8)
        if r is not None and r.status_code == 200:
            for mint, d in (r.json() or {}).items():
                if d and d.get("usdPrice") and d.get("decimals") is not None:
                    out[mint] = {"usd": float(d["usdPrice"]), "decimals": int(d["decimals"])}
        return out

    async def wallet_tokens(self) -> dict[str, int]:
        """{mint: raw balance} of every token this wallet holds (SPL Token and Token-2022)."""
        out: dict[str, int] = {}
        for program in ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"):
            res = await self._rpc("getTokenAccountsByOwner", [self.pubkey, {"programId": program},
                                                              {"encoding": "jsonParsed", "commitment": "confirmed"}])
            for acc in (res or {}).get("value", []):
                info = acc["account"]["data"]["parsed"]["info"]
                amt = int(info["tokenAmount"]["amount"])
                if amt > 0:
                    out[info["mint"]] = out.get(info["mint"], 0) + amt
        return out

    async def token_symbol(self, mint: str) -> str | None:
        try:
            r = await self.http.get(f"https://api.dexscreener.com/tokens/v1/solana/{mint}", timeout=8)
            pairs = r.json() if r.status_code == 200 else []
            for p in pairs if isinstance(pairs, list) else []:
                b = p.get("baseToken") or {}
                if b.get("address") == mint and b.get("symbol"):
                    return str(b["symbol"])[:60]
        except Exception:
            return None
        return None

    async def _history_rpc(self, method: str, params: list[Any]) -> Any:
        """Paced RPC call for history reads (many in a row): waits out 429s instead of failing."""
        for attempt in range(6):
            await asyncio.sleep(0.12)
            try:
                return await self._rpc(method, params)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 429 or attempt == 5:
                    raise SwapError(f"{method}: HTTP {exc.response.status_code}") from None
                await asyncio.sleep(1.5 * (attempt + 1))
        raise SwapError("unreachable")

    async def transfer_sol(self, to: str, lamports: int) -> str:
        """Send SOL from this wallet (owner's withdrawal). Returns the signature once confirmed."""
        from solders.hash import Hash
        from solders.message import Message
        from solders.pubkey import Pubkey
        from solders.system_program import TransferParams, transfer
        from solders.transaction import Transaction

        if self.mode != "live" or self.kp is None:
            raise SwapError("withdrawals need live mode and the wallet key")
        if lamports <= 0:
            raise SwapError("nothing to send")
        bh = await self._rpc("getLatestBlockhash", [{"commitment": "confirmed"}])
        blockhash = Hash.from_string(bh["value"]["blockhash"])
        ix = transfer(TransferParams(from_pubkey=self.kp.pubkey(), to_pubkey=Pubkey.from_string(to),
                                     lamports=lamports))
        tx = Transaction([self.kp], Message.new_with_blockhash([ix], self.kp.pubkey(), blockhash), blockhash)
        sig = await self._rpc("sendTransaction", [base64.b64encode(bytes(tx)).decode(),
                                                  {"encoding": "base64", "preflightCommitment": "confirmed"}])
        for _ in range(40):
            await asyncio.sleep(1)
            st = await self._rpc("getSignatureStatuses", [[sig]])
            v = ((st or {}).get("value") or [None])[0]
            if v and v.get("err"):
                raise SwapError(f"transfer failed on-chain: {v['err']}")
            if v and v.get("confirmationStatus") in ("confirmed", "finalized"):
                return sig
        raise SwapError(f"transfer {sig} sent but not confirmed yet: check Solscan before trying again")

    async def token_prices(self, mints: list[str]) -> dict[str, dict[str, float]]:
        """{mint: {"usd", "decimals"}}: PumpPortal's trade stream first (pump coins), then DexScreener (Jupiter
        only once per mint, for decimals)."""
        from app.api.live import jupiter_prices

        out = await self._pump_prices(mints)
        rest = [m for m in mints if m not in out]
        if rest:
            out.update(await jupiter_prices(self.http, rest))
        return out

    async def wallet_sales(self, mints: set[str], since: datetime) -> dict[str, list[dict[str, Any]]]:
        """Every confirmed transaction of this wallet since `since` that lowered its balance of one of `mints`.

        Returns {mint: [{sig, at, tokens_raw (sold, > 0), sol (received, >= 0), usd (stablecoins received)}]}.
        Raises SwapError when the history could not be read completely (nothing must be booked then).
        """
        out: dict[str, list[dict[str, Any]]] = {m: [] for m in mints}
        for sig, d in await self._wallet_history(mints, since):
            sold = [m for m in mints if d["tokens"].get(m, 0) < 0]
            for m in sold:              # one tx selling several tokens at once: split what came in evenly
                out[m].append({"sig": sig, "at": d["at"], "tokens_raw": -d["tokens"][m],
                               "sol": max(d["sol"], 0.0) / len(sold), "usd": max(d["usd"], 0.0) / len(sold)})
        return out

    async def wallet_buys(self, mints: set[str], since: datetime) -> dict[str, list[dict[str, Any]]]:
        """Like wallet_sales, for transactions that raised the balance: {mint: [{sig, at, tokens_raw (bought, > 0),
        sol (spent, >= 0), usd (stablecoins spent)}]}. A plain transfer in shows sol ~0 (the caller skips it)."""
        out: dict[str, list[dict[str, Any]]] = {m: [] for m in mints}
        for sig, d in await self._wallet_history(mints, since):
            got = [m for m in mints if d["tokens"].get(m, 0) > 0]
            for m in got:
                out[m].append({"sig": sig, "at": d["at"], "tokens_raw": d["tokens"][m],
                               "sol": max(-d["sol"], 0.0) / len(got), "usd": max(-d["usd"], 0.0) / len(got)})
        return out

    async def _wallet_history(self, mints: set[str], since: datetime) -> list[tuple[str, dict[str, Any]]]:
        """(signature, balance changes) of every confirmed transaction of this wallet since `since`."""
        if not self.pubkey or not mints:
            return []
        floor = since.timestamp()
        sigs: list[dict[str, Any]] = []
        before, complete = None, False
        for _ in range(HISTORY_PAGES):
            opts: dict[str, Any] = {"limit": 1000, "commitment": "confirmed"}
            if before:
                opts["before"] = before
            page = await self._history_rpc("getSignaturesForAddress", [self.pubkey, opts]) or []
            for r in page:
                if r.get("blockTime") is not None and r["blockTime"] < floor:
                    complete = True
                    break
                if r.get("err") is None:
                    sigs.append(r)
            if complete or len(page) < 1000:
                complete = True
                break
            before = page[-1]["signature"]
        if not complete:
            raise SwapError("wallet history too long to read")
        cache = self.__dict__.setdefault("_tx_cache", {})
        out: list[tuple[str, dict[str, Any]]] = []
        for r in sigs:
            sig = r["signature"]
            if sig not in cache:
                tx = await self._history_rpc("getTransaction", [sig, {
                    "encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 1}])
                if tx is None:
                    raise SwapError(f"transaction {sig[:12]} not available yet")
                cache[sig] = _tx_deltas(tx, self.pubkey)
                if len(cache) > 5000:
                    cache.pop(next(iter(cache)))
            out.append((sig, cache[sig]))
        return out

    async def _send(self, q: dict[str, Any]) -> tuple[str, bool]:
        from solders.transaction import VersionedTransaction

        t_build = time.monotonic()
        r = await JUPITER.request(self.http, "POST", f"{self.s.jupiter_api_url}/swap/v1/swap", json={
            "quoteResponse": q, "userPublicKey": self.pubkey, "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": {"priorityLevelWithMaxLamports": {
                "maxLamports": self.s.live_priority_fee_lamports, "priorityLevel": "veryHigh"}},
        })
        body = r.json() if r.status_code == 200 else {}
        if not body.get("swapTransaction"):
            raise SwapError(f"swap build failed ({r.status_code}): {r.text[:150]}")
        last_valid = body.get("lastValidBlockHeight")
        _took("swap_build", time.monotonic() - t_build)
        return await self._send_tx(base64.b64decode(body["swapTransaction"]), last_valid)

    async def _send_tx(self, tx_bytes: bytes, last_valid: int | None = None,
                       signed_b64: str | None = None) -> tuple[str, bool]:
        """Sign an unsigned swap transaction and get it on-chain (see _send). `signed_b64`: already signed and
        simulated by the caller, so the first send skips the preflight."""
        from solders.transaction import VersionedTransaction

        tx = VersionedTransaction.from_bytes(tx_bytes)
        raw = signed_b64 or base64.b64encode(bytes(VersionedTransaction(tx.message, [self.kp]))).decode()
        # First send with preflight (a doomed swap fails here, not on-chain). Then re-broadcast the same signed
        # transaction every ~2 s until it confirms or its blockhash expires: under load, nodes drop transactions
        # and a single send often never lands.
        t_send = time.monotonic()
        try:
            sig = await self._rpc("sendTransaction", [raw, {
                "encoding": "base64", "skipPreflight": bool(signed_b64), "maxRetries": 0,
                "preflightCommitment": "confirmed"}])
        except SwapError as exc:
            if _is_slippage(exc):
                raise SlippageError("price moved past the slippage limit (simulation)") from exc
            raise
        t_sent = time.monotonic()
        _took("send", t_sent - t_send)                # includes the RPC's preflight simulation
        deadline = time.monotonic() + 90
        n = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)        # was 2 s: the confirmation is seen up to 1.5 s sooner
            n += 1
            st = ((await self._rpc("getSignatureStatuses", [[sig]])) or {}).get("value", [None])[0]
            if st:
                if st.get("err"):
                    if _is_slippage(st["err"]):
                        raise SlippageError("price moved past the slippage limit (on-chain, fee only)")
                    raise SwapError(f"transaction failed: {st['err']}"[:250])
                if st.get("confirmationStatus") in ("confirmed", "finalized"):
                    _took("confirm", time.monotonic() - t_sent)
                    return sig, True
                continue        # processed: landed, waiting for confirmation; no need to resend
            if n % 4:
                continue                    # re-broadcast / expiry check every ~2 s as before
            if last_valid and n % 12 == 0:
                height = await self._rpc("getBlockHeight", [{"commitment": "confirmed"}])
                if isinstance(height, int) and height > last_valid:
                    raise Dropped(f"{sig[:12]}… expired without landing")
            elif not last_valid and n % 12 == 0:      # PumpPortal gives no last valid height: ask about the blockhash
                valid = await self._rpc("isBlockhashValid", [str(tx.message.recent_blockhash),
                                                             {"commitment": "processed"}])
                if isinstance(valid, dict) and valid.get("value") is False:
                    raise Dropped(f"{sig[:12]}… expired without landing")
            try:
                await self._rpc("sendTransaction", [raw, {"encoding": "base64", "skipPreflight": True,
                                                          "maxRetries": 0}])
            except Exception as exc:   # a failed re-broadcast is harmless: the next one or the status poll decides
                log.debug("re-broadcast %s: %s", sig[:12], exc)
        return sig, False   # unknown after 90 s: the caller checks balances

    # ---- trades ------------------------------------------------------------
    def _wider(self, bps: int) -> int:
        return min(int(bps * 1.6), self.s.live_max_slippage_bps)

    async def buy(self, mint: str, usd: float, max_price: float | None = None,
                  slippage_bps: int | None = None) -> Fill:
        bps = slippage_bps or self.s.live_slippage_bps
        timing: dict[str, float] = {}
        token = _TIMING.set(timing)
        t_start = time.monotonic()
        try:
            return await self._buy_attempts(mint, usd, max_price, bps, timing, t_start)
        finally:
            _TIMING.reset(token)

    async def _buy_attempts(self, mint: str, usd: float, max_price: float | None, bps: int,
                            timing: dict[str, float], t_start: float) -> Fill:
        direct = self.s.live_direct_pump_buys and mint.endswith("pump")
        for attempt in range(3):   # nothing is spent when a swap is dropped or hits slippage: try again, fresh quote
            try:
                f = None
                q0 = None
                if direct:
                    # best route: Jupiter quotes while PumpPortal builds and simulates; the better one is sent
                    rival = (asyncio.ensure_future(self._jup_buy_quote(mint, usd, bps))
                             if self.s.live_best_route else None)
                    try:
                        f = await self._buy_direct(mint, usd, max_price, bps, rival=rival)
                        timing["route"] = "pumpportal"
                    except JupiterBetter as exc:
                        log.info("buy %s: Jupiter gives %.2f%% more tokens after fees; buying there",
                                 mint[:8], exc.edge * 100)
                        q0 = exc.quote
                    except DirectUnavailable as exc:
                        log.info("buy %s: direct route unavailable (%s); using Jupiter", mint[:8], exc)
                        direct = False
                    finally:
                        if rival is not None and not rival.done():
                            rival.cancel()
                if f is None:
                    f = await (self._buy_once(mint, usd, max_price, bps, q0=q0) if q0 is not None
                               else self._buy_once(mint, usd, max_price, bps))
                    timing["route"] = "jupiter"
                timing["attempts"] = attempt + 1
                timing["executor_total"] = round(time.monotonic() - t_start, 2)
                f.timing = dict(timing)
                return f
            except Dropped as exc:
                if attempt == 2:
                    raise
                if isinstance(exc, SlippageError):
                    bps = self._wider(bps)
                log.warning("buy %s: %s; retrying with a fresh quote (slippage %.0f%%)", mint[:8], exc, bps / 100)
        raise SwapError("unreachable")

    async def _simulate_buy(self, raw_b64: str, mint: str) -> tuple[int, int, Any]:
        """Simulate a signed buy: (tokens it would bring, lamports it would cost, error). Reads both token programs'
        accounts, since newer pump.fun mints are Token-2022."""
        from app.solana_pda import TOKEN_PROGRAM, associated_token_address

        atas = [associated_token_address(self.pubkey, mint, TOKEN_PROGRAM),
                associated_token_address(self.pubkey, mint, TOKEN_2022_PROGRAM)]
        res = await self._rpc("simulateTransaction", [raw_b64, {
            "encoding": "base64", "commitment": "processed", "sigVerify": False,
            "accounts": {"encoding": "jsonParsed", "addresses": [self.pubkey, *atas]}}])
        v = (res or {}).get("value") or {}
        accs = v.get("accounts") or [None, None, None]
        tokens = 0
        for acc in accs[1:]:
            try:
                tokens = max(tokens, int(acc["data"]["parsed"]["info"]["tokenAmount"]["amount"]))
            except Exception:
                pass
        lamports_after = int((accs[0] or {}).get("lamports") or 0)
        return tokens, lamports_after, v.get("err")

    async def _jup_buy_quote(self, mint: str, usd: float, bps: int) -> dict[str, Any]:
        px = await self.sol_price()
        return await self.quote(SOL_MINT, mint, int(usd / px * LAMPORTS), bps)

    async def _rival_edge_buy(self, rival, got: int, spent: float, tok0: int) -> tuple[float, dict] | None:
        """Jupiter's edge over the simulated PumpPortal buy, waiting a little for its quote (speed first)."""
        try:
            q = await asyncio.wait_for(asyncio.shield(rival), timeout=self.s.live_best_route_wait_seconds)
        except Exception:
            return None
        if not q or not q.get("outAmount") or not q.get("inAmount"):
            return None
        jup_sol = (int(q["inAmount"]) / LAMPORTS + self.s.live_jupiter_fee_estimate_sol
                   + (ATA_RENT_SOL if not tok0 else 0.0))
        return route_edge_buy(got, spent, int(q["outAmount"]), jup_sol), q

    async def _buy_direct(self, mint: str, usd: float, max_price: float | None, bps: int,
                          rival=None) -> Fill:
        """Buy a pump.fun / PumpSwap token through PumpPortal's transaction API: no Jupiter queue. The signed
        transaction is simulated first, which gives the exact tokens and SOL for the chase check (and replaces the
        preflight). Anything that fails before sending raises DirectUnavailable (the caller uses Jupiter)."""
        from solders.transaction import VersionedTransaction

        if not getattr(self, "kp", None) or not self.pubkey:
            raise DirectUnavailable("no wallet key (dry run)")
        if time.monotonic() - _NO_DIRECT.get(mint, -1e9) < NO_DIRECT_SECONDS:
            raise DirectUnavailable("PumpPortal can't trade this token (seen earlier)")
        before = asyncio.ensure_future(asyncio.gather(self.token_balance(mint), self.sol_balance()))
        try:
            t0 = time.monotonic()
            px = await self.sol_price()
            _took("sol_price", time.monotonic() - t0)
            lamports = int(usd / px * LAMPORTS)
            slip = bps
            if max_price:       # the price we may pay is checked on the simulation below; cap the slippage too
                slip = max(min(bps, int(self.s.live_direct_slippage_cap_pct * 100)), 100)
            tok0 = sol0 = None

            async def build_and_simulate(slip_bps: int, amount: int | None = None):
                nonlocal tok0, sol0
                t1 = time.monotonic()
                try:
                    resp = await self.http.post(PUMPPORTAL_TRADE, data={
                        "publicKey": self.pubkey, "action": "buy", "mint": mint,
                        "amount": f"{(amount or lamports) / LAMPORTS:.9f}", "denominatedInSol": "true",
                        "slippage": f"{slip_bps / 100:.2f}",
                        "priorityFee": f"{self.s.live_direct_priority_fee_sol:.6f}", "pool": "auto"}, timeout=8)
                except Exception as exc:
                    raise DirectUnavailable(f"PumpPortal: {exc}") from exc
                _took("direct_build", time.monotonic() - t1)
                ctype = resp.headers.get("content-type", "")
                if resp.status_code != 200 or not resp.content or ctype.startswith(("application/json", "text")):
                    _NO_DIRECT[mint] = time.monotonic()         # e.g. a pool PumpPortal doesn't support
                    raise DirectUnavailable(f"PumpPortal {resp.status_code}: {resp.text[:150]}")
                try:
                    txo = VersionedTransaction.from_bytes(resp.content)
                except Exception as exc:
                    raise DirectUnavailable(f"PumpPortal sent no transaction: {exc}") from exc
                raw_b64 = base64.b64encode(bytes(VersionedTransaction(txo.message, [self.kp]))).decode()
                if tok0 is None:
                    t1 = time.monotonic()
                    tok0, sol0 = await before
                    _took("balances", time.monotonic() - t1)
                t1 = time.monotonic()
                try:
                    res = await self._simulate_buy(raw_b64, mint)
                except Exception as exc:
                    raise DirectUnavailable(f"simulation failed: {exc}") from exc
                _took("simulate", time.monotonic() - t1)
                return resp, raw_b64, res

            r, raw, (tok_after, lam_after, err) = await build_and_simulate(slip)
            if err and "6004" in str(err):
                # PumpPortal misprices some PumpSwap pools (its own slippage check fails): build once more with a
                # wider limit; what we really get is read from the simulation and checked against the chase limit
                slip = max(slip, int(self.s.live_direct_retry_slippage_pct * 100))
                r, raw, (tok_after, lam_after, err) = await build_and_simulate(slip)
                _took("direct_retry", 1)
            if err:     # nothing was sent: Jupiter tries it
                if "6063" in str(err):                   # pump.fun coin quoted in another token: not for PumpPortal
                    _NO_DIRECT[mint] = time.monotonic()
                raise DirectUnavailable(f"simulation error: {err}")
            got = tok_after - tok0
            spent = sol0 - lam_after / LAMPORTS
            if got <= 0 or spent <= 0:
                raise DirectUnavailable("simulation shows no tokens")
            # PumpPortal buys a fixed number of tokens with "max SOL = amount + slippage": on pools it misprices the
            # simulation spends up to that much more (+33% seen). Ask again for less until it costs what we set.
            want = lamports / LAMPORTS
            amount = lamports
            for _ in range(2):
                if spent <= want * (1 + self.s.live_direct_max_overspend_pct / 100):
                    break
                amount = int(amount * want / spent * 0.995)
                r, raw, (tok_after, lam_after, err) = await build_and_simulate(slip, amount)
                _took("direct_resize", 1)
                if err:
                    raise DirectUnavailable(f"simulation error after resizing: {err}")
                got, spent = tok_after - tok0, sol0 - lam_after / LAMPORTS
            if spent > want * (1 + self.s.live_direct_max_overspend_pct / 100) or got <= 0:
                raise DirectUnavailable(f"PumpPortal would spend {spent:.4f} SOL for {want:.4f}")
            if max_price:
                decimals = await self.decimals(mint)
                pay = spent * px / (got / 10 ** decimals)
                if pay > max_price:
                    raise ChaseError(pay, max_price)
            if rival is not None:
                # `spent` is all-in (simulated: PumpPortal's 0.5% fee, priority fee, rent); Jupiter's quote gets its
                # network fee and, on a first buy, the same rent added before comparing
                res = await self._rival_edge_buy(rival, got, spent, tok0)
                if res is not None:
                    edge, q = res
                    _took("route_edge_pct", round(edge * 100, 2))
                    if edge * 100 > self.s.live_best_route_margin_pct:
                        raise JupiterBetter(q, edge)
            if self.mode == "dry":
                self._dry_n += 1
                return Fill(f"dry-direct-{self._dry_n}", spent, got, px)
        finally:
            if not before.done():
                before.cancel()
        sig, ok = await self._send_tx(r.content, signed_b64=raw)
        t0 = time.monotonic()
        got = 0
        for _ in range(8):
            await asyncio.sleep(0.4)
            got = await self.token_balance(mint) - tok0
            if got > 0:
                break
        sol1 = await self.sol_balance()
        _took("after_confirm", time.monotonic() - t0)
        if got <= 0:
            raise SwapError(f"buy {sig} {'not confirmed' if not ok else 'returned no tokens'}")
        return Fill(sig, max(sol0 - sol1, 0.0), got, px)

    async def _buy_once(self, mint: str, usd: float, max_price: float | None = None,
                        bps: int | None = None, q0: dict[str, Any] | None = None) -> Fill:
        # Speed: the wallet's balances and the token's decimals are read from the RPC while Jupiter quotes;
        # only the quote and the swap go through the (shared, spaced) Jupiter queue.
        live = self.mode != "dry"
        before = asyncio.ensure_future(asyncio.gather(self.token_balance(mint), self.sol_balance())) if live else None
        dec = asyncio.ensure_future(self.decimals(mint)) if max_price else None
        try:
            t0 = time.monotonic()
            px = await self.sol_price()
            _took("sol_price", time.monotonic() - t0)
            lamports = int(usd / px * LAMPORTS)
            t0 = time.monotonic()
            q = q0 or await self.quote(SOL_MINT, mint, lamports, bps)   # q0: the quote that won the best-route check
            _took("quote", time.monotonic() - t0)
            if dec is not None:
                try:
                    decimals = await dec
                except Exception:
                    raise SwapError("token decimals unknown") from None
                # price we would actually pay per token, from the live quote (includes price impact)
                pay = usd / (int(q["outAmount"]) / 10 ** decimals)
                if pay > max_price:
                    raise ChaseError(pay, max_price)
                # the slippage allowed on top of the quote must not carry the fill past the chase limit either
                # (2026-11-21: a 30% limit with 25% slippage filled up to ~1.6x the leader's price)
                room = int((max_price / pay - 1) * 10_000)
                if room < (bps or 0):
                    q = await self.quote(SOL_MINT, mint, lamports, max(room, 100))
                    pay = usd / (int(q["outAmount"]) / 10 ** decimals)
                    if pay > max_price:
                        raise ChaseError(pay, max_price)
            if not live:
                self._dry_n += 1
                return Fill(f"dry-{self._dry_n}", lamports / LAMPORTS, int(q["outAmount"]), px)
            t0 = time.monotonic()
            tok0, sol0 = await before
            _took("balances", time.monotonic() - t0)
        finally:
            for t in (before, dec):
                if t is not None and not t.done():
                    t.cancel()
        sig, ok = await self._send(q)
        t0 = time.monotonic()
        got = 0
        for _ in range(8):            # the RPC shows the new balance a moment after confirmation
            await asyncio.sleep(0.4)
            got = await self.token_balance(mint) - tok0
            if got > 0:
                break
        sol1 = await self.sol_balance()
        _took("after_confirm", time.monotonic() - t0)
        if got <= 0:
            raise SwapError(f"buy {sig} {'not confirmed' if not ok else 'returned no tokens'}")
        return Fill(sig, max(sol0 - sol1, 0.0), got, px)

    async def sell(self, mint: str, tokens_raw: int) -> Fill:
        bps = self.s.live_slippage_bps
        for attempt in range(4):   # a dropped sell sold nothing: try again (fresh quote) rather than stay stuck
            try:
                return await self._sell_once(mint, tokens_raw, bps)
            except Dropped as exc:
                if attempt == 3:
                    raise
                if isinstance(exc, SlippageError):
                    bps = self._wider(bps)
                log.warning("sell %s: %s; retrying with a fresh quote (slippage %.0f%%)", mint[:8], exc, bps / 100)
        raise SwapError("unreachable")

    async def _sell_direct_offer(self, mint: str, amount: int, held: int, bps: int | None,
                                 sol0: float) -> tuple[bytes, str, float]:
        """Build a PumpPortal sell of `amount` raw tokens and simulate it: (unsigned tx, signed base64, SOL it
        would bring in, all fees included). Raises DirectUnavailable when it can't (nothing is sent)."""
        from solders.transaction import VersionedTransaction

        decimals = await self.decimals(mint)
        try:
            resp = await self.http.post(PUMPPORTAL_TRADE, data={
                "publicKey": self.pubkey, "action": "sell", "mint": mint,
                "amount": f"{amount / 10 ** decimals:.{decimals}f}", "denominatedInSol": "false",
                "slippage": f"{(bps or self.s.live_slippage_bps) / 100:.2f}",
                "priorityFee": f"{self.s.live_direct_priority_fee_sol:.6f}", "pool": "auto"}, timeout=8)
        except Exception as exc:
            raise DirectUnavailable(f"PumpPortal: {exc}") from exc
        ctype = resp.headers.get("content-type", "")
        if resp.status_code != 200 or not resp.content or ctype.startswith(("application/json", "text")):
            raise DirectUnavailable(f"PumpPortal {resp.status_code}: {resp.text[:150]}")
        try:
            txo = VersionedTransaction.from_bytes(resp.content)
        except Exception as exc:
            raise DirectUnavailable(f"PumpPortal sent no transaction: {exc}") from exc
        raw = base64.b64encode(bytes(VersionedTransaction(txo.message, [self.kp]))).decode()
        try:
            tok_after, lam_after, err = await self._simulate_buy(raw, mint)
        except Exception as exc:
            raise DirectUnavailable(f"simulation failed: {exc}") from exc
        if err:
            raise DirectUnavailable(f"simulation error: {err}")
        if held - tok_after < amount * 0.99:          # it would not sell what we asked (rounding aside)
            raise DirectUnavailable(f"simulation sells {held - tok_after} of {amount}")
        sol_out = lam_after / LAMPORTS - sol0
        if sol_out <= 0:                              # e.g. paid out as wrapped SOL: let Jupiter do it
            raise DirectUnavailable("simulation brings no SOL")
        return resp.content, raw, sol_out

    async def _sell_once(self, mint: str, tokens_raw: int, bps: int | None = None) -> Fill:
        px = await self.sol_price()
        if self.mode == "dry":
            q = await self.quote(mint, SOL_MINT, tokens_raw)
            self._dry_n += 1
            return Fill(f"dry-{self._dry_n}", int(q["outAmount"]) / LAMPORTS, tokens_raw, px)
        held = await self.token_balance(mint)
        amount = min(tokens_raw, held)
        if amount <= 0:
            raise SwapError("no tokens left to sell")
        sol0 = await self.sol_balance()
        offer = None
        if (self.s.live_best_route_sells and mint.endswith("pump") and getattr(self, "kp", None)
                and time.monotonic() - _NO_DIRECT.get(mint, -1e9) >= NO_DIRECT_SECONDS):
            # best route: Jupiter's quote and a simulated PumpPortal sell at the same time; more SOL wins
            jq, offer = await asyncio.gather(self.quote(mint, SOL_MINT, amount, bps),
                                             self._sell_direct_offer(mint, amount, held, bps, sol0),
                                             return_exceptions=True)
            if isinstance(offer, BaseException):
                log.info("sell %s: PumpPortal not available (%s)", mint[:8], str(offer)[:120])
                offer = None
            if isinstance(jq, BaseException):
                if offer is None:
                    raise jq
                q = None
            else:
                q = jq
        else:
            q = await self.quote(mint, SOL_MINT, amount, bps)
        use_pp = False
        if offer is not None:
            if q is None:
                use_pp = True
            else:
                jup_sol = int(q["outAmount"]) / LAMPORTS - self.s.live_jupiter_fee_estimate_sol
                edge = route_edge_sell(offer[2], jup_sol)
                use_pp = edge * 100 < -self.s.live_best_route_margin_pct
                log.info("sell %s: PumpPortal %.5f SOL vs Jupiter %.5f SOL after fees -> %s", mint[:8], offer[2],
                         jup_sol, "PumpPortal" if use_pp else "Jupiter")
        if use_pp:
            try:
                sig, ok = await self._send_tx(offer[0], signed_b64=offer[1])
            except SwapError as exc:
                # pump.fun / PumpSwap slippage codes (6002-6004): nothing was sold, retry with a fresh price
                if not isinstance(exc, Dropped) and any(c in str(exc) for c in ("6002", "6003", "6004")):
                    raise SlippageError(f"PumpPortal sell: {exc}") from exc
                raise
        else:
            sig, ok = await self._send(q)
        await asyncio.sleep(1)
        sold, sol1 = held - await self.token_balance(mint), await self.sol_balance()
        if sold <= 0:
            raise SwapError(f"sell {sig} {'not confirmed' if not ok else 'sold nothing'}")
        return Fill(sig, max(sol1 - sol0, 0.0), sold, px)


def _tx_deltas(tx: dict[str, Any], owner: str) -> dict[str, Any]:
    """What one parsed transaction changed for `owner`: SOL (incl. wrapped SOL, net of the fee), stablecoin USD
    and raw token amounts per mint."""
    meta = tx.get("meta") or {}
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
    sol = 0.0
    if owner in keys:
        i = keys.index(owner)
        sol = (meta["postBalances"][i] - meta["preBalances"][i]) / LAMPORTS
    tokens: dict[str, int] = {}
    for sign, field in ((-1, "preTokenBalances"), (1, "postTokenBalances")):
        for b in meta.get(field) or []:
            if b.get("owner") == owner:
                tokens[b["mint"]] = tokens.get(b["mint"], 0) + sign * int(b["uiTokenAmount"]["amount"])
    sol += tokens.pop(SOL_MINT, 0) / LAMPORTS
    usd = sum(tokens.pop(m, 0) / 10 ** dec for m, dec in STABLES.items())
    at = datetime.fromtimestamp(tx["blockTime"], timezone.utc) if tx.get("blockTime") else _now()
    return {"sol": sol, "usd": usd, "tokens": tokens, "at": at}


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _get(session: AsyncSession, key: str) -> str | None:
    row = await session.get(TrackerCursor, key)
    return row.value if row else None


async def _put(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(TrackerCursor, key)
    if row is None:
        session.add(TrackerCursor(key=key, value=value))
    else:
        row.value = value


async def is_paused(session: AsyncSession) -> bool:
    return (await _get(session, PAUSED)) == "1"


async def set_paused(session: AsyncSession, paused: bool) -> None:
    await _put(session, PAUSED, "1" if paused else "0")


async def request_manual_sell(session: AsyncSession, position_id: int, share: float) -> tuple[bool, str]:
    """Queue a manual sell; the worker (which holds the key) executes it within a few seconds."""
    if share not in (0.25, 0.5, 1.0):
        return False, "Share must be 25%, 50% or 100%"
    pos = await session.get(LivePosition, position_id)
    if pos is None or pos.status != "open":
        return False, "Position is not open"
    if pos.manual_sell_frac:
        return False, f"Already selling {pos.manual_sell_frac * 100:.0f}% of ${pos.symbol or pos.token_address[:6]}"
    pos.manual_sell_frac = share
    await session.commit()
    return True, f"Selling {share * 100:.0f}% of ${pos.symbol or pos.token_address[:6]}…"


async def set_position_target(session: AsyncSession, position_id: int, target_pct: float | None,
                              share: float | None, stop_pct: float | None, *, target_price: float | None = None,
                              stop_price: float | None = None, hands_off: bool | None = None) -> tuple[bool, str]:
    """The owner's sell target for one open position (None clears it), in % over the entry or by the coin's price
    (USD per token). hands_off: no bot rule sells it, only this target / stop. The worker checks every few seconds."""
    pos = await session.get(LivePosition, position_id)
    if pos is None or pos.status != "open":
        return False, "Position is not open"
    if target_price is not None or stop_price is not None:
        if (target_price is not None and not target_price > 0) or (stop_price is not None and not stop_price > 0):
            return False, "Prices must be above 0"
        if target_price is not None and stop_price is not None and stop_price >= target_price:
            return False, "The stop price must be below the target price"
        share = 1.0 if share is None else share
        if not 0.05 <= share <= 1.0:
            return False, "Share must be between 5% and 100%"
        pos.target_pct = pos.stop_pct = None
        pos.target_price, pos.stop_price = target_price, stop_price
        pos.target_share = share if target_price is not None else None
        pos.hands_off = bool(hands_off)
        await session.commit()
        sym = pos.symbol or pos.token_address[:6]
        parts = []
        if target_price is not None:
            parts.append(f"sell {share * 100:.0f}% at ${target_price:.6g}")
        if stop_price is not None:
            parts.append(f"sell all at ${stop_price:.6g}")
        return True, f"${sym}: " + ", ".join(parts) + (" · only your target/stop sell it" if hands_off else "")
    pos.target_price = pos.stop_price = None
    pos.hands_off = bool(hands_off) if hands_off is not None else None
    if target_pct is not None and not 1 <= target_pct <= 100_000:
        return False, "Target must be between +1% and +100000%"
    if stop_pct is not None:
        stop_pct = -abs(stop_pct)
        if not -99 <= stop_pct <= -1:
            return False, "Stop loss must be between -1% and -99%"
    share = 1.0 if share is None else share
    if not 0.05 <= share <= 1.0:
        return False, "Share must be between 5% and 100%"
    pos.target_pct, pos.target_share, pos.stop_pct = target_pct, (share if target_pct is not None else None), stop_pct
    await session.commit()
    sym = pos.symbol or pos.token_address[:6]
    if target_pct is None and stop_pct is None:
        if pos.hands_off:
            return True, f"Target cleared for ${sym} · only you sell it"
        return True, f"Target cleared for ${sym}"
    parts = []
    if target_pct is not None:
        parts.append(f"sell {share * 100:.0f}% at +{target_pct:g}%")
    if stop_pct is not None:
        parts.append(f"sell all at {stop_pct:g}%")
    return True, f"${sym}: " + ", ".join(parts) + (" · only your target/stop sell it" if pos.hands_off else "")


async def hold_with_target(session: AsyncSession, account: str, mode: str, mint: str, raw: int, symbol: str | None,
                           value_usd: float | None, share: float | None, target_price: float | None,
                           stop_price: float | None) -> tuple[bool, str]:
    """A sell price / stop price on any coin in an account's wallet (/live, Wallet tab). A coin the bot already holds
    gets the target on its position; any other coin becomes an owner-managed position (no leader, hands off) worth
    its value now, so the worker's target check sells it at the owner's prices."""
    if target_price is None and stop_price is None:
        return False, "Set a sell price or a stop price"
    if mint == SOL_MINT:
        return False, "SOL itself is not sold"
    pos = (await session.execute(select(LivePosition).where(
        LivePosition.token_address == mint, LivePosition.status == "open", LivePosition.mode == mode,
        _acct(account)).order_by(LivePosition.opened_at.desc()))).scalars().first()
    if pos is None:
        if raw <= 0:
            return False, "This wallet no longer holds that coin"
        if not value_usd or value_usd <= 0:
            return False, "No price for this coin yet, so it can't be watched"
        pos = LivePosition(
            mode=mode, account=account, token_address=mint, symbol=(symbol or mint[:6])[:60], leader_wallet=MANUAL,
            leader_trade_id=0, status="open", cost_usd=float(value_usd), sol_spent=0.0, tokens_raw=str(raw),
            tokens_bought_raw=str(raw), hands_off=True,
            fills=[{"side": "held", "tokens_raw": str(raw), "usd": round(float(value_usd), 2), "at": _now().isoformat()}])
        session.add(pos)
        await session.flush()
    return await set_position_target(session, pos.id, None, share, None, target_price=target_price,
                                     stop_price=stop_price, hands_off=True)


async def realized_24h(session: AsyncSession, mode: str, account: str = "main") -> float:
    since = _now() - timedelta(hours=24)
    v = (await session.execute(select(func.sum(LivePosition.pnl_usd)).where(
        LivePosition.mode == mode, LivePosition.status == "closed", LivePosition.closed_at >= since,
        _acct(account)))).scalar()
    return float(v or 0.0)


def _share(t: WalletTrade) -> float:
    """Share of its holding the leader just sold (1.0 = everything)."""
    if (t.kind or "") == "transfer":
        return 1.0                     # moved the tokens out: treat as an exit
    if t.remaining_amount is not None and t.remaining_amount <= 0:
        return 1.0
    if t.pct_sold:
        return min(max(t.pct_sold / 100.0, 0.0), 1.0)
    return 1.0


class CopyEngine:
    def __init__(self, executor: Executor, settings: Settings | None = None, notify=None,
                 account: Account | None = None, screener=None, send_to=None) -> None:
        self.x = executor
        self.send_to = send_to            # async (chat_id, text): the account owner's own chat
        self.screener = screener          # app.screen.TokenScreener, shared by all accounts; None = no check
        self.s = settings or get_settings()
        self.a = account or accounts_from_settings(self.s)[0]
        self.wallets = set(self.a.wallets)
        self.notify = notify or (lambda text: asyncio.sleep(0))
        self.cursor = _key(CURSOR, self.a.name)
        self._reconciled = 0.0              # monotonic time of the last wallet reconcile
        self._short_seen: dict[str, int] = {}   # mint -> reconciles in a row where the wallet held less
        self._extra_seen: dict[str, int] = {}   # mint -> reconciles in a row where the wallet held more
        self._extra_done: dict[str, int] = {}   # mint -> surplus already looked up (don't re-read history for it)
        self._priced = 0.0                  # monotonic time of the last take-capital price check
        self._fast_at = -1e9                # monotonic time of the last Jupiter price call for take-capital
        self._adopted = 0.0                 # monotonic time of the last look for the owner's own hand-bought tokens
        self._adopt_done: dict[str, int] = {}   # mint -> balance already looked up (no buy found / dust)
        self._said: dict[str, float] = {}    # repeating skip notices -> last time sent
        self._beat = 0.0                     # monotonic time of the last heartbeat written
        self._up_seen: dict[int, int] = {}  # position id -> checks in a row at or above the target
        self._quick_at = 0.0                # monotonic time of the last quick-exit check
        self._quick_seen: dict[int, int] = {}   # position id -> quick-exit readings in a row past TP / SL
        self.wake: asyncio.Event | None = None  # set by the WebSocket watcher when a copied wallet trades
        self._target_at = 0.0                   # last check of owner-set targets
        self._target_seen: dict[int, int] = {}  # position id -> readings in a row past its target / stop
        self._orders_at = 0.0                   # last check of the owner's price orders (app/orders.py)
        from app import curve_live

        self._curve_last = curve_live.last_id()  # curve signals (app/curve_live.py): only ones after we started
        self._order_seen: dict[int, int] = {}   # order id -> readings in a row past its trigger

    def update(self, account: Account) -> None:
        """New settings from the /accounts page; takes effect on the next step."""
        self.a = account
        self.wallets = set(account.wallets)

    def _free(self, wallet: str) -> bool:
        ld = self.a.leaders.get(wallet)
        return ld.unconditional if ld else wallet in self.s.live_unconditional_wallets

    def _new_only(self, wallet: str) -> bool:
        ld = self.a.leaders.get(wallet)
        return ld.new_tokens_only if ld else wallet in self.s.live_new_tokens_only_wallets

    def _chase(self, wallet: str, free: bool) -> float | None:
        """Most we pay above the leader's own price, in %: its own limit, else the account default
        (a looser one for "no checks" leaders, which buy fresh launches that move fast)."""
        ld = self.a.leaders.get(wallet)
        if ld is not None and ld.max_chase_pct is not None:
            return ld.max_chase_pct
        return self.s.live_max_chase_pct_no_checks if free else self.s.live_max_chase_pct

    def _size(self, wallet: str) -> float:
        ld = self.a.leaders.get(wallet)
        return ld.size_usd if ld and ld.size_usd else self.a.size_usd

    @staticmethod
    def _capital_back(pos: LivePosition) -> bool:
        """The position already returned its cost (take-capital, trims or hand sales)."""
        return (pos.proceeds_usd or 0) >= (pos.cost_usd or 0) * 0.98 > 0

    def _hold(self, mint: str) -> bool:
        """Hold-only token: the bot never sells it by itself."""
        return mint in self.s.live_hold_mints

    def _quick_rule(self, pos: LivePosition) -> tuple[float, float, timedelta]:
        """(take-profit ratio, stop-loss ratio, time limit) for this position's wallet: its own, else the defaults."""
        ld = self.a.leaders.get(pos.leader_wallet or "")
        tp = ld.quick_tp_pct if ld and ld.quick_tp_pct is not None else self.s.live_quick_tp_pct
        sl = ld.quick_sl_pct if ld and ld.quick_sl_pct is not None else self.s.live_quick_sl_pct
        hours = ld.quick_hours if ld and ld.quick_hours is not None else self.s.live_quick_max_hold_minutes / 60
        return 1 + tp / 100, 1 + sl / 100, timedelta(hours=hours)

    def _quick(self, pos: LivePosition) -> bool:
        """This position's wallet has the quick-exit switch on (and the token is not hold-only)."""
        ld = self.a.leaders.get(pos.leader_wallet or "")
        return bool(ld and ld.quick_exit) and not self._hold(pos.token_address) and not pos.hands_off

    def _take_capital_target(self) -> float | None:
        t = self.a.take_capital_at_pct
        return self.s.live_take_capital_at_pct if t is _FROM_SETTINGS else t

    async def tell_user(self, text: str) -> None:
        """Note for a registered user's own chat. Never name or hint at the leader wallet."""
        if self.send_to is not None and self.a.user_chat_id:
            try:
                await self.send_to(self.a.user_chat_id, text)
            except Exception:
                log.warning("live: user note failed", exc_info=True)

    def _tag(self) -> str:
        tag = "🟢 LIVE" if self.x.mode == "live" else "🧪 DRY-RUN"
        return tag if self.a.name == "main" else f"{tag} [{self.a.name}]"

    def _mine(self):
        return (LivePosition.mode == self.x.mode, _acct(self.a.name))

    async def step(self, session: AsyncSession) -> None:
        max_id = (await session.execute(select(func.max(WalletTrade.id)))).scalar() or 0
        cur = await _get(session, self.cursor)
        if cur is None:                       # first run: copy from now on only
            await _put(session, self.cursor, str(max_id))
            await session.commit()
            return
        since = int(cur)
        if max_id > since:
            trades = (await session.execute(
                select(WalletTrade).where(WalletTrade.id > since, WalletTrade.chain == Chain.solana)
                .order_by(WalletTrade.id))).scalars().all()
            for t in trades:
                if t.wallet in self.wallets:
                    if (t.side or "buy") == "buy" and (t.kind or "buy") == "buy":
                        await self._on_buy(session, t)
                    elif (t.side or "") == "sell":
                        await self._on_sell(session, t)
                await _put(session, self.cursor, str(t.id))
                await session.commit()        # one trade at a time: a crash never repeats a swap
            await _put(session, self.cursor, str(max_id))
            await session.commit()
        await self._housekeeping(session)
        if time.monotonic() - self._beat > 60:          # the host watchdog alerts when this goes stale
            self._beat = time.monotonic()
            await _put(session, _key(HEARTBEAT, self.a.name), _now().isoformat())
            await session.commit()

    # ---- buys --------------------------------------------------------------
    async def _skip_reason(self, session: AsyncSession, t: WalletTrade) -> str | None:
        if await is_paused(session):
            return "paused (/live on to resume)"
        if self.a.paused:
            return f"account {self.a.name} paused"
        ld = self.a.leaders.get(t.wallet)
        if ld is not None and not ld.enabled:
            return "leader off on this account"
        if t.token_address in self.s.blocked_mints():
            return "token blocked (never buy)"
        if self._new_only(t.wallet):
            if t.remaining_amount is not None:
                before = t.remaining_amount - t.amount          # leader's balance before this buy
                if before > max(t.amount, 1.0) * 1e-6:
                    return "already holding: leader added to a token it held (new tokens only)"
            elif not t.first_buy:                               # older rows without balances: fall back
                return "already holding: leader bought this token before (new tokens only)"
        sold = (await session.execute(select(WalletTrade.id).where(
            WalletTrade.wallet == t.wallet, WalletTrade.token_address == t.token_address,
            WalletTrade.side == "sell", WalletTrade.id > t.id).limit(1))).first()
        if sold and not (ld is not None and ld.ignore_sells):
            # it already sold again (DULX3e often flips in 3-4 s): buying now only pays the fees both ways
            return "leader already sold it"
        seen = utc(t.block_time) or utc(t.detected_at)
        if seen and (_now() - seen).total_seconds() > MAX_TRADE_AGE_SECONDS:
            return f"leader buy is {(_now() - seen).total_seconds() / 60:.0f} min old"
        free = self._free(t.wallet)            # copy as-is: no liquidity / chase / scanner check
        size = self._size(t.wallet)
        if not free and (t.liquidity_usd or 0) < self.s.live_min_liquidity_usd:
            return f"liquidity ${t.liquidity_usd or 0:,.0f} < ${self.s.live_min_liquidity_usd:,.0f}"
        exists = (await session.execute(select(LivePosition.id).where(
            LivePosition.token_address == t.token_address, LivePosition.status == "open",
            *self._mine()))).first()
        if exists:
            return "already holding"
        open_cost = (await session.execute(select(func.sum(LivePosition.cost_usd)).where(
            LivePosition.status == "open", *self._mine()))).scalar() or 0.0
        if open_cost + size > self.a.max_open_usd + 1e-6:
            return f"limit: ${open_cost:,.0f} already open (max ${self.a.max_open_usd:,.0f})"
        loss = await realized_24h(session, self.x.mode, self.a.name)
        if loss <= -self.a.max_daily_loss_usd:
            return f"daily loss limit hit (${loss:,.2f} in 24h)"
        if self.x.mode == "live":
            bal = await self.x.sol_balance()
            try:
                px = await self.x.sol_price()
            except Exception:
                px = None
            if px and bal - self.s.live_sol_reserve < size / px:
                return f"not enough SOL ({bal:.3f})"
        return None

    async def _on_buy(self, session: AsyncSession, t: WalletTrade) -> None:
        sym = t.symbol or t.token_address[:6]
        t_pick = time.monotonic()
        seen = utc(t.detected_at)
        pickup = (_now() - seen).total_seconds() if seen else None      # tracker saw it -> engine took it
        why = await self._skip_reason(session, t)
        checks = time.monotonic() - t_pick
        if why:
            log.info("live: skip buy %s (%s)", sym, why)
            quiet = "already holding" in why or why.startswith(("leader off", "token blocked"))
            if why.startswith("not enough SOL") or why.startswith("account ") or why.startswith("limit:"):
                # an unfunded, paused or full account would repeat this on every leader buy: say it every 6 h
                key = why.split(" ")[0]
                quiet = time.monotonic() - self._said.get(key, -1e9) < 6 * 3600
                if not quiet:
                    self._said[key] = time.monotonic()
            if not quiet:
                await self.notify(f"{self._tag()} ⏭ Skipped ${sym}: {why}")
                if why.startswith("not enough SOL"):
                    await self.tell_user(user_note(self.a.user_lang, "low_sol"))
                elif why.startswith("daily loss limit"):
                    await self.tell_user(user_note(self.a.user_lang, "loss_limit"))
            return
        free = self._free(t.wallet)
        if self.screener is not None and not free:
            res = await self.screener.check(t.token_address, t.wallet_label or t.wallet[:6])
            if not res.ok:
                log.info("live: skip buy %s (scanner: %s)", sym, res.reason)
                await self.notify(f"{self._tag()} 🛡 Skipped ${sym}: scanner says {res.reason}")
                return
            log.info("live: %s passed the scanner (%s)", sym, res.reason)
        chase = self._chase(t.wallet, free)
        max_price = t.price_usd * (1 + chase / 100) if (t.price_usd and chase is not None) else None
        t_exec = time.monotonic()
        try:
            f = await self.x.buy(t.token_address, self._size(t.wallet), max_price=max_price,
                                 slippage_bps=self.s.live_slippage_bps_unconditional if free else None)
        except ChaseError as exc:
            over = (exc.pay / t.price_usd - 1) * 100
            log.info("live: skip buy %s (price +%.0f%% over the leader's, limit +%.0f%%)", sym, over, chase)
            await self.notify(f"{self._tag()} ⏭ Skipped ${sym}: price already +{over:.0f}% above where "
                              f"{t.wallet_label or t.wallet[:6]} bought (limit +{chase:.0f}%)")
            return
        except Exception as exc:
            log.warning("live: buy %s failed: %s", sym, exc)
            session.add(LivePosition(mode=self.x.mode, account=self.a.name, token_address=t.token_address, symbol=t.symbol or "",
                                     leader_wallet=t.wallet, leader_trade_id=t.id, status="failed",
                                     cost_usd=0.0, error=str(exc)[:300], closed_at=_now()))
            await self.notify(f"{self._tag()} ❌ Buy ${sym} failed: {str(exc)[:200]}")
            return
        timing = {"block_to_seen": round((seen - utc(t.block_time)).total_seconds(), 2) if seen and t.block_time else None,
                  "pickup": round(pickup, 2) if pickup is not None else None, "checks": round(checks, 2),
                  **(getattr(f, "timing", None) or {}), "buy_call": round(time.monotonic() - t_exec, 2)}
        if t.block_time:
            timing["block_to_bought"] = round((_now() - utc(t.block_time)).total_seconds(), 2)
        session.add(LivePosition(
            mode=self.x.mode, account=self.a.name, token_address=t.token_address, symbol=t.symbol or "", leader_wallet=t.wallet,
            leader_trade_id=t.id, status="open", cost_usd=f.usd, sol_spent=f.sol, tokens_raw=str(f.tokens_raw),
            tokens_bought_raw=str(f.tokens_raw), buy_signature=f.signature,
            fills=[{"side": "buy", "sig": f.signature, "sol": f.sol, "tokens_raw": str(f.tokens_raw),
                    "usd": round(f.usd, 2), "at": _now().isoformat(), "timing": timing}],
        ))
        log.info("live: timing BUY %s [%s] %s", sym, self.a.name,
                 " ".join(f"{k}={v}" for k, v in timing.items() if v is not None))
        log.info("live: BUY %s for %.4f SOL ($%.2f) %s", sym, f.sol, f.usd, f.signature)
        await self.notify(f"{self._tag()} ✅ Bought ${sym} for {f.sol:.4f} SOL (${f.usd:,.2f})\n"
                          f"Copying {t.wallet_label or t.wallet[:6]}\n{self._tx(f.signature)}")
        await self.tell_user(user_note(self.a.user_lang, "bought", sym=sym, usd=f.usd, tx=self._tx(f.signature)))

    # ---- sells -------------------------------------------------------------
    async def _on_sell(self, session: AsyncSession, t: WalletTrade) -> None:
        pos = (await session.execute(select(LivePosition).where(
            LivePosition.token_address == t.token_address, LivePosition.leader_wallet == t.wallet,
            LivePosition.status == "open", *self._mine()))).scalars().first()
        share = _share(t)
        if share >= 0.99:
            await self._exit_alert(t, pos)
        if self._hold(t.token_address):
            sym = t.symbol or t.token_address[:6]
            if pos is not None:
                pos.leader_trade_id = t.id
                await session.commit()
                log.info("live: %s is hold-only: leader sold %.0f%%, keeping it", sym, share * 100)
                await self.notify(f"{self._tag()} 🔒 {t.wallet_label or t.wallet[:6]} sold {share * 100:.0f}% of "
                                  f"${sym}, but ${sym} is hold-only: keeping it (sell by hand if you want)")
            return
        if pos is not None and pos.hands_off:
            # the owner manages it: only their target / stop (or a sell from the page) takes it out
            pos.leader_trade_id = t.id
            await session.commit()
            log.info("live: %s is managed by the owner: ignoring leader sell %.0f%%",
                     pos.symbol or pos.token_address[:6], share * 100)
            return
        ld = self.a.leaders.get(t.wallet)
        if pos is not None and ld is not None and ld.ignore_sells:
            # the user's rule for fast wallets: its sells are not followed; quick exit / max hold take it out
            pos.leader_trade_id = t.id
            await session.commit()
            log.info("live: %s ignoring %s's sell (%.0f%%): its sells are not followed",
                     t.symbol or t.token_address[:6], t.wallet_label or t.wallet[:6], share * 100)
            return
        if pos is not None and self._quick(pos):
            # quick exit: the wallet's first sell (any size) takes the whole position out
            pos.leader_trade_id = t.id
            await self._sell(session, pos, 1.0, "leader_sold")
            await session.commit()
        elif pos is not None:
            pos.leader_trade_id = t.id
            if share < 0.99 and self.s.live_follow_after_trim:
                sym = pos.symbol or pos.token_address[:6]
                if self._took_half(pos) and not self._following(pos):
                    # 50% already sold at 2x: the rest leaves only on the leader's full exit
                    await session.commit()
                    log.info("live: %s 50%% already out at 2x: ignoring leader trim %.0f%%", sym, share * 100)
                    await self.notify(f"{self._tag()} ⏸ {t.wallet_label or t.wallet[:6]} sold {share * 100:.0f}% of "
                                      f"${sym}; half was already sold at 2x, the rest waits for its full exit")
                else:
                    # the leader trims before 2x: copy it, and from now on follow every sell of this token
                    await self._sell(session, pos, share, "leader_trim")
                    await session.commit()
            elif share < 0.99 and self.s.live_bag_waits_for_full_exit and self._capital_back(pos):
                # the capital is already out: the profit bag ignores partial sells and leaves only when the
                # leader sells everything
                await session.commit()
                sym = pos.symbol or pos.token_address[:6]
                log.info("live: %s capital already out: ignoring leader trim %.0f%%", sym, share * 100)
                await self.notify(f"{self._tag()} ⏸ {t.wallet_label or t.wallet[:6]} sold {share * 100:.0f}% of ${sym}; "
                                  "your capital is already out, so the rest waits for its full exit")
            elif share < 0.99 and not self.s.live_copy_trims:
                # the user's rule: partial sells are not copied; the rest leaves on the leader's full exit
                await session.commit()
                sym = pos.symbol or pos.token_address[:6]
                log.info("live: %s ignoring leader trim %.0f%%", sym, share * 100)
                await self.notify(f"{self._tag()} ⏸ {t.wallet_label or t.wallet[:6]} sold {share * 100:.0f}% of ${sym}; "
                                  "partial sells are not copied, the rest waits for its full exit")
            elif share < 0.99 and self.s.live_trim_cap_pct is not None:
                await self._capped_trim(session, pos, t, share)
            else:
                await self._sell(session, pos, share, "leader_sold" if share >= 0.99 else "leader_trim")
                await session.commit()
        if self.s.live_sell_manual_with_leader and self.x.mode == "live":
            await self._sell_manual(session, t, share)

    async def _exit_alert(self, t: WalletTrade, pos: LivePosition | None) -> None:
        """The user's alert: a copied wallet sold ALL of a token this account still holds (bot or hand-bought)."""
        sym = t.symbol or t.token_address[:6]
        who = t.wallet_label or t.wallet[:6]
        held_by_hand = 0
        if pos is None:
            if self.x.mode != "live":
                return
            try:
                held_by_hand = await self.x.token_balance(t.token_address)
            except Exception:
                return
            if held_by_hand <= 0:
                return
        ld = self.a.leaders.get(t.wallet)
        if pos is None:
            what = "you still hold it (not a bot position): sell by hand if you want"
        elif self._hold(t.token_address):
            what = "it is hold-only: the bot keeps it"
        elif pos.hands_off:
            what = "you manage it: only your target / stop sell it"
        elif ld is not None and ld.ignore_sells:
            what = "its sells are ignored: the bot keeps it (quick exit / max hold)"
        else:
            what = "the bot sells its position now"
        pnl = ""
        if pos is not None and pos.cost_usd:
            pnl = f" · in ${pos.cost_usd:,.2f}, out so far ${pos.proceeds_usd or 0:,.2f}"
        await self.notify(f"{self._tag()} 🚪 {who} sold ALL its ${sym}{pnl}\n{what}")

    @staticmethod
    def _following(pos: LivePosition) -> bool:
        """The leader trimmed this token and we copied it: the position follows its sells, no 50% at 2x."""
        return any(f.get("reason") == "leader_trim" for f in (pos.fills or []))

    @staticmethod
    def _took_half(pos: LivePosition) -> bool:
        return any(f.get("reason") == "take_capital" for f in (pos.fills or []))

    def _cap_room(self, pos: LivePosition) -> int:
        """Tokens that may still be sold before LIVE_TRIM_CAP_PCT of the position is sold in all."""
        held = int(pos.tokens_raw)
        bought = int(pos.tokens_bought_raw or 0) or held
        return max(held - int(bought * (1 - self.s.live_trim_cap_pct / 100)), 0)

    async def _capped_trim(self, session: AsyncSession, pos: LivePosition, t: WalletTrade, share: float) -> None:
        """Copy a leader's partial sell only until LIVE_TRIM_CAP_PCT of the position has been sold in all; the rest
        waits for the leader's full exit."""
        cap = self.s.live_trim_cap_pct / 100
        held = int(pos.tokens_raw)
        bought = int(pos.tokens_bought_raw or 0) or held
        room = held - int(bought * (1 - cap))          # tokens we may still sell before the cap
        want = int(held * share)
        sym = pos.symbol or pos.token_address[:6]
        who = t.wallet_label or t.wallet[:6]
        if room <= bought // 100 or held <= 0:          # under 1% left before the cap: treat it as reached
            await session.commit()
            log.info("live: %s trim cap reached: ignoring leader trim %.0f%%", sym, share * 100)
            await self.notify(f"{self._tag()} ⏸ {who} sold {share * 100:.0f}% of ${sym}; {cap * 100:.0f}% is already "
                              "sold, so the rest waits for its full exit")
            return
        take = min(want, room)
        await self._sell(session, pos, (take + 0.5) / held, "leader_trim")   # +0.5: int() in _sell lands on take
        await session.commit()
        if take < want:
            await self.notify(f"{self._tag()} ⏸ ${sym}: sold only up to {cap * 100:.0f}% of the position; "
                              "the rest waits for the leader's full exit")

    async def _sell_manual(self, session: AsyncSession, t: WalletTrade, share: float) -> None:
        """Tokens the owner bought by hand in this wallet follow the leader out too (same share)."""
        sym = t.symbol or t.token_address[:6]
        try:
            held = await self.x.token_balance(t.token_address)
        except Exception as exc:
            log.warning("live: balance check for manual %s failed: %s", sym, exc)
            return
        bot = (await session.execute(select(func.sum(LivePosition.tokens_raw.cast(Numeric))).where(
            LivePosition.token_address == t.token_address, LivePosition.status == "open", *self._mine()))).scalar()
        manual = held - int(bot or 0)
        amount = manual if share >= 0.99 else int(manual * share)
        if amount <= 0:
            return
        try:
            f = await self.x.sell(t.token_address, amount)
        except Exception as exc:
            log.warning("live: manual sell %s failed: %s", sym, exc)
            await self.notify(f"{self._tag()} ⚠️ Could not sell your own ${sym} with the wallet: {str(exc)[:160]}\n"
                              f"Sell it by hand in Phantom.")
            return
        log.info("live: SELL manual %s %.0f%% (%d raw) for %.4f SOL ($%.2f) %s", sym, share * 100, f.tokens_raw,
                 f.sol, f.usd, f.signature)
        await self.notify(f"{self._tag()} 💰 Sold {share * 100:.0f}% of your own ${sym} (bought by hand) for "
                          f"{f.sol:.4f} SOL (${f.usd:,.2f}) · {t.wallet_label or t.wallet[:6]} sold\n"
                          f"{self._tx(f.signature)}")

    async def _sell(self, session: AsyncSession, pos: LivePosition, share: float, reason: str) -> bool:
        held = int(pos.tokens_raw)
        amount = held if share >= 0.99 else int(held * share)
        sym = pos.symbol or pos.token_address[:6]
        if amount <= 0:
            return False
        try:
            f = await self.x.sell(pos.token_address, amount)
        except Exception as exc:
            attempts = sum(1 for x in (pos.fills or []) if x.get("side") == "sell_failed") + 1
            pos.fills = [*(pos.fills or []), {"side": "sell_failed", "share": share, "reason": reason,
                                              "error": str(exc)[:200], "at": _now().isoformat()}]
            pos.error = str(exc)[:300]
            log.warning("live: sell %s failed (%d): %s", sym, attempts, exc)
            if "no tokens left" in str(exc):
                self._reconciled = 0.0          # sold by hand? check the wallet on the next step
            if attempts in (1, MAX_SELL_ATTEMPTS):
                await self.notify(f"{self._tag()} ⚠️ Sell ${sym} failed{' again, giving up retries' if attempts >= MAX_SELL_ATTEMPTS else ', retrying'}: "
                                  f"{str(exc)[:160]}")
            return False
        pos.error = None
        remaining = max(held - f.tokens_raw, 0)
        if share >= 0.99:
            remaining = 0
        pos.tokens_raw = str(remaining)
        pos.proceeds_sol += f.sol
        pos.proceeds_usd += f.usd
        pos.fills = [*(f2 for f2 in (pos.fills or []) if f2.get("side") != "sell_failed"),
                     {"side": "sell", "sig": f.signature, "sol": f.sol, "tokens_raw": str(f.tokens_raw),
                      "usd": round(f.usd, 2), "reason": reason, "at": _now().isoformat()}]
        done = remaining == 0
        if done:
            pos.status, pos.closed_at, pos.exit_reason = "closed", _now(), reason
            pos.pnl_usd = pos.proceeds_usd - pos.cost_usd
        log.info("live: SELL %s %.0f%% for %.4f SOL ($%.2f) %s", sym, share * 100, f.sol, f.usd, f.signature)
        tail = (f"\nClosed: P&L {'+' if pos.pnl_usd >= 0 else '−'}${abs(pos.pnl_usd):,.2f}" if done
                else f"\nStill holding {100 - share * 100:.0f}% of the position")
        await self.notify(f"{self._tag()} 💰 Sold {share * 100:.0f}% of ${sym} for {f.sol:.4f} SOL (${f.usd:,.2f})"
                          f" · {reason.replace('_', ' ')}{tail}\n{self._tx(f.signature)}")
        await self.tell_user(user_note(self.a.user_lang, "sold", sym=sym, usd=f.usd, share=share, done=done,
                                       reason=reason, pnl=pos.pnl_usd or 0, tx=self._tx(f.signature)))
        return True

    async def _reconcile(self, session: AsyncSession) -> None:
        """Book tokens the owner sold by hand (e.g. in Phantom) so open positions and P&L match the wallet.

        For every token, compare what the open positions say we hold with the wallet's real balance. When the
        wallet holds less on two checks in a row, read the wallet's own history for the sales that are not the
        bot's, and book what they really received (SOL + stablecoins) into the positions, oldest first. A position
        whose tokens are all gone closes as "sold_by_hand". Nothing is booked if the history can't be read fully.
        """
        if self.x.mode != "live" or not hasattr(self.x, "wallet_sales"):
            return
        if time.monotonic() - self._reconciled < self.s.live_reconcile_seconds:
            return
        self._reconciled = time.monotonic()
        opened = (await session.execute(select(LivePosition).where(
            LivePosition.status == "open", *self._mine()).order_by(LivePosition.opened_at, LivePosition.id)
        )).scalars().all()
        by_mint: dict[str, list[LivePosition]] = {}
        for p in opened:
            by_mint.setdefault(p.token_address, []).append(p)
        short: dict[str, int] = {}
        extra: dict[str, int] = {}
        for mint, ps in by_mint.items():
            try:
                held = await self.x.token_balance(mint)
            except Exception as exc:
                log.warning("live: reconcile balance %s failed: %s", mint[:8], exc)
                continue
            missing = sum(int(p.tokens_raw) for p in ps) - held
            if missing > 0:
                self._short_seen[mint] = self._short_seen.get(mint, 0) + 1
                if self._short_seen[mint] >= 2:          # one odd RPC answer never books anything
                    short[mint] = missing
            else:
                self._short_seen.pop(mint, None)
            if missing < 0 and self.s.live_book_hand_buys and self._extra_done.get(mint) != -missing:
                self._extra_seen[mint] = self._extra_seen.get(mint, 0) + 1
                if self._extra_seen[mint] >= 2:
                    extra[mint] = -missing
            else:
                self._extra_seen.pop(mint, None)
        for d in (self._short_seen, self._extra_seen, self._extra_done):
            for mint in list(d):
                if mint not in by_mint:
                    d.pop(mint)
        if extra and hasattr(self.x, "wallet_buys"):
            await self._book_hand_buys(session, by_mint, extra)
        if not short:
            return
        rows = (await session.execute(select(LivePosition).where(
            LivePosition.token_address.in_(list(short)), *self._mine()))).scalars().all()
        known = {p.buy_signature for p in rows if p.buy_signature}
        for p in rows:
            for f in p.fills or []:
                known.update(s for s in [f.get("sig"), *(f.get("sigs") or [])] if s)
        since = min(utc(p.opened_at) for m in short for p in by_mint[m]) - timedelta(minutes=2)
        try:
            sales = await self.x.wallet_sales(set(short), since)
            px = await self.x.sol_price()
        except Exception as exc:
            log.warning("live: reconcile history failed: %s", exc)
            return
        lines = []
        for mint, missing in short.items():
            hs = [h for h in sales.get(mint, []) if h["sig"] not in known]
            sold = sum(h["tokens_raw"] for h in hs)
            if sold <= 0:
                log.warning("live: %s: wallet holds %d fewer tokens than open positions but no sale was found",
                            mint[:8], missing)
                continue
            booked = min(missing, sold)
            k = booked / sold                   # share of those sales that were the bot's tokens
            sol_in = sum(h["sol"] for h in hs) * k
            usd_in = sol_in * px + sum(h["usd"] for h in hs) * k
            when = max(h["at"] for h in hs)
            sigs = [h["sig"] for h in hs]
            left = booked
            for p in by_mint[mint]:
                if left <= 0:
                    break
                take = min(int(p.tokens_raw), left)
                if take <= 0:
                    continue
                left -= take
                part = take / booked
                p.tokens_raw = str(int(p.tokens_raw) - take)
                p.proceeds_sol += sol_in * part
                p.proceeds_usd += usd_in * part
                p.fills = [*(f for f in (p.fills or []) if f.get("side") != "sell_failed"),
                           {"side": "sell_hand", "sigs": sigs, "sol": sol_in * part, "tokens_raw": str(take),
                            "usd": round(usd_in * part, 2), "reason": "sold_by_hand", "at": when.isoformat()}]
                p.error = None
                sym = p.symbol or mint[:6]
                if int(p.tokens_raw) == 0:
                    p.status, p.closed_at, p.exit_reason = "closed", when, "sold_by_hand"
                    p.pnl_usd = p.proceeds_usd - p.cost_usd
                    lines.append(f"${sym}: closed, P&L {'+' if p.pnl_usd >= 0 else '−'}${abs(p.pnl_usd):,.2f}")
                else:
                    lines.append(f"${sym}: {take / int(p.tokens_bought_raw or take) * 100:.0f}% sold by hand")
            self._short_seen.pop(mint, None)
            log.info("live: booked %d raw %s sold by hand for %.4f SOL ($%.2f) %s", booked, mint[:8], sol_in,
                     usd_in, ",".join(s[:10] for s in sigs))
        await session.commit()
        if lines:
            await self.notify(f"{self._tag()} 🧾 Found sales you made by hand in the wallet, P&L updated:\n"
                              + "\n".join(lines))
            await self.tell_user(user_note(self.a.user_lang, "hand", lines="\n".join(lines)))

    async def _book_hand_buys(self, session: AsyncSession, by_mint: dict[str, list[LivePosition]],
                              extra: dict[str, int]) -> None:
        """Tokens the owner bought by hand (e.g. in Phantom) of a token the bot holds join that open position:
        its cost (total entry), tokens and P&L then include them, and they follow the leader's sells with it.
        Only real purchases count (SOL or stablecoins spent); a plain transfer in stays the owner's own."""
        rows = (await session.execute(select(LivePosition).where(
            LivePosition.token_address.in_(list(extra)), *self._mine()))).scalars().all()
        known = {p.buy_signature for p in rows if p.buy_signature}
        for p in rows:
            for f in p.fills or []:
                known.update(s for s in [f.get("sig"), *(f.get("sigs") or [])] if s)
        since = min(utc(p.opened_at) for m in extra for p in by_mint[m]) - timedelta(minutes=2)
        try:
            buys = await self.x.wallet_buys(set(extra), since)
            px = await self.x.sol_price()
        except Exception as exc:
            log.warning("live: hand-buy history failed: %s", exc)
            return
        lines = []
        for mint, surplus in extra.items():
            self._extra_done[mint], self._extra_seen[mint] = surplus, 0
            hs = [h for h in buys.get(mint, []) if h["sig"] not in known and (h["sol"] > 1e-4 or h["usd"] > 0.01)]
            got = sum(h["tokens_raw"] for h in hs)
            if got <= 0:
                continue
            booked = min(surplus, got)
            k = booked / got
            sol_out = sum(h["sol"] for h in hs) * k
            usd_out = sol_out * px + sum(h["usd"] for h in hs) * k
            p = by_mint[mint][-1]                       # the newest open position of this token
            p.tokens_raw = str(int(p.tokens_raw) + booked)
            p.tokens_bought_raw = str(int(p.tokens_bought_raw or 0) + booked)
            p.cost_usd += usd_out
            p.sol_spent += sol_out
            p.fills = [*(p.fills or []), {"side": "buy_hand", "sigs": [h["sig"] for h in hs], "sol": sol_out,
                                          "tokens_raw": str(booked), "usd": round(usd_out, 2),
                                          "at": max(h["at"] for h in hs).isoformat()}]
            self._extra_done[mint] = 0
            sym = p.symbol or mint[:6]
            lines.append(f"${sym}: +${usd_out:,.2f} added by hand, total entry now ${p.cost_usd:,.2f}")
            log.info("live: booked %d raw %s bought by hand for %.4f SOL ($%.2f) into position %d", booked,
                     mint[:8], sol_out, usd_out, p.id)
        await session.commit()
        if lines:
            await self.notify(f"{self._tag()} 🧾 Found buys you made by hand in the wallet, added to the position:\n"
                              + "\n".join(lines))

    async def _adopt_hand_buys(self, session: AsyncSession) -> None:
        """Tokens the owner bought by hand that the bot does not hold become a "manual" position (no leader): the
        bot takes the capital out at the take-capital target and books hand sales, and never sells it for max hold.
        Only real purchases count (SOL or stablecoins spent) within LIVE_ADOPT_LOOKBACK_HOURS; airdrops and
        transfers in stay the owner's own."""
        if not self.s.live_adopt_hand_buys or self.x.mode != "live" or not hasattr(self.x, "wallet_tokens"):
            return
        if time.monotonic() - self._adopted < self.s.live_reconcile_seconds:
            return
        self._adopted = time.monotonic()
        try:
            held = await self.x.wallet_tokens()
        except Exception as exc:
            log.warning("live: adopt balances failed: %s", exc)
            return
        opened = {p.token_address for p in (await session.execute(select(LivePosition).where(
            LivePosition.status == "open", *self._mine()))).scalars().all()}
        skip = set(STABLES) | {SOL_MINT} | set(self.s.live_hold_mints)
        new = {m: b for m, b in held.items()
               if m not in opened and m not in skip and self._adopt_done.get(m) != b}
        self._adopt_done = {m: b for m, b in self._adopt_done.items() if m in held}
        if not new:
            return
        rows = (await session.execute(select(LivePosition).where(
            LivePosition.token_address.in_(list(new)), *self._mine()))).scalars().all()
        known = {p.buy_signature for p in rows if p.buy_signature}
        for p in rows:
            for f in p.fills or []:
                known.update(s for s in [f.get("sig"), *(f.get("sigs") or [])] if s)
        since = _now() - timedelta(hours=self.s.live_adopt_lookback_hours)
        try:
            buys = await self.x.wallet_buys(set(new), since)
            px = await self.x.sol_price()
        except Exception as exc:
            log.warning("live: adopt history failed: %s", exc)
            return
        lines = []
        for mint, bal in new.items():
            self._adopt_done[mint] = bal
            hs = [h for h in buys.get(mint, []) if h["sig"] not in known and (h["sol"] > 1e-4 or h["usd"] > 0.01)]
            got = sum(h["tokens_raw"] for h in hs)
            if got <= 0:
                continue
            tokens = min(bal, got)
            k = tokens / got
            sol_out = sum(h["sol"] for h in hs) * k
            usd_out = sol_out * px + sum(h["usd"] for h in hs) * k
            sym = (await self.x.token_symbol(mint) if hasattr(self.x, "token_symbol") else None) or mint[:6]
            first = min(h["at"] for h in hs)
            session.add(LivePosition(
                mode=self.x.mode, account=self.a.name, token_address=mint, symbol=sym, leader_wallet=MANUAL,
                leader_trade_id=0, status="open", cost_usd=usd_out, sol_spent=sol_out, tokens_raw=str(tokens),
                tokens_bought_raw=str(tokens), buy_signature=hs[0]["sig"], opened_at=first,
                fills=[{"side": "buy_hand", "sigs": [h["sig"] for h in hs], "sol": sol_out, "tokens_raw": str(tokens),
                        "usd": round(usd_out, 2), "at": max(h["at"] for h in hs).isoformat()}]))
            lines.append(f"${sym}: ${usd_out:,.2f} entry")
            log.info("live: adopted %s bought by hand: %d raw for %.4f SOL ($%.2f)", mint[:8], tokens, sol_out, usd_out)
        await session.commit()
        if lines:
            target = self._take_capital_target()
            await self.notify(f"{self._tag()} 🧾 Tracking tokens you bought by hand"
                              + (f" (capital comes out at +{target:.0f}%)" if target else "") + ":\n" + "\n".join(lines))

    async def _take_capital(self, session: AsyncSession) -> None:
        """Position up LIVE_TAKE_CAPITAL_AT_PCT (value now + already sold vs cost): sell tokens worth what is still
        missing to get the cost back, once per position. The rest keeps following the leader's sells."""
        target = self._take_capital_target()
        if target is None or not hasattr(self.x, "token_prices"):
            return
        if time.monotonic() - self._priced < self.s.live_take_capital_check_seconds:
            return
        self._priced = time.monotonic()
        opened = (await session.execute(select(LivePosition).where(
            LivePosition.status == "open", *self._mine()))).scalars().all()
        def taken(p: LivePosition) -> bool:
            # capital was taken at this entry level already; a hand add-on raises the cost and allows it again
            return any(f.get("reason") == "take_capital" and float(f.get("cost_at") or p.cost_usd) >= p.cost_usd - 0.01
                       for f in (p.fills or []))

        capped = self.s.live_trim_cap_pct is not None
        todo = [p for p in opened
                if not self._hold(p.token_address)
                and not p.hands_off                                            # the owner manages it
                and not self._quick(p)                                         # quick exit has its own targets
                and not taken(p)
                and not (self.s.live_follow_after_trim and self._following(p))   # following the leader instead
                # with the cap: until that share of the coins is sold; without: until the cost is back
                and (self._cap_room(p) > int(p.tokens_bought_raw or 0) // 100 if capped
                     else (p.proceeds_usd or 0) < p.cost_usd)
                and int(p.tokens_raw) > 0 and not p.manual_sell_frac]
        self._up_seen = {k: v for k, v in self._up_seen.items() if k in {p.id for p in todo}}
        if not todo:
            return
        try:
            prices = dict(await self.x.token_prices(sorted({p.token_address for p in todo})) or {})
        except Exception as exc:
            log.warning("live: take-capital prices failed: %s", exc)
            prices = {}

        def worth(p: LivePosition, px: dict[str, float] | None) -> float | None:
            if not px or not px.get("usd"):
                return None
            return int(p.tokens_raw) / 10 ** int(px["decimals"]) * float(px["usd"]) + (p.proceeds_usd or 0)

        goal = 1 + target / 100
        unpriced = {p.token_address for p in todo if worth(p, prices.get(p.token_address)) is None}
        close = {p.token_address for p in todo if p.token_address not in unpriced
                 and worth(p, prices.get(p.token_address)) >= p.cost_usd * (1 + (goal - 1) * self.s.live_take_capital_fast_from)}
        near = sorted(unpriced | close)
        # the Jupiter gate is shared with swaps: every check only when a position is close to its target,
        # otherwise (no DexScreener price yet) at most every 10 s
        wait = self.s.live_take_capital_check_seconds if close else 10.0
        if near and time.monotonic() - self._fast_at < wait:
            near = []
        if near and hasattr(self.x, "token_prices_fast"):
            self._fast_at = time.monotonic()
            try:
                prices.update(await self.x.token_prices_fast(near))     # fresher where it matters
            except Exception as exc:
                log.warning("live: take-capital fast prices failed: %s", exc)
        for p in todo:
            px = prices.get(p.token_address)
            total = worth(p, px)
            if total is None:
                continue
            value = total - (p.proceeds_usd or 0)
            if value <= 0 or total < p.cost_usd * goal:
                self._up_seen.pop(p.id, None)
                continue
            self._up_seen[p.id] = self._up_seen.get(p.id, 0) + 1
            strong = total >= p.cost_usd * goal * self.s.live_take_capital_strong
            if self._up_seen[p.id] < 2 and not strong:   # two checks in a row, unless far past the target
                continue
            self._up_seen.pop(p.id, None)
            if capped:
                # the user's rule: at the target, sell up to LIVE_TRIM_CAP_PCT of the coins (48%), what trims
                # already sold counts; the rest waits for the leader's full exit
                share = (self._cap_room(p) + 0.5) / int(p.tokens_raw)
            else:
                share = min((p.cost_usd - (p.proceeds_usd or 0)) / value, 1.0)
            share = min(share, 0.98)             # always keep a profit bag (>= 0.99 would sell everything)
            if await self._sell(session, p, share, "take_capital"):
                fills = [dict(f) for f in (p.fills or [])]
                fills[-1]["cost_at"] = round(p.cost_usd, 2)     # the entry this take-capital paid back
                p.fills = fills
            await session.commit()

    async def _quick_exits(self, session: AsyncSession) -> None:
        """Quick-exit wallets: sell the whole position at +TP / -SL (two readings in a row, one when far past)
        or after the time limit."""
        if not any(ld.quick_exit for ld in self.a.leaders.values()):
            return
        if time.monotonic() - self._quick_at < self.s.live_quick_check_seconds:
            return
        self._quick_at = time.monotonic()
        opened = [p for p in (await session.execute(select(LivePosition).where(
            LivePosition.status == "open", *self._mine()))).scalars().all()
            if self._quick(p) and int(p.tokens_raw) > 0 and not p.manual_sell_frac
            and not any(x.get("side") == "sell_failed" for x in (p.fills or []))]
        if not opened:
            return
        now = _now()
        for p in [p for p in opened if now - utc(p.opened_at) >= self._quick_rule(p)[2]]:
            await self._sell(session, p, 1.0, "quick_time")
            await session.commit()
        opened = [p for p in opened if p.status == "open" and now - utc(p.opened_at) < self._quick_rule(p)[2]]
        if not opened or not hasattr(self.x, "token_prices"):
            return

        def ratio(p: LivePosition, px: dict[str, float] | None) -> float | None:
            if not px or not px.get("usd") or not p.cost_usd:
                return None
            held = int(p.tokens_raw) / 10 ** int(px["decimals"]) * float(px["usd"])
            return (held + (p.proceeds_usd or 0)) / p.cost_usd

        # Jupiter's price first: DexScreener can lag minutes behind on a fast pump.fun token (2026-11-21: dca was
        # +90% while DexScreener still showed +15%). One call covers every quick position.
        mints = sorted({p.token_address for p in opened})
        prices: dict[str, dict[str, float]] = {}
        if hasattr(self.x, "token_prices_fast"):
            try:
                prices = dict(await self.x.token_prices_fast(mints) or {})
            except Exception as exc:
                log.warning("live: quick-exit prices failed: %s", exc)
        missing = [m for m in mints if m not in prices]
        if missing:
            try:
                prices.update(await self.x.token_prices(missing) or {})
            except Exception as exc:
                log.warning("live: quick-exit backup prices failed: %s", exc)
        for p in opened:
            tp, sl, _ = self._quick_rule(p)
            r = ratio(p, prices.get(p.token_address))
            if r is None or sl < r < tp:
                self._quick_seen.pop(p.id, None)
                continue
            self._quick_seen[p.id] = self._quick_seen.get(p.id, 0) + 1
            far = r >= 1 + (tp - 1) * self.s.live_take_capital_strong or r <= sl * 0.9
            if self._quick_seen[p.id] < 2 and not far:       # two readings in a row, unless far past
                continue
            self._quick_seen.pop(p.id, None)
            await self._sell(session, p, 1.0, "take_profit" if r >= tp else "stop_loss")
            await session.commit()

    async def _targets(self, session: AsyncSession) -> None:
        """Owner-set targets on single positions (/live): sell the chosen share at +target, everything at the stop.
        Two readings in a row, like quick exit; Jupiter's price first, DexScreener as a backup."""
        if time.monotonic() - self._target_at < self.s.live_quick_check_seconds:
            return
        self._target_at = time.monotonic()
        rows = [p for p in (await session.execute(select(LivePosition).where(
            LivePosition.status == "open", *self._mine(),
            or_(LivePosition.target_pct.is_not(None), LivePosition.stop_pct.is_not(None),
                LivePosition.target_price.is_not(None), LivePosition.stop_price.is_not(None))))).scalars().all()
            if int(p.tokens_raw) > 0 and not p.manual_sell_frac
            and not any(x.get("side") == "sell_failed" for x in (p.fills or []))]
        if not rows or not hasattr(self.x, "token_prices"):
            return
        mints = sorted({p.token_address for p in rows})
        prices: dict[str, dict[str, float]] = {}
        if hasattr(self.x, "token_prices_fast"):
            try:
                prices = dict(await self.x.token_prices_fast(mints) or {})
            except Exception as exc:
                log.warning("live: target prices failed: %s", exc)
        missing = [m for m in mints if m not in prices]
        if missing:
            try:
                prices.update(await self.x.token_prices(missing) or {})
            except Exception as exc:
                log.warning("live: target backup prices failed: %s", exc)
        for p in rows:
            px = prices.get(p.token_address)
            if not px or not px.get("usd") or not p.cost_usd:
                continue
            # price now vs our average entry price (cost / tokens bought): "+50%" means the coin is up 50%
            bought = int(p.tokens_bought_raw or p.tokens_raw) / 10 ** int(px["decimals"])
            if bought <= 0:
                continue
            now_px = float(px["usd"])
            r = now_px / (p.cost_usd / bought)
            hit = None
            if p.stop_pct is not None and r <= 1 + p.stop_pct / 100:
                hit = "stop"
            elif p.stop_price is not None and now_px <= p.stop_price:
                hit = "stop"
            elif p.target_pct is not None and r >= 1 + p.target_pct / 100:
                hit = "target"
            elif p.target_price is not None and now_px >= p.target_price:
                hit = "target"
            if hit is None:
                self._target_seen.pop(p.id, None)
                continue
            self._target_seen[p.id] = self._target_seen.get(p.id, 0) + 1
            if self._target_seen[p.id] < 2:
                continue
            self._target_seen.pop(p.id, None)
            share = 1.0 if hit == "stop" else (p.target_share or 1.0)
            if await self._sell(session, p, share, "your_target" if hit == "target" else "your_stop"):
                if hit == "target":
                    p.target_pct = p.target_price = p.target_share = None   # done; a stop, if set, stays
                else:
                    p.target_pct = p.target_price = p.target_share = p.stop_pct = p.stop_price = None
            await session.commit()

    async def _curve_skip(self, session: AsyncSession, mint: str) -> str | None:
        if await is_paused(session):
            return "paused (/live on to resume)"
        if self.a.paused:
            return f"account {self.a.name} paused"
        if mint in self.s.blocked_mints():
            return "token blocked"
        if (await session.execute(select(LivePosition.id).where(
                LivePosition.token_address == mint, LivePosition.status == "open", *self._mine()))).first():
            return "already holding"
        open_cost = (await session.execute(select(func.sum(LivePosition.cost_usd)).where(
            LivePosition.status == "open", *self._mine()))).scalar() or 0.0
        if open_cost + self.a.size_usd > self.a.max_open_usd + 1e-6:
            return f"limit: ${open_cost:,.0f} already open (max ${self.a.max_open_usd:,.0f})"
        loss = await realized_24h(session, self.x.mode, self.a.name)
        if loss <= -self.a.max_daily_loss_usd:
            return f"daily loss limit hit (${loss:,.2f} in 24h)"
        if self.x.mode == "live":
            bal, px = await self.x.sol_balance(), await self.x.sol_price()
            if px and bal - self.s.live_sol_reserve < self.a.size_usd / px:
                return f"not enough SOL ({bal:.3f})"
        return None

    async def _curve(self, session: AsyncSession) -> None:
        """Real buys of the account's curve rule (app/curve_live.py): the paper engine decided, we buy now at the
        account's entry size, capped at LIVE_CURVE_MAX_CHASE_PCT above the decision price. The position is
        hands-off: sold at the rule's exit level, at its stop (from our fill) or when its max hold runs out."""
        strat = self.a.curve_strategy
        if not strat:
            return
        from app import curve_live

        sigs, self._curve_last = curve_live.take(self._curve_last, strat, self.s.live_curve_max_signal_age_s)
        for sig in sigs:
            sym = sig.symbol or sig.mint[:6]
            try:
                why = await self._curve_skip(session, sig.mint)
                sol_usd = await self.x.sol_price()
            except Exception as exc:
                why = f"checks failed: {exc}"
            if why:
                log.info("live[%s]: curve %s skip %s (%s)", self.a.name, strat, sym, why)
                if why.startswith(("not enough SOL", "daily loss", "limit:")):
                    key = "curve " + why.split(" ")[0]
                    if time.monotonic() - self._said.get(key, -1e9) >= 6 * 3600:
                        self._said[key] = time.monotonic()
                        await self.notify(f"{self._tag()} ⏸ curve {strat}: skipped ${sym} ({why})")
                continue
            decision_usd = sig.px_sol * sol_usd
            max_price = decision_usd * (1 + self.s.live_curve_max_chase_pct / 100)
            try:
                f = await self.x.buy(sig.mint, self.a.size_usd, max_price=max_price)
            except ChaseError as exc:
                log.info("live[%s]: curve %s skip %s (%s)", self.a.name, strat, sym, exc)
                continue
            except Exception as exc:
                log.warning("live[%s]: curve %s buy %s failed: %s", self.a.name, strat, sym, exc)
                await self.notify(f"{self._tag()} ❌ curve {strat}: buy ${sym} failed: {str(exc)[:160]}")
                continue
            now = _now()
            target = decision_usd * sig.target_ratio
            pos = LivePosition(
                mode=self.x.mode, account=self.a.name, token_address=sig.mint, symbol=sym[:60],
                leader_wallet=f"curve:{strat}"[:64], leader_trade_id=0, status="open", cost_usd=f.usd,
                sol_spent=f.sol, tokens_raw=str(f.tokens_raw), tokens_bought_raw=str(f.tokens_raw),
                buy_signature=f.signature, hands_off=True, target_price=target, target_share=1.0,
                stop_pct=sig.sl_pct, sell_by=now + timedelta(seconds=sig.max_hold_s),
                fills=[{"side": "buy", "sig": f.signature, "sol": f.sol, "tokens_raw": str(f.tokens_raw),
                        "usd": round(f.usd, 2), "at": now.isoformat(), "curve": strat, "why": sig.why,
                        "late_s": round(time.monotonic() - sig.at, 1),
                        "timing": getattr(f, "timing", None) or {}}])
            session.add(pos)
            await session.commit()
            log.info("live[%s]: curve %s BUY %s for %.4f SOL ($%.2f) %s", self.a.name, strat, sym, f.sol, f.usd,
                     f.signature)
            await self.notify(f"{self._tag()} 📈 curve {strat}: bought ${sym} for {f.sol:.4f} SOL (${f.usd:,.2f})\n"
                              f"{sig.why} · sell at ${target:.6g} or {sig.sl_pct:+.0f}% or after "
                              f"{sig.max_hold_s / 60:.0f} min\n{self._tx(f.signature)}")

    async def _sell_by(self, session: AsyncSession) -> None:
        """Positions with a time limit (curve trades): sell everything once it has passed."""
        now = _now()
        rows = (await session.execute(select(LivePosition).where(
            LivePosition.status == "open", *self._mine(), LivePosition.sell_by.is_not(None),
            LivePosition.sell_by <= now))).scalars().all()
        for p in rows:
            if int(p.tokens_raw or 0) <= 0 or p.manual_sell_frac \
                    or any(x.get("side") == "sell_failed" for x in (p.fills or [])):
                continue
            if await self._sell(session, p, 1.0, "quick_time"):
                p.target_pct = p.target_price = p.target_share = p.stop_pct = p.stop_price = None
            await session.commit()

    async def _orders(self, session: AsyncSession) -> None:
        """The owner's price orders: buy once the price reaches the trigger (app/orders.py)."""
        from app.orders import check_orders

        await check_orders(self, session)

    async def _housekeeping(self, session: AsyncSession) -> None:
        """Book hand sales; take the capital out at the target; retry failed sells; sell after max hold."""
        # a copied wallet just traded (WebSocket watcher): stop here, the next step takes it first
        for part in (self._curve, self._reconcile, self._adopt_hand_buys, self._take_capital, self._quick_exits,
                     self._targets, self._sell_by, self._orders):
            if self.wake is not None and self.wake.is_set():
                return
            await part(session)
        if self.wake is not None and self.wake.is_set():
            return
        opened = (await session.execute(select(LivePosition).where(
            LivePosition.status == "open", *self._mine()))).scalars().all()
        for pos in opened:
            if pos.manual_sell_frac:
                share, pos.manual_sell_frac = pos.manual_sell_frac, None
                await session.commit()        # clear first: a crash never repeats a manual sell
                await self._sell(session, pos, share, "manual")
                await session.commit()
                continue
            failed = [x for x in (pos.fills or []) if x.get("side") == "sell_failed"]
            if failed and len(failed) < MAX_SELL_ATTEMPTS:
                last = failed[-1]
                await self._sell(session, pos, float(last.get("share", 1.0)), last.get("reason", "retry"))
                await session.commit()
                continue
            age = _now() - utc(pos.opened_at)
            if (age >= timedelta(hours=self.a.max_hold_hours or self.s.live_max_hold_hours)
                    and not failed and not self._hold(pos.token_address) and pos.leader_wallet != MANUAL
                    and not pos.hands_off):
                await self._sell(session, pos, 1.0, "max_hold")
                await session.commit()
                continue
            stop = self.s.live_time_stop_minutes
            if (stop and age >= timedelta(minutes=stop) and not failed and not self._capital_back(pos)
                    and not self._hold(pos.token_address) and pos.leader_wallet != MANUAL and not pos.hands_off):
                # its capital never came back: the trades that sit for hours are where the losses gathered
                await self._sell(session, pos, 1.0, "time_stop")
                await session.commit()

    @staticmethod
    def _tx(sig: str) -> str:
        return "(simulated, nothing sent)" if sig.startswith("dry-") else f"https://solscan.io/tx/{sig}"


async def live_status(session: AsyncSession, settings: Settings | None = None) -> dict[str, Any]:
    """Status of every trading account. Pausing applies to all accounts."""
    s = settings or get_settings()
    mode = s.live_mode if s.live_mode in ("dry", "live") else "off"
    from app.accounts import rows as account_rows, to_account

    accounts = []
    for r in await account_rows(session, s):
        a = to_account(r, s, None)
        rows = (await session.execute(select(LivePosition).where(LivePosition.mode == mode, _acct(a.name))
                                      .order_by(LivePosition.opened_at.desc()).limit(200))).scalars().all()
        closed = [r for r in rows if r.status == "closed"]
        opened = [r for r in rows if r.status == "open"]
        accounts.append({
            "name": a.name, "active": r.enabled and (mode == "dry" or bool(r.key_enc) or (r.key_source == "env" and any(
                e.name == a.name and e.private_key for e in accounts_from_settings(s)))),
            "paused": a.paused, "enabled": r.enabled,
            "pubkey": await _get(session, _key(PUBKEY, a.name)) or r.pubkey, "wallets": a.wallets,
            "size_usd": a.size_usd, "max_open_usd": a.max_open_usd, "max_daily_loss_usd": a.max_daily_loss_usd,
            "open": opened, "closed": closed, "failed": [r for r in rows if r.status == "failed"],
            "open_cost_usd": sum(r.cost_usd for r in opened),
            "realized_usd": sum(r.pnl_usd or 0 for r in closed),
            "realized_24h": await realized_24h(session, mode, a.name),
        })
    return {"mode": mode, "paused": await is_paused(session), "accounts": accounts}


class LiveTrader:
    def __init__(self, settings: Settings | None = None) -> None:
        self.s = settings or get_settings()

    async def _send_to(self, chat_id: str, text: str) -> None:
        if not self.s.telegram_bot_token:
            return
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(f"https://api.telegram.org/bot{self.s.telegram_bot_token}/sendMessage",
                         json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True})

    async def _telegram(self, text: str) -> None:
        if not (self.s.telegram_bot_token and self.s.telegram_chat_id):
            return
        try:
            from app.db import SessionLocal
            from app.tg_prefs import get_prefs, live_kind

            async with SessionLocal() as session:          # the operator's switches on /admin
                prefs = await get_prefs(session, self.s.telegram_signal_alerts)
            if not prefs.get(live_kind(text), True):
                return
        except Exception:
            log.warning("live: telegram prefs unavailable; sending", exc_info=True)
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                await c.post(f"https://api.telegram.org/bot{self.s.telegram_bot_token}/sendMessage",
                             json={"chat_id": self.s.telegram_chat_id, "text": text,
                                   "disable_web_page_preview": True})
        except Exception:
            log.warning("live: telegram notify failed", exc_info=True)

    async def run_forever(self) -> None:
        """Run one engine per enabled account from the trading_accounts table and pick up edits made on the
        /accounts page every LIVE_ACCOUNTS_RELOAD_SECONDS, without a restart."""
        from app.accounts import ensure_tracked, load_accounts
        from app.db import SessionLocal

        mode = self.s.live_mode
        if mode not in ("dry", "live"):
            log.info("Live copy trading off (LIVE_MODE=%s)", mode)
            return
        self._screener = None
        if self.s.live_screen_enabled:
            from app.screen import TokenScreener

            self._screener = TokenScreener(self.s)
        if self.s.live_pump_prices and self.s.pumpportal_api_key:
            from app import pump_prices

            pump_prices.PUMP = pump_prices.PumpPrices(self.s)
            self._pump_task = asyncio.create_task(pump_prices.PUMP.run_forever())
            log.info("PumpPortal prices on (max %d coins at once)", self.s.live_pump_prices_max_coins)
        if self.s.live_pool_prices and self.s.live_rpc_url:
            from app import pool_prices

            pool_prices.POOL = pool_prices.PoolPrices(self.s)
            self._pool_task = asyncio.create_task(pool_prices.POOL.run_forever())
            log.info("On-chain pool prices on (max %d coins at once)", self.s.live_pool_prices_max_coins)
        running: dict[str, tuple[asyncio.Task, str | None, CopyEngine]] = {}
        said: set[str] = set()                 # idle notices already sent
        fast: tuple[asyncio.Task, frozenset[str]] | None = None
        try:
            while True:
                try:
                    async with SessionLocal() as session:
                        accounts = await load_accounts(session, self.s)
                        await ensure_tracked(session, sorted({w for a in accounts for w in a.wallets}))
                        await session.commit()
                except Exception:
                    log.exception("live: loading accounts failed")
                    accounts = None
                if accounts is not None:
                    fast = await self._sync(accounts, mode, running, said, fast)
                await asyncio.sleep(self.s.live_accounts_reload_seconds)
        finally:
            for task, _, _ in running.values():
                task.cancel()
            if fast:
                fast[0].cancel()

    async def _sync(self, accounts: list[Account], mode: str, running: dict, said: set, fast):
        want = {a.name: a for a in accounts}
        for name in list(running):
            task, key, eng = running[name]
            a = want.get(name)
            if a is None or a.private_key != key or task.done():
                task.cancel()
                running.pop(name)
                if a is None:
                    log.info("live[%s]: account disabled, engine stopped", name)
                    await self._telegram(f"⏹ Copy account [{name}] stopped (disabled on the accounts page).")
            else:
                eng.update(a)
        for name, a in want.items():
            if name in running:
                continue
            env = "LIVE_PRIVATE_KEY" if name == "main" else f"LIVE_PRIVATE_KEY_{name.upper()}"
            if not a.wallets and f"nowallets:{name}" not in said:
                # no copied wallets: still run its engine, which books hand sales, runs the owner's targets / stops
                # and the page's Sell button on the positions it holds (2026-11-23: a sale in Phantom on such an
                # account was never booked because its engine never started)
                log.info("live[%s]: no wallets to copy; managing its own positions only", name)
                said.add(f"nowallets:{name}")
            if mode == "live" and not a.private_key:
                if name not in said:
                    log.warning("live[%s]: no private key, account idle", name)
                    await self._telegram(f"⏸ Copy account [{name}] is idle: no private key ({env} on the server, "
                                         f"or the account's stored key can't be decrypted).")
                    said.add(name)
                continue
            try:
                x = JupiterExecutor(self.s, live=(mode == "live"), private_key=a.private_key)
            except Exception as exc:
                if name not in said:
                    log.error("live[%s]: bad private key (%s)", name, type(exc).__name__)
                    await self._telegram(f"❌ Copy account [{name}]: the private key is not a valid Solana key.")
                    said.add(name)
                continue
            said.discard(name)
            eng = CopyEngine(x, self.s, notify=self._telegram, account=a, screener=self._screener,
                             send_to=self._send_to)
            running[name] = (asyncio.create_task(self._run_account(a, x, mode, eng)), a.private_key, eng)
        wallets = frozenset(w for name in running for w in want[name].wallets)
        if self.s.live_fast_detection and self.s.live_rpc_url and (fast is None or fast[1] != wallets
                                                                   or fast[0].done()):
            if fast:
                fast[0].cancel()
            fast = None
            if wallets:
                from app.fast_watch import FastWatcher

                fast = (asyncio.create_task(FastWatcher(sorted(wallets), self.s).run_forever()), wallets)
        return fast

    async def _run_account(self, a: Account, x: JupiterExecutor, mode: str, engine: CopyEngine) -> None:
        from app.db import SessionLocal

        async with SessionLocal() as session:
            if x.pubkey:
                await _put(session, _key(PUBKEY, a.name), x.pubkey)
            await session.commit()
        log.info("Live copy trading started: account=%s mode=%s wallet=%s copying=%s $%.0f/trade max $%.0f open",
                 a.name, mode, x.pubkey, ",".join(a.wallets), a.size_usd, a.max_open_usd)
        await self._telegram(f"{engine._tag()} copy trading started\nCopying: {', '.join(a.wallets)}\n"
                             f"${a.size_usd:.0f} per buy · max ${a.max_open_usd:.0f} open · "
                             f"pause after ${a.max_daily_loss_usd:.0f} loss/24h"
                             + (f"\nTrading wallet: {x.pubkey}" if x.pubkey else ""))
        from app.fast_watch import WAKE

        wake = asyncio.Event()
        engine.wake = wake
        WAKE.append(wake)
        try:
            while True:
                t0 = time.monotonic()
                wake.clear()
                try:
                    async with SessionLocal() as session:
                        await engine.step(session)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("live[%s] step failed", a.name)
                # next tick, or at once when the WebSocket watcher records a copied trade
                try:
                    await asyncio.wait_for(wake.wait(),
                                           timeout=max(self.s.live_interval_seconds - (time.monotonic() - t0), 0.5))
                except asyncio.TimeoutError:
                    pass
        finally:
            WAKE.remove(wake)
            await x.close()
