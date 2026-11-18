"""Direct on-chain check: which tracked smart wallets hold each scanned token.

Top-holder lists (RugCheck / GoPlus) only show the 10-20 biggest wallets, and good traders are rarely
among them. Here every (smart wallet, token) pair is checked directly, in batches, on free RPCs:

  Solana  derive the wallet's associated token account for the mint (both SPL Token and Token-2022
          programs, computed locally), then one getMultipleAccounts call per 100 accounts.
  EVM     ERC-20 balanceOf(wallet) + decimals() as JSON-RPC batches on public nodes.

A wallet counts as holding a token when its balance is worth >= HOLDINGS_MIN_USD (dust and airdrop
spam are ignored). Failures never break a scan: the cycle falls back to top-holder matching.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Any

import httpx

from app.config import Settings
from app.models import Chain
from app.repository import normalize_address
from app.schemas import MarketMetrics
from app.solana_pda import TOKEN_2022_PROGRAM, TOKEN_PROGRAM, associated_token_address

log = logging.getLogger("holdings")

SOLANA_BATCH = 100
EVM_BATCH = 40
SEL_BALANCE_OF = "0x70a08231"
SEL_DECIMALS = "0x313ce567"

Holdings = dict[tuple[Chain, str], list[str]]  # (chain, token address) -> wallet labels


class HoldingsChecker:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self.s = settings
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(12.0),
                                              headers={"User-Agent": "alpha-scanner/0.1"})
        self._own = http is None
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        if self._own:
            await self.http.aclose()

    async def _post(self, url: str, payload: Any) -> Any:
        async with self._lock:  # stay polite with free public RPCs
            wait = self.s.holdings_min_interval_seconds - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                resp = await self.http.post(url, json=payload)
            finally:
                self._last = time.monotonic()
        resp.raise_for_status()
        return resp.json()

    # ---- Solana ---------------------------------------------------------
    async def _solana(self, tokens: list[MarketMetrics], wallets: dict[str, str], out: Holdings) -> int:
        lookups: list[tuple[str, MarketMetrics, str]] = []  # (ata, token, wallet)
        for m in tokens:
            for w in wallets:
                for prog in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
                    try:
                        lookups.append((associated_token_address(w, m.address, prog), m, w))
                    except (ValueError, KeyError):
                        break  # malformed address
        lookups = lookups[: self.s.holdings_max_accounts]
        calls = 0
        for i in range(0, len(lookups), SOLANA_BATCH):
            chunk = lookups[i:i + SOLANA_BATCH]
            body = await self._post(self.s.solana_rpc_url, {
                "jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts",
                "params": [[a for a, _, _ in chunk], {"encoding": "jsonParsed", "commitment": "confirmed"}],
            })
            calls += 1
            for (ata, m, w), acc in zip(chunk, (body.get("result") or {}).get("value") or []):
                if not acc:
                    continue
                info = (((acc.get("data") or {}).get("parsed") or {}).get("info")) or {}
                if info.get("owner") != w or info.get("mint") != m.address:
                    continue
                amount = (info.get("tokenAmount") or {}).get("uiAmount") or 0.0
                if amount * (m.price_usd or 0.0) >= self.s.holdings_min_usd:
                    out[(Chain.solana, m.address)].append(wallets[w])
        return calls

    # ---- EVM ------------------------------------------------------------
    async def _evm(self, chain: Chain, tokens: list[MarketMetrics], wallets: dict[str, str], out: Holdings) -> int:
        url = self.s.evm_rpc_urls.get(chain.value)
        if not url:
            return 0
        reqs: list[tuple[dict[str, Any], MarketMetrics, str | None]] = []
        for m in tokens:
            reqs.append(({"to": m.address, "data": SEL_DECIMALS}, m, None))
            for w in wallets:
                data = SEL_BALANCE_OF + w.lower().removeprefix("0x").rjust(64, "0")
                reqs.append(({"to": m.address, "data": data}, m, w))
        reqs = reqs[: self.s.holdings_max_accounts]
        results: dict[int, str] = {}
        calls = 0
        for i in range(0, len(reqs), EVM_BATCH):
            chunk = reqs[i:i + EVM_BATCH]
            body = await self._post(url, [
                {"jsonrpc": "2.0", "id": i + j, "method": "eth_call", "params": [call, "latest"]}
                for j, (call, _, _) in enumerate(chunk)
            ])
            calls += 1
            for r in body if isinstance(body, list) else []:
                if isinstance(r, dict) and isinstance(r.get("result"), str):
                    results[int(r["id"])] = r["result"]
        decimals: dict[str, int] = {}
        for idx, (_, m, w) in enumerate(reqs):
            raw = results.get(idx)
            if raw is None or raw in ("0x", ""):
                continue
            value = int(raw, 16)
            if w is None:
                decimals[m.address] = value if 0 <= value <= 36 else 18
                continue
            if value == 0:
                continue
            amount = value / 10 ** decimals.get(m.address, 18)
            if amount * (m.price_usd or 0.0) >= self.s.holdings_min_usd:
                out[(chain, m.address)].append(wallets[w])
        return calls

    # ---- public ---------------------------------------------------------
    async def check(self, tokens: list[MarketMetrics], wallets: dict[Chain, dict[str, str]]) -> Holdings:
        out: Holdings = defaultdict(list)
        if not self.s.holdings_check_enabled:
            return out
        by_chain: dict[Chain, list[MarketMetrics]] = defaultdict(list)
        for m in tokens:
            by_chain[m.chain].append(m)
        jobs = []
        for chain, toks in by_chain.items():
            ws = {normalize_address(chain, a): label for a, label in (wallets.get(chain) or {}).items()}
            if not ws:
                continue
            jobs.append(self._solana(toks, ws, out) if chain == Chain.solana else self._evm(chain, toks, ws, out))
        if not jobs:
            return out
        try:
            results = await asyncio.wait_for(asyncio.gather(*jobs, return_exceptions=True),
                                             timeout=self.s.holdings_timeout_seconds)
        except asyncio.TimeoutError:
            log.warning("holdings check timed out after %ss; using partial results",
                        self.s.holdings_timeout_seconds)
            return out
        errors = [r for r in results if isinstance(r, BaseException)]
        for e in errors:
            log.warning("holdings check failed on one chain: %r", e)
        hits = sum(len(v) for v in out.values())
        calls = sum(r for r in results if isinstance(r, int))
        log.info("holdings check: %d tokens, %d rpc calls, %d smart-wallet positions", len(tokens), calls, hits)
        return out
