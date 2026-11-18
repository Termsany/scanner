"""Buy alerts for wallets the operator tracks (`/track` in Telegram, or `python -m app.smart_money track`).

Every TRACKER_INTERVAL_SECONDS (default 30s), on free public RPCs:

  Solana  getSignaturesForAddress(wallet, until=<last seen>) -> getTransaction(jsonParsed) for each new,
          successful transaction -> the wallet's token balance delta per mint (pre vs post token balances).
          A positive delta of a non-quote mint (not SOL / USDC / USDT) is a buy.
  EVM     one eth_getLogs per chain for ERC-20 Transfer events *to* any tracked wallet since the last block,
          kept only when the wallet itself sent the transaction (unsolicited spam airdrops are dropped).

Buys are priced with DexScreener and kept when liquidity >= TRACKER_MIN_LIQUIDITY_USD and the buy is worth
>= TRACKER_MIN_BUY_USD. Repeat buys of the same token within TRACKER_REGROUP_MINUTES are recorded without a
new alert. A new wallet starts from "now": its history is never replayed. Rows land in `wallet_trades`;
the Telegram module (API process) delivers them, and the scanner adds bought tokens to its next cycle.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models import Chain, SmartWallet, Token, TrackerCursor, WalletTrade

log = logging.getLogger("tracker")

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
SEL_DECIMALS = "0x313ce567"
SOLANA_QUOTES = {
    "So11111111111111111111111111111111111111112",   # wSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}
EVM_QUOTES = {
    Chain.ethereum: {"0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
                     "0xdac17f958d2ee523a2206206994597c13d831ec7", "0x6b175474e89094c44da98b954eedeac495271d0f"},
    Chain.base: {"0x4200000000000000000000000000000000000006", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"},
    Chain.bsc: {"0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c", "0x55d398326f99059ff775485246999027b3197955",
                "0x8ac76a51cc950d9822d68b83fe1ad5f6f3e9c00d", "0xe9e7cea3dedca5984780bafc599bd69add087d56"},
}
EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


@dataclass
class Buy:
    chain: Chain
    wallet: str
    token: str
    amount: float
    tx_hash: str
    block_time: datetime | None = None


def _ui(entry: dict[str, Any]) -> float:
    ta = entry.get("uiTokenAmount") or {}
    v = ta.get("uiAmount")
    if v is None:
        try:
            v = float(ta.get("uiAmountString") or 0)
        except ValueError:
            v = 0.0
    return float(v or 0.0)


def solana_buys_from_tx(tx: dict[str, Any], wallet: str, signature: str) -> list[Buy]:
    """Token balance increases for `wallet` in a parsed transaction (quote mints excluded)."""
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return []
    keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    signers = {k.get("pubkey") for k in keys if isinstance(k, dict) and k.get("signer")}
    if keys and wallet not in signers:
        return []  # the wallet did not sign: airdrop / spam sent to it, not a buy
    delta: dict[str, float] = defaultdict(float)
    for e in meta.get("preTokenBalances") or []:
        if e.get("owner") == wallet:
            delta[e["mint"]] -= _ui(e)
    for e in meta.get("postTokenBalances") or []:
        if e.get("owner") == wallet:
            delta[e["mint"]] += _ui(e)
    bt = tx.get("blockTime")
    when = datetime.fromtimestamp(bt, tz=timezone.utc) if bt else None
    return [Buy(Chain.solana, wallet, mint, d, signature, when)
            for mint, d in delta.items() if d > 0 and mint not in SOLANA_QUOTES]


def evm_buys_from_logs(chain: Chain, logs: list[dict[str, Any]], senders: dict[str, str],
                       wallets: set[str], decimals: dict[str, int]) -> list[Buy]:
    """Incoming ERC-20 transfers to a tracked wallet in transactions that wallet sent itself."""
    total: dict[tuple[str, str, str], int] = defaultdict(int)
    for lg in logs:
        topics = lg.get("topics") or []
        if len(topics) < 3 or topics[0].lower() != TRANSFER_TOPIC:
            continue
        to = "0x" + topics[2][-40:].lower()
        token = str(lg.get("address", "")).lower()
        tx = str(lg.get("transactionHash", "")).lower()
        if to not in wallets or token in EVM_QUOTES.get(chain, set()):
            continue
        if senders.get(tx) != to:
            continue  # the wallet did not initiate this transaction: airdrop / spam / someone else's swap
        try:
            total[(to, token, tx)] += int(lg.get("data") or "0x0", 16)
        except ValueError:
            continue
    return [Buy(chain, w, tok, raw / 10 ** decimals.get(tok, 18), tx)
            for (w, tok, tx), raw in total.items() if raw > 0]


class WalletTracker:
    def __init__(self, settings: Settings | None = None, http: httpx.AsyncClient | None = None) -> None:
        self.s = settings or get_settings()
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(20.0), headers={"User-Agent": "alpha-scanner/0.1"})
        self._own = http is None
        self._last = 0.0
        self._lock = asyncio.Lock()
        self._backoff: dict[Chain, tuple[float, int]] = {}  # chain -> (paused until, last delay s)

    async def close(self) -> None:
        if self._own:
            await self.http.aclose()

    # ---- transport --------------------------------------------------------
    async def _rpc(self, url: str, payload: Any) -> Any:
        async with self._lock:
            wait = 0.2 - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                resp = await self.http.post(url, json=payload)
            finally:
                self._last = time.monotonic()
        resp.raise_for_status()
        body = resp.json()
        if isinstance(body, dict) and body.get("error"):
            raise RuntimeError(f"RPC error: {body['error']}")
        return body

    async def _sol(self, method: str, params: list[Any]) -> Any:
        return (await self._rpc(self.s.solana_rpc_url, {"jsonrpc": "2.0", "id": 1, "method": method,
                                                        "params": params})).get("result")

    # ---- cursors ----------------------------------------------------------
    @staticmethod
    async def _get_cursor(session: AsyncSession, key: str) -> str | None:
        row = await session.get(TrackerCursor, key)
        return row.value if row else None

    @staticmethod
    async def _set_cursor(session: AsyncSession, key: str, value: str) -> None:
        row = await session.get(TrackerCursor, key)
        if row is None:
            session.add(TrackerCursor(key=key, value=value))
        else:
            row.value = value

    # ---- Solana -------------------------------------------------------------
    async def poll_solana(self, session: AsyncSession, wallet: str) -> list[Buy]:
        key = f"sol:{wallet}"
        cursor = await self._get_cursor(session, key)
        if cursor is None:
            latest = await self._sol("getSignaturesForAddress", [wallet, {"limit": 1}]) or []
            await self._set_cursor(session, key, latest[0]["signature"] if latest else "-")
            return []  # start from now, never replay history
        opts: dict[str, Any] = {"limit": self.s.tracker_max_tx_per_poll, "commitment": "confirmed"}
        if cursor != "-":
            opts["until"] = cursor
        sigs = await self._sol("getSignaturesForAddress", [wallet, opts]) or []
        if not sigs:
            return []
        buys: list[Buy] = []
        for item in reversed(sigs):  # oldest first
            if item.get("err") is not None:
                continue
            try:
                # v1 transactions exist on mainnet; a client capped at v0 is refused (RPC error -32015)
                tx = await self._sol("getTransaction", [item["signature"], {
                    "encoding": "jsonParsed", "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}])
            except (RuntimeError, httpx.HTTPError) as exc:
                log.warning("skipping tx %s: %s", item["signature"][:12], exc)
                continue
            if tx:
                buys.extend(solana_buys_from_tx(tx, wallet, item["signature"]))
        await self._set_cursor(session, key, sigs[0]["signature"])
        return buys

    # ---- EVM ----------------------------------------------------------------
    async def poll_evm(self, session: AsyncSession, chain: Chain, wallets: set[str]) -> list[Buy]:
        url = self.s.tracker_evm_rpc_urls.get(chain.value)
        if not url or not wallets:
            return []
        key = f"evm:{chain.value}"
        latest = int((await self._rpc(url, {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber",
                                            "params": []}))["result"], 16)
        cursor = await self._get_cursor(session, key)
        if cursor is None:
            await self._set_cursor(session, key, str(latest))
            return []
        start = max(int(cursor) + 1, latest - self.s.tracker_max_evm_block_range)
        if start > latest:
            return []
        to_topics = ["0x" + w.removeprefix("0x").rjust(64, "0") for w in sorted(wallets)]
        logs: list[dict[str, Any]] = []
        span = max(1, self.s.tracker_evm_log_spans.get(chain.value, self.s.tracker_evm_log_span))
        lo = start
        while lo <= latest:
            hi = min(lo + span - 1, latest)
            logs += (await self._rpc(url, {"jsonrpc": "2.0", "id": 1, "method": "eth_getLogs", "params": [{
                "fromBlock": hex(lo), "toBlock": hex(hi), "topics": [TRANSFER_TOPIC, None, to_topics],
            }]})).get("result") or []
            await self._set_cursor(session, key, str(hi))  # progress survives a failure mid catch-up
            lo = hi + 1
        if not logs:
            return []
        txs = sorted({str(lg.get("transactionHash", "")).lower() for lg in logs})
        tokens = sorted({str(lg.get("address", "")).lower() for lg in logs})
        batch = [{"jsonrpc": "2.0", "id": i, "method": "eth_getTransactionByHash", "params": [h]}
                 for i, h in enumerate(txs)]
        batch += [{"jsonrpc": "2.0", "id": len(txs) + i, "method": "eth_call",
                   "params": [{"to": t, "data": SEL_DECIMALS}, "latest"]} for i, t in enumerate(tokens)]
        results: dict[int, Any] = {}
        for i in range(0, len(batch), 40):
            for r in await self._rpc(url, batch[i:i + 40]) or []:
                if isinstance(r, dict) and "id" in r:
                    results[int(r["id"])] = r.get("result")
        senders = {h: str((results.get(i) or {}).get("from", "")).lower() for i, h in enumerate(txs)}
        decimals: dict[str, int] = {}
        for i, t in enumerate(tokens):
            raw = results.get(len(txs) + i)
            try:
                d = int(raw, 16) if isinstance(raw, str) and raw not in ("0x", "") else 18
            except ValueError:
                d = 18
            decimals[t] = d if 0 <= d <= 36 else 18
        return evm_buys_from_logs(chain, logs, senders, wallets, decimals)

    # ---- enrichment + persistence -------------------------------------------
    async def _price(self, buys: list[Buy]) -> dict[tuple[Chain, str], dict[str, Any]]:
        from app.scanner_worker import DEXSCREENER_CHAIN, select_primary_pair

        out: dict[tuple[Chain, str], dict[str, Any]] = {}
        by_chain: dict[Chain, set[str]] = defaultdict(set)
        for b in buys:
            by_chain[b.chain].add(b.token)
        for chain, toks in by_chain.items():
            toks_l = sorted(toks)
            for i in range(0, len(toks_l), 30):
                chunk = toks_l[i:i + 30]
                try:
                    resp = await self.http.get(
                        f"https://api.dexscreener.com/tokens/v1/{DEXSCREENER_CHAIN[chain]}/{','.join(chunk)}")
                    pairs = resp.json() if resp.status_code == 200 else []
                except (httpx.HTTPError, ValueError):
                    pairs = []
                for t in chunk:
                    p = select_primary_pair(chain, t, pairs if isinstance(pairs, list) else [])
                    if p:
                        out[(chain, t)] = p
        return out

    async def record(self, session: AsyncSession, buys: list[Buy], labels: dict[tuple[Chain, str], str]) -> int:
        from app.scanner_worker import SKIP_SYMBOLS

        if not buys:
            return 0
        pairs = await self._price(buys)
        now = datetime.now(timezone.utc)
        regroup = now - timedelta(minutes=self.s.tracker_regroup_minutes)
        alerts = 0
        for b in buys:
            pair = pairs.get((b.chain, b.token))
            if not pair:
                continue  # no DEX pair: spam / not tradable
            base = pair.get("baseToken") or {}
            price = float(pair.get("priceUsd") or 0) or None
            liq = float((pair.get("liquidity") or {}).get("usd") or 0)
            usd = b.amount * price if price else None
            symbol = str(base.get("symbol") or "")
            if symbol.upper() in SKIP_SYMBOLS or liq < self.s.tracker_min_liquidity_usd:
                continue
            if usd is None or usd < self.s.tracker_min_buy_usd:
                continue
            exists = (await session.execute(select(WalletTrade.id).where(
                WalletTrade.chain == b.chain, WalletTrade.tx_hash == b.tx_hash, WalletTrade.token_address == b.token
            ))).first()
            if exists:
                continue
            previous = (await session.execute(
                select(WalletTrade.detected_at).where(WalletTrade.chain == b.chain, WalletTrade.wallet == b.wallet,
                                                      WalletTrade.token_address == b.token)
                .order_by(WalletTrade.detected_at.desc()).limit(1)
            )).scalar_one_or_none()
            recent = previous is not None and (previous if previous.tzinfo else previous.replace(tzinfo=timezone.utc)) >= regroup
            tok = (await session.execute(select(Token.alpha_score).where(
                Token.chain == b.chain, Token.address == b.token))).scalar_one_or_none()
            session.add(WalletTrade(
                chain=b.chain, wallet=b.wallet, wallet_label=labels.get((b.chain, b.wallet), ""),
                token_address=b.token, symbol=symbol[:64], name=str(base.get("name") or "")[:128], tx_hash=b.tx_hash,
                amount=b.amount, usd_value=usd, price_usd=price, liquidity_usd=liq,
                market_cap=float(pair.get("marketCap") or pair.get("fdv") or 0) or None, pair_url=pair.get("url"),
                alpha_score=tok, first_buy=previous is None, block_time=b.block_time,
                telegram_sent=recent,  # repeat buy inside the regroup window: recorded, no new alert
            ))
            if not recent:
                alerts += 1
                log.info("BUY %s %s bought %s (%s) ~$%.0f tx %s", b.chain.value, labels.get((b.chain, b.wallet)),
                         symbol, b.token, usd, b.tx_hash)
        await session.flush()
        return alerts

    # ---- loop ----------------------------------------------------------------
    async def poll_once(self, session: AsyncSession) -> dict[str, int]:
        rows = (await session.execute(select(SmartWallet).where(SmartWallet.track_buys.is_(True)))).scalars().all()
        labels = {(w.chain, w.address): w.label or w.address[:6] for w in rows}
        evm: dict[Chain, set[str]] = defaultdict(set)
        buys: list[Buy] = []
        errors = 0
        for w in rows:
            if w.chain == Chain.solana:
                try:
                    buys += await self.poll_solana(session, w.address)
                except Exception as exc:
                    errors += 1
                    log.warning("solana poll failed for %s: %r", w.address[:6], exc)
            elif EVM_ADDRESS.match(w.address):
                evm[w.chain].add(w.address.lower())
        now = time.monotonic()
        for chain, ws in evm.items():
            paused_until, _ = self._backoff.get(chain, (0.0, 0))
            if now < paused_until:
                continue  # rate-limited recently; the cursor keeps our place, nothing is lost
            try:
                buys += await self.poll_evm(session, chain, ws)
                self._backoff.pop(chain, None)
            except Exception as exc:
                errors += 1
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                    _, prev = self._backoff.get(chain, (0.0, 0))
                    delay = min(max(60, prev * 2), self.s.tracker_backoff_max_seconds)
                    self._backoff[chain] = (now + delay, delay)
                    log.warning("%s rate-limited (429); pausing it for %ss", chain.value, delay)
                else:
                    log.warning("%s poll failed: %r", chain.value, exc)
        alerts = await self.record(session, buys, labels)
        await session.commit()
        return {"wallets": len(rows), "buys": len(buys), "alerts": alerts, "errors": errors}

    async def run_forever(self) -> None:
        from app.db import SessionLocal

        if not self.s.tracker_enabled:
            return
        log.info("Wallet tracker started (every %ss)", self.s.tracker_interval_seconds)
        while True:
            t0 = time.monotonic()
            try:
                async with SessionLocal() as session:
                    await self.poll_once(session)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("wallet tracker poll failed")
            await asyncio.sleep(max(self.s.tracker_interval_seconds - (time.monotonic() - t0), 5))
