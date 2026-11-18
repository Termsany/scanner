import asyncio
import json

import httpx

from app.config import Settings
from app.models import Chain
from app.wallet_check import SYSTEM_PROGRAM, WalletChecker

S = Settings(solana_rpc_url="https://sol.test", evm_rpc_urls={"ethereum": "https://eth.test"},
             smart_activity_check_enabled=True)  # explicit: test_api turns it off via env


def run(handler, chain, addr):
    async def go():
        c = WalletChecker(S, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        try:
            return await c.check(chain, addr)
        finally:
            await c.http.aclose()
    return asyncio.run(go())


def sol(owner=SYSTEM_PROGRAM, n=50, span_s=86_400 * 3, failed=0, exists=True):
    def handler(req):
        body = json.loads(req.content)
        if body["method"] == "getAccountInfo":
            return httpx.Response(200, json={"result": {"value": {"owner": owner} if exists else None}})
        sigs = [{"signature": f"s{i}", "blockTime": 1_000_000 + int(span_s * i / max(n - 1, 1)),
                 "err": {"x": 1} if i < failed else None} for i in range(n)]
        return httpx.Response(200, json={"result": sigs})
    return handler


def test_solana_rules():
    assert run(sol(), Chain.solana, "W").ok
    r = run(sol(n=1000, span_s=6), Chain.solana, "W")
    assert not r.ok and "bot-like activity" in r.reason and r.info["tx_per_min"] == 10000.0
    assert "not a user wallet" in run(sol(owner="TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"), Chain.solana, "W").reason
    assert "mostly failed" in run(sol(n=300, failed=280), Chain.solana, "W").reason
    assert "no on-chain account" in run(sol(exists=False), Chain.solana, "W").reason
    assert run(sol(n=1000, span_s=86_400), Chain.solana, "W").ok  # 1,000 tx over a day: active trader, fine


def evm(code="0x", nonce=500):
    def handler(req):
        m = json.loads(req.content)["method"]
        return httpx.Response(200, json={"result": code if m == "eth_getCode" else hex(nonce)})
    return handler


def test_evm_rules_and_rpc_failure_is_not_a_rejection():
    assert run(evm(), Chain.ethereum, "0x1").ok
    assert run(evm(code="0xef0100" + "ab" * 20), Chain.ethereum, "0x1").ok          # EIP-7702 wallet
    assert run(evm(code="0x6080604052"), Chain.ethereum, "0x1").reason == "contract, not a wallet"
    assert "bot-like nonce" in run(evm(nonce=120_000), Chain.ethereum, "0x1").reason
    flaky = run(lambda req: httpx.Response(503), Chain.ethereum, "0x1")
    assert flaky.ok and flaky.reason == "unchecked"
