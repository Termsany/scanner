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
MAX_TRADE_AGE_SECONDS = 180
MAX_SELL_ATTEMPTS = 8
CURSOR = "live:wallet_trades"
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


class ChaseError(Exception):
    """The price already ran too far past the leader's: skip instead of buying the top."""

    def __init__(self, pay: float, limit: float) -> None:
        super().__init__(f"price ran: would pay {pay:.4g} vs limit {limit:.4g}")
        self.pay, self.limit = pay, limit


@dataclass
class Fill:
    signature: str
    sol: float           # SOL spent (buy) or received (sell)
    tokens_raw: int      # tokens received (buy) or sold (sell)
    sol_price: float     # USD per SOL at the time

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


# What a user is told about an exit: never which wallet, only what happened
USER_REASON = {"leader_sold": "exit signal", "leader_trim": "partial exit", "take_capital": "capital taken back",
               "sold_by_hand": "sold by hand", "manual": "sold from your page", "max_hold": "max hold time"}
USER_REASON_AR = {"leader_sold": "إشارة خروج", "leader_trim": "خروج جزئي", "take_capital": "سحب رأس المال",
                  "sold_by_hand": "اتباعت يدوي", "manual": "اتباعت من صفحتك", "max_hold": "انتهت مدة الاحتفاظ"}


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

    async def _slot(self) -> None:
        async with self._lock:
            wait = self._last + self.interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    async def request(self, http: httpx.AsyncClient, method: str, url: str, **kw) -> httpx.Response:
        r: httpx.Response | None = None
        for attempt in range(self.retries + 1):
            await self._slot()
            r = await http.request(method, url, **kw)
            if r.status_code != 429 or attempt == self.retries:
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
SOL_PRICE_TTL = 20.0
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
        res = await self._rpc("getTokenAccountsByOwner", [self.pubkey, {"mint": mint},
                                                          {"encoding": "jsonParsed", "commitment": "confirmed"}])
        total = 0
        for acc in (res or {}).get("value", []):
            info = acc["account"]["data"]["parsed"]["info"]
            total += int(info["tokenAmount"]["amount"])
        return total

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
        """{mint: {"usd", "decimals"}} from DexScreener (Jupiter only once per mint, for decimals)."""
        from app.api.live import jupiter_prices

        return await jupiter_prices(self.http, mints)

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
        tx = VersionedTransaction.from_bytes(base64.b64decode(body["swapTransaction"]))
        signed = VersionedTransaction(tx.message, [self.kp])
        raw = base64.b64encode(bytes(signed)).decode()
        # First send with preflight (a doomed swap fails here, not on-chain). Then re-broadcast the same signed
        # transaction every ~2 s until it confirms or its blockhash expires: under load, nodes drop transactions
        # and a single send often never lands.
        try:
            sig = await self._rpc("sendTransaction", [raw, {
                "encoding": "base64", "skipPreflight": False, "maxRetries": 0, "preflightCommitment": "confirmed"}])
        except SwapError as exc:
            if _is_slippage(exc):
                raise SlippageError("price moved past the slippage limit (simulation)") from exc
            raise
        deadline = time.monotonic() + 90
        n = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            n += 1
            st = ((await self._rpc("getSignatureStatuses", [[sig]])) or {}).get("value", [None])[0]
            if st:
                if st.get("err"):
                    if _is_slippage(st["err"]):
                        raise SlippageError("price moved past the slippage limit (on-chain, fee only)")
                    raise SwapError(f"transaction failed: {st['err']}"[:250])
                if st.get("confirmationStatus") in ("confirmed", "finalized"):
                    return sig, True
                continue        # processed: landed, waiting for confirmation; no need to resend
            if last_valid and n % 3 == 0:
                height = await self._rpc("getBlockHeight", [{"commitment": "confirmed"}])
                if isinstance(height, int) and height > last_valid:
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
        for attempt in range(3):   # nothing is spent when a swap is dropped or hits slippage: try again, fresh quote
            try:
                return await self._buy_once(mint, usd, max_price, bps)
            except Dropped as exc:
                if attempt == 2:
                    raise
                if isinstance(exc, SlippageError):
                    bps = self._wider(bps)
                log.warning("buy %s: %s; retrying with a fresh quote (slippage %.0f%%)", mint[:8], exc, bps / 100)
        raise SwapError("unreachable")

    async def _buy_once(self, mint: str, usd: float, max_price: float | None = None,
                        bps: int | None = None) -> Fill:
        decimals = None
        if max_price:   # one price call gives SOL's price and the token's decimals
            d = ((await self._prices([SOL_MINT, mint])).get(mint) or {}).get("decimals")
            if d is None:
                raise SwapError("token decimals unknown")
            decimals = int(d)
        px = await self.sol_price()
        lamports = int(usd / px * LAMPORTS)
        q = await self.quote(SOL_MINT, mint, lamports, bps)
        if decimals is not None:
            # price we would actually pay per token, from the live quote (includes price impact)
            pay = usd / (int(q["outAmount"]) / 10 ** decimals)
            if pay > max_price:
                raise ChaseError(pay, max_price)
        if self.mode == "dry":
            self._dry_n += 1
            return Fill(f"dry-{self._dry_n}", lamports / LAMPORTS, int(q["outAmount"]), px)
        tok0, sol0 = await self.token_balance(mint), await self.sol_balance()
        sig, ok = await self._send(q)
        await asyncio.sleep(1)
        got, sol1 = await self.token_balance(mint) - tok0, await self.sol_balance()
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
        q = await self.quote(mint, SOL_MINT, amount, bps)
        sol0 = await self.sol_balance()
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
        self._said: dict[str, float] = {}    # repeating skip notices -> last time sent
        self._beat = 0.0                     # monotonic time of the last heartbeat written
        self._up_seen: dict[int, int] = {}  # position id -> checks in a row at or above the target

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

    def _size(self, wallet: str) -> float:
        ld = self.a.leaders.get(wallet)
        return ld.size_usd if ld and ld.size_usd else self.a.size_usd

    def _hold(self, mint: str) -> bool:
        """Hold-only token: the bot never sells it by itself."""
        return mint in self.s.live_hold_mints

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
        if self._new_only(t.wallet):
            if t.remaining_amount is not None:
                before = t.remaining_amount - t.amount          # leader's balance before this buy
                if before > max(t.amount, 1.0) * 1e-6:
                    return "already holding: leader added to a token it held (new tokens only)"
            elif not t.first_buy:                               # older rows without balances: fall back
                return "already holding: leader bought this token before (new tokens only)"
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
        why = await self._skip_reason(session, t)
        if why:
            log.info("live: skip buy %s (%s)", sym, why)
            quiet = "already holding" in why or why.startswith("leader off")
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
        chase = self.s.live_max_chase_pct
        max_price = t.price_usd * (1 + chase / 100) if (t.price_usd and chase is not None and not free) else None
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
        session.add(LivePosition(
            mode=self.x.mode, account=self.a.name, token_address=t.token_address, symbol=t.symbol or "", leader_wallet=t.wallet,
            leader_trade_id=t.id, status="open", cost_usd=f.usd, sol_spent=f.sol, tokens_raw=str(f.tokens_raw),
            tokens_bought_raw=str(f.tokens_raw), buy_signature=f.signature,
            fills=[{"side": "buy", "sig": f.signature, "sol": f.sol, "tokens_raw": str(f.tokens_raw),
                    "usd": round(f.usd, 2), "at": _now().isoformat()}],
        ))
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
        if self._hold(t.token_address):
            sym = t.symbol or t.token_address[:6]
            if pos is not None:
                pos.leader_trade_id = t.id
                await session.commit()
                log.info("live: %s is hold-only: leader sold %.0f%%, keeping it", sym, share * 100)
                await self.notify(f"{self._tag()} 🔒 {t.wallet_label or t.wallet[:6]} sold {share * 100:.0f}% of "
                                  f"${sym}, but ${sym} is hold-only: keeping it (sell by hand if you want)")
            return
        if pos is not None:
            pos.leader_trade_id = t.id
            await self._sell(session, pos, share, "leader_sold" if share >= 0.99 else "leader_trim")
            await session.commit()
        if self.s.live_sell_manual_with_leader and self.x.mode == "live":
            await self._sell_manual(session, t, share)

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
        todo = [p for p in opened
                if not self._hold(p.token_address)
                and not any(f.get("reason") == "take_capital" for f in (p.fills or []))
                and (p.proceeds_usd or 0) < p.cost_usd and int(p.tokens_raw) > 0 and not p.manual_sell_frac]
        self._up_seen = {k: v for k, v in self._up_seen.items() if k in {p.id for p in todo}}
        if not todo:
            return
        try:
            prices = await self.x.token_prices(sorted({p.token_address for p in todo}))
        except Exception as exc:
            log.warning("live: take-capital prices failed: %s", exc)
            return
        for p in todo:
            px = (prices or {}).get(p.token_address)
            if not px or not px.get("usd"):
                continue
            value = int(p.tokens_raw) / 10 ** int(px["decimals"]) * float(px["usd"])
            if value <= 0 or (value + (p.proceeds_usd or 0)) < p.cost_usd * (1 + target / 100):
                self._up_seen.pop(p.id, None)
                continue
            self._up_seen[p.id] = self._up_seen.get(p.id, 0) + 1
            if self._up_seen[p.id] < 2:          # two checks in a row: one odd price never sells
                continue
            self._up_seen.pop(p.id, None)
            share = min((p.cost_usd - (p.proceeds_usd or 0)) / value, 1.0)
            share = min(share, 0.98)             # always keep a profit bag (>= 0.99 would sell everything)
            await self._sell(session, p, share, "take_capital")
            await session.commit()

    async def _housekeeping(self, session: AsyncSession) -> None:
        """Book hand sales; take the capital out at the target; retry failed sells; sell after max hold."""
        await self._reconcile(session)
        await self._take_capital(session)
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
            if (_now() - utc(pos.opened_at) >= timedelta(hours=self.a.max_hold_hours or self.s.live_max_hold_hours)
                    and not failed and not self._hold(pos.token_address)):
                await self._sell(session, pos, 1.0, "max_hold")
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
            if a is None or not a.wallets or a.private_key != key or task.done():
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
            if not a.wallets:
                if name not in said:
                    log.warning("live[%s]: no wallets to copy", name)
                    said.add(name)
                continue
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
