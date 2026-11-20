"""Is this address a person's trading wallet, or a bot / program / contract?

Used before a wallet is added automatically (holder auto-discovery, Birdeye). On-chain facts only:

  Solana  owner of the account must be the System Program (user wallets are; PDAs and program accounts are
          owned by other programs); the last 1,000 transactions must not all fall within
          BOT_WINDOW_MINUTES (sustained >16 tx/min), and must not be mostly failed (sniper spam).
  EVM     no contract code at the address (EIP-7702 delegated EOAs, code 0xef0100..., are allowed);
          nonce (transactions sent) below BOT_NONCE.

Network trouble returns ok=True with reason "unchecked": a flaky RPC must not block discovery.
"""

from __future__ import annotations

import asyncio

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings
from app.models import Chain

log = logging.getLogger("wallet_check")

SYSTEM_PROGRAM = "11111111111111111111111111111111"
SIG_SAMPLE = 1000
BOT_WINDOW_MINUTES = 60          # 1,000 transactions inside an hour = sustained > 16 tx/min
FAILED_SHARE = 0.8               # with >= MIN_SAMPLE_FOR_FAILED signatures
MIN_SAMPLE_FOR_FAILED = 200
BOT_NONCE = 50_000
EIP7702_PREFIX = "0xef0100"


@dataclass
class CheckResult:
    ok: bool
    reason: str
    info: dict[str, Any]


# Solana checks run on the trading RPC (LIVE_RPC_URL) when set: at most 2 at a time, spaced out, so the scanner
# never crowds out the trading engine on the same plan.
_SOL_GATE = asyncio.Semaphore(2)
SOL_SPACING = 0.15


class WalletChecker:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self.s = settings
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(20.0), headers={"User-Agent": "alpha-scanner/0.1"})
        self._own = http is None

    async def close(self) -> None:
        if self._own:
            await self.http.aclose()

    async def _rpc(self, url: str, method: str, params: list[Any]) -> Any:
        resp = await self.http.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        resp.raise_for_status()
        body = resp.json()
        if body.get("error"):
            raise RuntimeError(str(body["error"])[:200])
        return body.get("result")

    async def _sol(self, url: str, method: str, params: list[Any]) -> Any:
        async with _SOL_GATE:
            try:
                return await self._rpc(url, method, params)
            finally:
                await asyncio.sleep(SOL_SPACING)

    async def check(self, chain: Chain, address: str) -> CheckResult:
        if not self.s.smart_activity_check_enabled:
            return CheckResult(True, "check disabled", {})
        try:
            return await (self._solana(address) if chain == Chain.solana else self._evm(chain, address))
        except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError) as exc:
            log.warning("activity check for %s %s failed: %r", chain.value, address[:8], exc)
            return CheckResult(True, "unchecked", {"error": str(exc)[:120]})

    async def _solana(self, address: str) -> CheckResult:
        from app.solana_pda import is_on_curve, pubkey_bytes

        # Off the ed25519 curve = program-derived address: no private key exists, it can never sign.
        if not is_on_curve(pubkey_bytes(address)):
            return CheckResult(False, "program-derived address (cannot sign)", {})
        url = self.s.live_rpc_url or self.s.solana_rpc_url
        acct = await self._sol(url, "getAccountInfo", [address, {"encoding": "base64"}])
        value = (acct or {}).get("value")
        # No account = 0 SOL. Still a normal keypair: another account can pay its fees, so not a rejection.
        owner = value.get("owner") if value else None
        if value is not None and owner != SYSTEM_PROGRAM:
            return CheckResult(False, f"not a user wallet (account owned by program {owner[:8]}…)", {"owner": owner})
        sigs = await self._sol(url, "getSignaturesForAddress", [address, {"limit": SIG_SAMPLE}]) or []
        times = [s["blockTime"] for s in sigs if s.get("blockTime")]
        failed = sum(1 for s in sigs if s.get("err") is not None)
        span_min = (max(times) - min(times)) / 60 if len(times) >= 2 else None
        info = {"sample": len(sigs), "failed": failed, "zero_sol": value is None,
                "span_minutes": round(span_min, 1) if span_min is not None else None}
        if len(sigs) >= SIG_SAMPLE and span_min is not None and span_min < BOT_WINDOW_MINUTES:
            rate = len(sigs) / max(span_min, 0.1)
            info["tx_per_min"] = round(rate, 1)
            return CheckResult(False, f"bot-like activity ({rate:,.0f} tx/min)", info)
        if len(sigs) >= MIN_SAMPLE_FOR_FAILED and failed / len(sigs) > FAILED_SHARE:
            return CheckResult(False, f"mostly failed transactions ({failed / len(sigs):.0%}): sniper bot", info)
        return CheckResult(True, "looks like a user wallet", info)

    async def _evm(self, chain: Chain, address: str) -> CheckResult:
        url = self.s.evm_rpc_urls.get(chain.value)
        if not url:
            return CheckResult(True, "unchecked", {"error": "no RPC for chain"})
        code = str(await self._rpc(url, "eth_getCode", [address, "latest"]) or "0x").lower()
        if code not in ("0x", "0x0") and not code.startswith(EIP7702_PREFIX):
            return CheckResult(False, "contract, not a wallet", {"code_bytes": (len(code) - 2) // 2})
        nonce = int(await self._rpc(url, "eth_getTransactionCount", [address, "latest"]) or "0x0", 16)
        info = {"nonce": nonce, "eip7702": code.startswith(EIP7702_PREFIX)}
        if nonce > BOT_NONCE:
            return CheckResult(False, f"bot-like nonce ({nonce:,} transactions sent)", info)
        return CheckResult(True, "looks like a user wallet", info)
