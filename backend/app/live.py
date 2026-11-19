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
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

import httpx
from sqlalchemy import func, select
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


class SwapError(Exception):
    pass


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
    async def buy(self, mint: str, usd: float) -> Fill: ...
    async def sell(self, mint: str, tokens_raw: int) -> Fill: ...
    async def close(self) -> None: ...


def load_keypair(secret: str):
    from solders.keypair import Keypair

    secret = secret.strip()
    if secret.startswith("["):
        return Keypair.from_bytes(bytes(json.loads(secret)))
    return Keypair.from_base58_string(secret)


class JupiterExecutor:
    """Quotes real routes. In dry mode stops there; in live mode signs and sends."""

    def __init__(self, s: Settings, live: bool) -> None:
        self.s = s
        self.mode = "live" if live else "dry"
        self.kp = load_keypair(s.live_private_key) if (live or s.live_private_key) else None
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

    async def sol_price(self) -> float:
        r = await self.http.get(f"{self.s.jupiter_api_url}/price/v3", params={"ids": SOL_MINT})
        r.raise_for_status()
        px = float((r.json().get(SOL_MINT) or {}).get("usdPrice") or 0)
        if px <= 0:
            raise SwapError("no SOL price")
        return px

    async def quote(self, input_mint: str, output_mint: str, amount: int) -> dict[str, Any]:
        r = await self.http.get(f"{self.s.jupiter_api_url}/swap/v1/quote", params={
            "inputMint": input_mint, "outputMint": output_mint, "amount": str(amount),
            "slippageBps": self.s.live_slippage_bps, "restrictIntermediateTokens": "true",
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

    async def _send(self, q: dict[str, Any]) -> tuple[str, bool]:
        from solders.transaction import VersionedTransaction

        r = await self.http.post(f"{self.s.jupiter_api_url}/swap/v1/swap", json={
            "quoteResponse": q, "userPublicKey": self.pubkey, "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": {"priorityLevelWithMaxLamports": {
                "maxLamports": self.s.live_priority_fee_lamports, "priorityLevel": "veryHigh"}},
        })
        if r.status_code != 200 or not r.json().get("swapTransaction"):
            raise SwapError(f"swap build failed ({r.status_code}): {r.text[:150]}")
        tx = VersionedTransaction.from_bytes(base64.b64decode(r.json()["swapTransaction"]))
        signed = VersionedTransaction(tx.message, [self.kp])
        sig = await self._rpc("sendTransaction", [base64.b64encode(bytes(signed)).decode(), {
            "encoding": "base64", "skipPreflight": False, "maxRetries": 3, "preflightCommitment": "confirmed"}])
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            st = ((await self._rpc("getSignatureStatuses", [[sig]])) or {}).get("value", [None])[0]
            if st:
                if st.get("err"):
                    raise SwapError(f"transaction failed: {st['err']}"[:250])
                if st.get("confirmationStatus") in ("confirmed", "finalized"):
                    return sig, True
        return sig, False   # not confirmed in time: the caller checks balances

    # ---- trades ------------------------------------------------------------
    async def buy(self, mint: str, usd: float) -> Fill:
        px = await self.sol_price()
        lamports = int(usd / px * LAMPORTS)
        q = await self.quote(SOL_MINT, mint, lamports)
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
        px = await self.sol_price()
        if self.mode == "dry":
            q = await self.quote(mint, SOL_MINT, tokens_raw)
            self._dry_n += 1
            return Fill(f"dry-{self._dry_n}", int(q["outAmount"]) / LAMPORTS, tokens_raw, px)
        held = await self.token_balance(mint)
        amount = min(tokens_raw, held)
        if amount <= 0:
            raise SwapError("no tokens left to sell")
        q = await self.quote(mint, SOL_MINT, amount)
        sol0 = await self.sol_balance()
        sig, ok = await self._send(q)
        await asyncio.sleep(1)
        sold, sol1 = held - await self.token_balance(mint), await self.sol_balance()
        if sold <= 0:
            raise SwapError(f"sell {sig} {'not confirmed' if not ok else 'sold nothing'}")
        return Fill(sig, max(sol1 - sol0, 0.0), sold, px)


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


async def realized_24h(session: AsyncSession, mode: str) -> float:
    since = _now() - timedelta(hours=24)
    v = (await session.execute(select(func.sum(LivePosition.pnl_usd)).where(
        LivePosition.mode == mode, LivePosition.status == "closed", LivePosition.closed_at >= since))).scalar()
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
    def __init__(self, executor: Executor, settings: Settings | None = None, notify=None) -> None:
        self.x = executor
        self.s = settings or get_settings()
        self.wallets = set(self.s.live_wallets)
        self.notify = notify or (lambda text: asyncio.sleep(0))

    def _tag(self) -> str:
        return "🟢 LIVE" if self.x.mode == "live" else "🧪 DRY-RUN"

    async def step(self, session: AsyncSession) -> None:
        max_id = (await session.execute(select(func.max(WalletTrade.id)))).scalar() or 0
        cur = await _get(session, CURSOR)
        if cur is None:                       # first run: copy from now on only
            await _put(session, CURSOR, str(max_id))
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
                await _put(session, CURSOR, str(t.id))
                await session.commit()        # one trade at a time: a crash never repeats a swap
            await _put(session, CURSOR, str(max_id))
            await session.commit()
        await self._housekeeping(session)

    # ---- buys --------------------------------------------------------------
    async def _skip_reason(self, session: AsyncSession, t: WalletTrade) -> str | None:
        if await is_paused(session):
            return "paused (/live on to resume)"
        seen = utc(t.block_time) or utc(t.detected_at)
        if seen and (_now() - seen).total_seconds() > MAX_TRADE_AGE_SECONDS:
            return f"leader buy is {(_now() - seen).total_seconds() / 60:.0f} min old"
        if (t.liquidity_usd or 0) < self.s.live_min_liquidity_usd:
            return f"liquidity ${t.liquidity_usd or 0:,.0f} < ${self.s.live_min_liquidity_usd:,.0f}"
        exists = (await session.execute(select(LivePosition.id).where(
            LivePosition.token_address == t.token_address, LivePosition.status == "open",
            LivePosition.mode == self.x.mode))).first()
        if exists:
            return "already holding"
        open_cost = (await session.execute(select(func.sum(LivePosition.cost_usd)).where(
            LivePosition.status == "open", LivePosition.mode == self.x.mode))).scalar() or 0.0
        if open_cost + self.s.live_size_usd > self.s.live_max_open_usd + 1e-6:
            return f"limit: ${open_cost:,.0f} already open (max ${self.s.live_max_open_usd:,.0f})"
        loss = await realized_24h(session, self.x.mode)
        if loss <= -self.s.live_max_daily_loss_usd:
            return f"daily loss limit hit (${loss:,.2f} in 24h)"
        if self.x.mode == "live":
            bal = await self.x.sol_balance()
            try:
                px = await self.x.sol_price()
            except Exception:
                px = None
            if px and bal - self.s.live_sol_reserve < self.s.live_size_usd / px:
                return f"not enough SOL ({bal:.3f})"
        return None

    async def _on_buy(self, session: AsyncSession, t: WalletTrade) -> None:
        sym = t.symbol or t.token_address[:6]
        why = await self._skip_reason(session, t)
        if why:
            log.info("live: skip buy %s (%s)", sym, why)
            if "already holding" not in why:
                await self.notify(f"{self._tag()} ⏭ Skipped ${sym}: {why}")
            return
        try:
            f = await self.x.buy(t.token_address, self.s.live_size_usd)
        except Exception as exc:
            log.warning("live: buy %s failed: %s", sym, exc)
            session.add(LivePosition(mode=self.x.mode, token_address=t.token_address, symbol=t.symbol or "",
                                     leader_wallet=t.wallet, leader_trade_id=t.id, status="failed",
                                     cost_usd=0.0, error=str(exc)[:300], closed_at=_now()))
            await self.notify(f"{self._tag()} ❌ Buy ${sym} failed: {str(exc)[:200]}")
            return
        session.add(LivePosition(
            mode=self.x.mode, token_address=t.token_address, symbol=t.symbol or "", leader_wallet=t.wallet,
            leader_trade_id=t.id, status="open", cost_usd=f.usd, sol_spent=f.sol, tokens_raw=str(f.tokens_raw),
            tokens_bought_raw=str(f.tokens_raw), buy_signature=f.signature,
            fills=[{"side": "buy", "sig": f.signature, "sol": f.sol, "tokens_raw": str(f.tokens_raw),
                    "usd": round(f.usd, 2), "at": _now().isoformat()}],
        ))
        log.info("live: BUY %s for %.4f SOL ($%.2f) %s", sym, f.sol, f.usd, f.signature)
        await self.notify(f"{self._tag()} ✅ Bought ${sym} for {f.sol:.4f} SOL (${f.usd:,.2f})\n"
                          f"Copying {t.wallet_label or t.wallet[:6]}\n{self._tx(f.signature)}")

    # ---- sells -------------------------------------------------------------
    async def _on_sell(self, session: AsyncSession, t: WalletTrade) -> None:
        pos = (await session.execute(select(LivePosition).where(
            LivePosition.token_address == t.token_address, LivePosition.leader_wallet == t.wallet,
            LivePosition.status == "open", LivePosition.mode == self.x.mode))).scalars().first()
        if pos is None:
            return
        pos.leader_trade_id = t.id
        await self._sell(session, pos, _share(t), "leader_sold" if _share(t) >= 0.99 else "leader_trim")

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
        return True

    async def _housekeeping(self, session: AsyncSession) -> None:
        """Retry failed sells; sell everything after LIVE_MAX_HOLD_HOURS."""
        opened = (await session.execute(select(LivePosition).where(
            LivePosition.status == "open", LivePosition.mode == self.x.mode))).scalars().all()
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
            if _now() - utc(pos.opened_at) >= timedelta(hours=self.s.live_max_hold_hours) and not failed:
                await self._sell(session, pos, 1.0, "max_hold")
                await session.commit()

    @staticmethod
    def _tx(sig: str) -> str:
        return "(simulated, nothing sent)" if sig.startswith("dry-") else f"https://solscan.io/tx/{sig}"


async def live_status(session: AsyncSession, settings: Settings | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    mode = s.live_mode if s.live_mode in ("dry", "live") else "off"
    rows = (await session.execute(select(LivePosition).where(LivePosition.mode == mode)
                                  .order_by(LivePosition.opened_at.desc()).limit(200))).scalars().all()
    closed = [r for r in rows if r.status == "closed"]
    return {
        "mode": mode, "paused": await is_paused(session), "pubkey": await _get(session, PUBKEY),
        "wallets": list(s.live_wallets), "size_usd": s.live_size_usd, "max_open_usd": s.live_max_open_usd,
        "open": [r for r in rows if r.status == "open"], "closed": closed,
        "failed": [r for r in rows if r.status == "failed"],
        "realized_usd": sum(r.pnl_usd or 0 for r in closed),
        "realized_24h": await realized_24h(session, mode),
    }


class LiveTrader:
    def __init__(self, settings: Settings | None = None) -> None:
        self.s = settings or get_settings()

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
        from app.db import SessionLocal

        mode = self.s.live_mode
        if mode not in ("dry", "live"):
            log.info("Live copy trading off (LIVE_MODE=%s)", mode)
            return
        if not self.s.live_wallets:
            log.warning("Live copy trading: LIVE_WALLETS is empty, nothing to copy")
            return
        if mode == "live" and not self.s.live_private_key:
            log.error("LIVE_MODE=live but LIVE_PRIVATE_KEY is not set: not trading")
            return
        try:
            x = JupiterExecutor(self.s, live=(mode == "live"))
        except Exception as exc:
            log.error("Live copy trading: bad LIVE_PRIVATE_KEY (%s)", type(exc).__name__)
            return
        engine = CopyEngine(x, self.s, notify=self._telegram)
        async with SessionLocal() as session:
            if x.pubkey:
                await _put(session, PUBKEY, x.pubkey)
            await session.commit()
        log.info("Live copy trading started: mode=%s wallet=%s copying=%s $%.0f/trade max $%.0f open",
                 mode, x.pubkey, ",".join(self.s.live_wallets), self.s.live_size_usd, self.s.live_max_open_usd)
        await self._telegram(f"{engine._tag()} copy trading started\nCopying: {', '.join(self.s.live_wallets)}\n"
                             f"${self.s.live_size_usd:.0f} per buy · max ${self.s.live_max_open_usd:.0f} open"
                             + (f"\nTrading wallet: {x.pubkey}" if x.pubkey else ""))
        try:
            while True:
                t0 = time.monotonic()
                try:
                    async with SessionLocal() as session:
                        await engine.step(session)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("live step failed")
                await asyncio.sleep(max(self.s.live_interval_seconds - (time.monotonic() - t0), 1))
        finally:
            await x.close()
