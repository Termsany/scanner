"""Who holds a pump.fun coin right now, rebuilt from its own transactions since the create (2026-11-26).

The hybrid curve feed streams PumpPortal trades only from 50 % of the curve, so the launch trades (bundle,
snipers) are never seen live. At the fill of a rule with holder filters (curve_70_organic_safe) we read the coin's
history from Helius instead (getTransactionsForAddress, oldest first) and replay the token balances.

On 682 past 70 % coins (11-23..11-25, checked on-chain): coins whose snipers had sold out by our entry did far
better (curve_70_organic +23.6 % vs -13.8 %), and coins with one holder at 12 %+ far worse (-39.7 %)."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

SUPPLY = 1e9            # pump.fun coins: 1B tokens
SNIPER_SLOTS = 2        # a first buy within this many slots of the create (~1 s) is a sniper / bundle
MAX_PAGES = 10
RETRY_WAITS = (0.3, 0.8, 1.5)


class NotPump(Exception):
    pass


async def _rpc(http: httpx.AsyncClient, url: str, method: str, params: list[Any]) -> Any:
    for wait in RETRY_WAITS + (None,):
        r = await http.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if r.status_code in (429, 503) and wait is not None:
            await asyncio.sleep(wait)
            continue
        r.raise_for_status()
        body = r.json()
        if body.get("error"):
            raise RuntimeError(f"{method}: {body['error']}"[:200])
        return body.get("result")
    raise RuntimeError(f"{method}: RPC busy")


def replay(mint: str, txs: list[dict[str, Any]]) -> dict[str, Any]:
    """Holder facts from the coin's successful transactions, oldest first (the first one is the create)."""
    from app import pump_tx

    bc = pump_tx.curve_address(mint)
    bal: dict[str, float] = {}
    first_buy: dict[str, int] = {}
    c_slot = creator = None
    for t in txs:
        meta = t.get("meta") or {}
        if meta.get("err"):
            continue
        if c_slot is None:
            if not any("Instruction: Create" in l for l in meta.get("logMessages") or []):
                raise NotPump("first transaction is not the create")
            c_slot = t.get("slot")
            creator = ((t.get("transaction") or {}).get("message") or {}).get("accountKeys", [None])[0]
        pre = {b["owner"]: b["uiTokenAmount"]["uiAmount"] or 0.0 for b in meta.get("preTokenBalances") or []
               if b.get("mint") == mint and b.get("owner")}
        post = {b["owner"]: b["uiTokenAmount"]["uiAmount"] or 0.0 for b in meta.get("postTokenBalances") or []
                if b.get("mint") == mint and b.get("owner")}
        for o in pre:
            post.setdefault(o, 0.0)
        for o, v in post.items():
            if v > pre.get(o, 0.0) + 1e-6 and o not in (bc, creator) and o not in first_buy:
                first_buy[o] = t.get("slot") or 0
            bal[o] = v
    if c_slot is None:
        raise NotPump("no history")
    held = {o: v for o, v in bal.items() if o != bc and v > 1e-6}
    top = sorted(held.values(), reverse=True)
    snipers = [o for o, s in first_buy.items() if s <= c_slot + SNIPER_SLOTS]
    return {
        "creator": creator, "holders": len(held),
        "top1_pct": round((top[0] if top else 0.0) / SUPPLY * 100, 2),
        "top10_pct": round(sum(top[:10]) / SUPPLY * 100, 2),
        "dev_hold_pct": round(bal.get(creator, 0.0) / SUPPLY * 100, 2),
        "sniper_n": len(snipers),
        "sniper_hold_pct": round(sum(bal.get(o, 0.0) for o in snipers) / SUPPLY * 100, 2),
    }


async def check(url: str, mint: str, http: httpx.AsyncClient | None = None,
                until: float | None = None) -> dict[str, Any]:
    """Holder facts for `mint` from its whole history up to `until` (unix time, default now). Raises on failure."""
    own = http is None
    http = http or httpx.AsyncClient(timeout=10)
    try:
        txs: list[dict[str, Any]] = []
        token = None
        for _ in range(MAX_PAGES):
            opts: dict[str, Any] = {
                "transactionDetails": "full", "sortOrder": "asc", "limit": 100, "encoding": "json",
                "maxSupportedTransactionVersion": 1,
                "filters": {"status": "succeeded", "blockTime": {"lte": int(until or time.time())}}}
            if token:
                opts["paginationToken"] = token
            res = await _rpc(http, url, "getTransactionsForAddress", [mint, opts]) or {}
            txs += res.get("data") or []
            token = res.get("paginationToken")
            if not token or not res.get("data"):
                break
        else:
            raise RuntimeError(f"more than {MAX_PAGES * 100} transactions")
        return replay(mint, txs)
    finally:
        if own:
            await http.aclose()
