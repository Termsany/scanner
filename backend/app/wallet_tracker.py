"""Buy and sell alerts for wallets the operator tracks (`/track` in Telegram, or the CLI).

Every TRACKER_INTERVAL_SECONDS (default 30s), on free public RPCs:

  Solana  getSignaturesForAddress(wallet, until=<last seen>) -> getTransaction(jsonParsed) for each new,
          successful transaction *signed by the wallet* -> the wallet's token balance delta per mint.
            delta > 0  -> BUY
            delta < 0  -> SELL when the wallet received SOL / USDC / USDT in the same transaction,
                          otherwise a TRANSFER out (recorded, no alert)
  EVM     eth_getLogs for ERC-20 Transfer events to and from tracked wallets (address-less, chunked), only in
          transactions the wallet sent. Incoming -> BUY. Outgoing -> SELL when the transaction went through a
          router / contract, TRANSFER when the wallet called the token contract directly.

Trades are priced with DexScreener and kept when liquidity >= TRACKER_MIN_LIQUIDITY_USD and the trade is worth
>= TRACKER_MIN_BUY_USD. Sells get the share of the position sold (from the remaining balance) and, when this
wallet's buys of the token were recorded, an estimated P&L against their average cost. Repeat trades of the same
token and side within TRACKER_REGROUP_MINUTES are recorded without a new alert, except full exits.
A new wallet starts from "now": its history is never replayed.
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
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models import Chain, SmartWallet, Token, TrackerCursor, WalletTrade

log = logging.getLogger("tracker")

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
SEL_DECIMALS = "0x313ce567"
SEL_BALANCE_OF = "0x70a08231"
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
MIN_SOL_RECEIVED_LAMPORTS = 1_000_000   # 0.001 SOL: more than a fee refund
FULL_EXIT_PCT = 99.0
SIG_PAGE = 1000                         # getSignaturesForAddress max page
QUOTA_BACKOFF_SECONDS = 1800            # provider says the plan's usage limit is reached


@dataclass
class Trade:
    chain: Chain
    wallet: str
    token: str
    amount: float                 # always positive
    tx_hash: str
    block_time: datetime | None = None
    side: str = "buy"             # buy | sell
    kind: str = "buy"             # buy | sell | transfer
    remaining: float | None = None  # wallet balance of the token after the trade (Solana: buys and sells)


Buy = Trade  # backwards-compatible name


def _ui(entry: dict[str, Any]) -> float:
    ta = entry.get("uiTokenAmount") or {}
    v = ta.get("uiAmount")
    if v is None:
        try:
            v = float(ta.get("uiAmountString") or 0)
        except ValueError:
            v = 0.0
    return float(v or 0.0)


def solana_trades_from_tx(tx: dict[str, Any], wallet: str, signature: str) -> list[Trade]:
    """Buys and sells of non-quote mints by `wallet` in a parsed transaction it signed."""
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return []
    keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    signers = {k.get("pubkey") for k in keys if isinstance(k, dict) and k.get("signer")}
    if keys and wallet not in signers:
        return []  # the wallet did not sign: airdrop / spam sent to it
    pre: dict[str, float] = defaultdict(float)
    post: dict[str, float] = defaultdict(float)
    for e in meta.get("preTokenBalances") or []:
        if e.get("owner") == wallet:
            pre[e["mint"]] += _ui(e)
    for e in meta.get("postTokenBalances") or []:
        if e.get("owner") == wallet:
            post[e["mint"]] += _ui(e)
    delta = {m: post[m] - pre[m] for m in set(pre) | set(post)}

    # Did the wallet receive value (native SOL or a quote token)? Distinguishes a sale from a transfer out.
    sol_in = 0
    idx = next((i for i, k in enumerate(keys) if isinstance(k, dict) and k.get("pubkey") == wallet), None)
    pb, qb = meta.get("preBalances") or [], meta.get("postBalances") or []
    if idx is not None and idx < len(pb) and idx < len(qb):
        sol_in = int(qb[idx]) - int(pb[idx])
    received = sol_in >= MIN_SOL_RECEIVED_LAMPORTS or any(delta.get(q, 0) > 0 for q in SOLANA_QUOTES)

    bt = tx.get("blockTime")
    when = datetime.fromtimestamp(bt, tz=timezone.utc) if bt else None
    out: list[Trade] = []
    for mint, d in delta.items():
        if mint in SOLANA_QUOTES or d == 0:
            continue
        if d > 0:   # remaining = balance after the buy, so remaining - amount = what it already held
            out.append(Trade(Chain.solana, wallet, mint, d, signature, when, remaining=max(post[mint], 0.0)))
        else:
            out.append(Trade(Chain.solana, wallet, mint, -d, signature, when, side="sell",
                             kind="sell" if received else "transfer", remaining=max(post[mint], 0.0)))
    return out


def solana_buys_from_tx(tx: dict[str, Any], wallet: str, signature: str) -> list[Trade]:
    return [t for t in solana_trades_from_tx(tx, wallet, signature) if t.side == "buy"]


def evm_trades_from_logs(chain: Chain, logs: list[dict[str, Any]], txinfo: dict[str, dict[str, str]],
                         wallets: set[str], decimals: dict[str, int]) -> list[Trade]:
    """ERC-20 transfers to/from tracked wallets, only in transactions the wallet sent itself.
    txinfo: tx hash -> {"from": ..., "to": ...} (lowercase)."""
    total: dict[tuple[str, str, str, str], int] = defaultdict(int)  # (side, wallet, token, tx) -> raw
    quotes = EVM_QUOTES.get(chain, set())
    for lg in logs:
        topics = lg.get("topics") or []
        if len(topics) < 3 or topics[0].lower() != TRANSFER_TOPIC:
            continue
        frm = "0x" + topics[1][-40:].lower()
        to = "0x" + topics[2][-40:].lower()
        token = str(lg.get("address", "")).lower()
        tx = str(lg.get("transactionHash", "")).lower()
        sender = (txinfo.get(tx) or {}).get("from")
        if token in quotes:
            continue
        try:
            raw = int(lg.get("data") or "0x0", 16)
        except ValueError:
            continue
        if to in wallets and sender == to:
            total[("buy", to, token, tx)] += raw
        if frm in wallets and sender == frm:
            total[("sell", frm, token, tx)] += raw
    out: list[Trade] = []
    for (side, w, tok, tx), raw in total.items():
        if raw <= 0:
            continue
        amount = raw / 10 ** decimals.get(tok, 18)
        if side == "buy":
            out.append(Trade(chain, w, tok, amount, tx))
        else:
            direct = (txinfo.get(tx) or {}).get("to") == tok  # called transfer() on the token itself
            out.append(Trade(chain, w, tok, amount, tx, side="sell", kind="transfer" if direct else "sell"))
    return out


def evm_buys_from_logs(chain: Chain, logs: list[dict[str, Any]], senders: dict[str, str],
                       wallets: set[str], decimals: dict[str, int]) -> list[Trade]:
    info = {h: {"from": f, "to": ""} for h, f in senders.items()}
    return [t for t in evm_trades_from_logs(chain, logs, info, wallets, decimals) if t.side == "buy"]


def _aware(dt: datetime | None) -> datetime | None:
    return None if dt is None else (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))


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
            gap = self.s.tracker_solana_min_interval if url == self.s.solana_rpc_url else 0.2
            wait = gap - (time.monotonic() - self._last)
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

    async def _batch(self, url: str, calls: list[dict[str, Any]]) -> dict[int, Any]:
        results: dict[int, Any] = {}
        for i in range(0, len(calls), 40):
            for r in await self._rpc(url, calls[i:i + 40]) or []:
                if isinstance(r, dict) and "id" in r:
                    results[int(r["id"])] = r.get("result")
        return results

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
    async def poll_solana(self, session: AsyncSession, wallet: str) -> list[Trade]:
        key = f"sol:{wallet}"
        cursor = await self._get_cursor(session, key)
        if cursor is None:
            latest = await self._sol("getSignaturesForAddress", [wallet, {"limit": 1}]) or []
            await self._set_cursor(session, key, latest[0]["signature"] if latest else "-")
            return []  # start from now, never replay history
        # One call returns up to 1,000 signatures since the cursor; only successful ones cost a getTransaction,
        # capped at TRACKER_MAX_TX_PER_POLL per poll (the cursor resumes the rest next poll).
        opts: dict[str, Any] = {"limit": SIG_PAGE, "commitment": "confirmed"}
        if cursor != "-":
            opts["until"] = cursor
        sigs = await self._sol("getSignaturesForAddress", [wallet, opts]) or []
        if not sigs:
            return []
        if len(sigs) >= SIG_PAGE and cursor != "-":
            # >1,000 transactions reference this address within one poll: a bot / program account, not a
            # person trading. Catching up is impossible on a public RPC; skip ahead and say so.
            log.warning("%s: %d+ transactions per poll (bot/program-like); skipping ahead", wallet[:6], SIG_PAGE)
            await self._set_cursor(session, key, sigs[0]["signature"])
            return []
        trades: list[Trade] = []
        fetched = 0
        for item in reversed(sigs):  # oldest first; the cursor only moves past what was actually read
            if item.get("err") is None and fetched >= self.s.tracker_max_tx_per_poll:
                break  # budget for this poll used; resume here next poll
            if item.get("err") is None:
                fetched += 1
                # HTTP errors (429, timeouts) propagate: the cursor stays at the last transaction read, so the
                # next poll retries from there and nothing is lost.
                try:
                    # v1 transactions exist on mainnet; a client capped at v0 is refused (RPC error -32015)
                    tx = await self._sol("getTransaction", [item["signature"], {
                        "encoding": "jsonParsed", "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}])
                except RuntimeError as exc:  # JSON-RPC error for this transaction: permanent, skip it
                    log.warning("skipping tx %s: %s", item["signature"][:12], exc)
                    tx = {}
                if tx is None:
                    break  # not available yet at "confirmed": retry next poll from here
                if tx:
                    trades.extend(solana_trades_from_tx(tx, wallet, item["signature"]))
            cursor = item["signature"]
            await self._set_cursor(session, key, cursor)
        return trades

    # ---- EVM ----------------------------------------------------------------
    async def poll_evm(self, session: AsyncSession, chain: Chain, wallets: set[str]) -> list[Trade]:
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
        padded = ["0x" + w.removeprefix("0x").rjust(64, "0") for w in sorted(wallets)]
        logs: list[dict[str, Any]] = []
        span = max(1, self.s.tracker_evm_log_spans.get(chain.value, self.s.tracker_evm_log_span))
        lo = start
        while lo <= latest:
            hi = min(lo + span - 1, latest)
            for topics in ([TRANSFER_TOPIC, None, padded], [TRANSFER_TOPIC, padded, None]):  # incoming, outgoing
                logs += (await self._rpc(url, {"jsonrpc": "2.0", "id": 1, "method": "eth_getLogs", "params": [{
                    "fromBlock": hex(lo), "toBlock": hex(hi), "topics": topics}]})).get("result") or []
            await self._set_cursor(session, key, str(hi))  # progress survives a failure mid catch-up
            lo = hi + 1
        if not logs:
            return []
        txs = sorted({str(lg.get("transactionHash", "")).lower() for lg in logs})
        tokens = sorted({str(lg.get("address", "")).lower() for lg in logs})
        calls = [{"jsonrpc": "2.0", "id": i, "method": "eth_getTransactionByHash", "params": [h]}
                 for i, h in enumerate(txs)]
        calls += [{"jsonrpc": "2.0", "id": len(txs) + i, "method": "eth_call",
                   "params": [{"to": t, "data": SEL_DECIMALS}, "latest"]} for i, t in enumerate(tokens)]
        results = await self._batch(url, calls)
        txinfo = {h: {"from": str((results.get(i) or {}).get("from", "")).lower(),
                      "to": str((results.get(i) or {}).get("to") or "").lower()} for i, h in enumerate(txs)}
        decimals: dict[str, int] = {}
        for i, t in enumerate(tokens):
            raw = results.get(len(txs) + i)
            try:
                d = int(raw, 16) if isinstance(raw, str) and raw not in ("0x", "") else 18
            except ValueError:
                d = 18
            decimals[t] = d if 0 <= d <= 36 else 18
        trades = evm_trades_from_logs(chain, logs, txinfo, wallets, decimals)
        sells = [t for t in trades if t.side == "sell"]
        if sells:  # remaining balance after the sale -> share of the position sold
            bal = await self._batch(url, [
                {"jsonrpc": "2.0", "id": i, "method": "eth_call", "params": [
                    {"to": t.token, "data": SEL_BALANCE_OF + t.wallet.removeprefix("0x").rjust(64, "0")}, "latest"]}
                for i, t in enumerate(sells)])
            for i, t in enumerate(sells):
                raw = bal.get(i)
                if isinstance(raw, str) and raw.startswith("0x") and len(raw) > 2:
                    t.remaining = int(raw, 16) / 10 ** decimals.get(t.token, 18)
        return trades

    # ---- enrichment + persistence -------------------------------------------
    async def _price(self, trades: list[Trade]) -> dict[tuple[Chain, str], dict[str, Any]]:
        from app.scanner_worker import DEXSCREENER_CHAIN, select_primary_pair

        out: dict[tuple[Chain, str], dict[str, Any]] = {}
        by_chain: dict[Chain, set[str]] = defaultdict(set)
        for t in trades:
            by_chain[t.chain].add(t.token)
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

    @staticmethod
    def _side_filter(side: str):
        # rows written before sells existed have side = NULL and are buys
        return or_(WalletTrade.side == "buy", WalletTrade.side.is_(None)) if side == "buy" else WalletTrade.side == side

    async def _last_alerted(self, session: AsyncSession, t: Trade) -> datetime | None:
        return _aware((await session.execute(
            select(WalletTrade.detected_at).where(
                WalletTrade.chain == t.chain, WalletTrade.wallet == t.wallet, WalletTrade.token_address == t.token,
                self._side_filter(t.side), or_(WalletTrade.kind.is_(None), WalletTrade.kind != "transfer"))
            .order_by(WalletTrade.detected_at.desc()).limit(1)
        )).scalar_one_or_none())

    async def record(self, session: AsyncSession, trades: list[Trade], labels: dict[tuple[Chain, str], str]) -> int:
        from app.scanner_worker import SKIP_SYMBOLS

        if not trades:
            return 0
        pairs = await self._price(trades)
        regroup = datetime.now(timezone.utc) - timedelta(minutes=self.s.tracker_regroup_minutes)
        alerts = 0
        for t in trades:
            pair = pairs.get((t.chain, t.token))
            if not pair:
                continue  # no DEX pair: spam / not tradable
            base = pair.get("baseToken") or {}
            price = float(pair.get("priceUsd") or 0) or None
            liq = float((pair.get("liquidity") or {}).get("usd") or 0)
            usd = t.amount * price if price else None
            symbol = str(base.get("symbol") or "")
            if symbol.upper() in SKIP_SYMBOLS or liq < self.s.tracker_min_liquidity_usd:
                continue
            if usd is None or usd < self.s.tracker_min_buy_usd:
                continue
            exists = (await session.execute(select(WalletTrade.id).where(
                WalletTrade.chain == t.chain, WalletTrade.tx_hash == t.tx_hash, WalletTrade.token_address == t.token
            ))).first()
            if exists:
                continue
            previous = await self._last_alerted(session, t)
            recent = previous is not None and previous >= regroup

            pnl_usd = pnl_pct = pct_sold = cost = None
            first_buy = False
            if t.side == "buy":
                first_buy = previous is None
            else:
                if t.remaining is not None and t.amount + t.remaining > 0:
                    pct_sold = round(t.amount / (t.amount + t.remaining) * 100.0, 1)
                bought = (await session.execute(
                    select(func.sum(WalletTrade.amount), func.sum(WalletTrade.usd_value)).where(
                        WalletTrade.chain == t.chain, WalletTrade.wallet == t.wallet,
                        WalletTrade.token_address == t.token, self._side_filter("buy"))
                )).one()
                if bought[0] and bought[1] and price:
                    cost = bought[1] / bought[0]                    # average buy price, USD per token
                    pnl_usd = round(t.amount * (price - cost), 2)
                    pnl_pct = round((price / cost - 1.0) * 100.0, 1)
            full_exit = t.side == "sell" and pct_sold is not None and pct_sold >= FULL_EXIT_PCT
            alert = t.kind != "transfer" and (not recent or full_exit)

            tok = (await session.execute(select(Token.alpha_score).where(
                Token.chain == t.chain, Token.address == t.token))).scalar_one_or_none()
            session.add(WalletTrade(
                chain=t.chain, wallet=t.wallet, wallet_label=labels.get((t.chain, t.wallet), ""),
                token_address=t.token, symbol=symbol[:64], name=str(base.get("name") or "")[:128], tx_hash=t.tx_hash,
                amount=t.amount, usd_value=usd, price_usd=price, liquidity_usd=liq,
                market_cap=float(pair.get("marketCap") or pair.get("fdv") or 0) or None, pair_url=pair.get("url"),
                alpha_score=tok, first_buy=first_buy, block_time=t.block_time,
                side=t.side, kind=t.kind, remaining_amount=t.remaining, pct_sold=pct_sold,
                cost_basis_usd=cost, pnl_usd=pnl_usd, pnl_pct=pnl_pct,
                telegram_sent=not alert,  # repeat trade inside the regroup window / transfer: recorded, no alert
            ))
            if alert:
                alerts += 1
                log.info("%s %s %s %s (%s) ~$%.0f%s tx %s", t.kind.upper(), t.chain.value,
                         labels.get((t.chain, t.wallet)), symbol, t.token, usd,
                         f" pnl {pnl_usd:+.0f}" if pnl_usd is not None else "", t.tx_hash)
        await session.flush()
        return alerts

    # ---- loop ----------------------------------------------------------------
    async def poll_once(self, session: AsyncSession) -> dict[str, int]:
        rows = (await session.execute(select(SmartWallet).where(SmartWallet.track_buys.is_(True)))).scalars().all()
        labels = {(w.chain, w.address): w.label or w.address[:6] for w in rows}
        evm: dict[Chain, set[str]] = defaultdict(set)
        trades: list[Trade] = []
        errors = 0
        sol_limited = False
        for w in rows:
            if w.chain == Chain.solana:
                if sol_limited:
                    continue  # RPC said slow down: the other Solana wallets wait for the next poll
                try:
                    trades += await self.poll_solana(session, w.address)
                except Exception as exc:
                    errors += 1
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                        sol_limited = True
                        log.warning("solana rate-limited (429) at %s; resuming next poll", w.address[:6])
                    else:
                        log.warning("solana poll failed for %s: %r", w.address[:6], exc)
            elif EVM_ADDRESS.match(w.address):
                evm[w.chain].add(w.address.lower())
        now = time.monotonic()
        for chain, ws in evm.items():
            paused_until, _ = self._backoff.get(chain, (0.0, 0))
            if now < paused_until:
                continue  # rate-limited recently; the cursor keeps our place, nothing is lost
            try:
                trades += await self.poll_evm(session, chain, ws)
                self._backoff.pop(chain, None)
            except Exception as exc:
                errors += 1
                quota = isinstance(exc, RuntimeError) and "limit" in str(exc).lower()
                if quota or (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429):
                    _, prev = self._backoff.get(chain, (0.0, 0))
                    delay = QUOTA_BACKOFF_SECONDS if quota else min(max(60, prev * 2), self.s.tracker_backoff_max_seconds)
                    self._backoff[chain] = (now + delay, delay)
                    log.warning("%s %s; pausing it for %ss", chain.value,
                                "RPC plan usage limit reached" if quota else "rate-limited (429)", delay)
                else:
                    log.warning("%s poll failed: %r", chain.value, exc)
        alerts = await self.record(session, trades, labels)
        await session.commit()
        return {"wallets": len(rows), "buys": sum(t.side == "buy" for t in trades),
                "sells": sum(t.side == "sell" for t in trades), "alerts": alerts, "errors": errors}

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
