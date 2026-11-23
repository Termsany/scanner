"""Our own pump.fun bonding-curve buy / sell transactions, without PumpPortal (2026-11-23).

PumpPortal builds a transaction over HTTP (~0.35-0.45 s a trade). Built here, a trade costs one RPC read (the
curve, the mint and a blockhash, together) instead. The account layout is pump.fun's current buy_v2 / sell_v2
(27 / 26 accounts, the same for every coin), as Chainstack's pump.fun bot documents and machine-checks it against
the IDL (github.com/chainstacklabs/pumpfun-bonkfun-bot, docs/pumpfun-protocol.md).

Only SOL-paired, non-mayhem coins still on the curve: anything else (graduated to PumpSwap, another quote asset,
mayhem) raises NotHere and the caller uses PumpPortal. Every transaction built here is simulated by the caller
before it is sent, so a wrong layout (a pump.fun upgrade) costs a fallback to PumpPortal, never a failed trade.
"""

from __future__ import annotations

import base64
import random
import struct
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
FEE_PROGRAM = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
GLOBAL = "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"
EVENT_AUTHORITY = "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1"
FEE_RECIPIENT = "CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM"
BUYBACK_FEE_RECIPIENTS = (
    "5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD", "9M4giFFMxmFGXtc3feFzRai56WbBqehoSeRE5GK7gf7",
    "GXPFM2caqTtQYC2cJ5yJRi9VDkpsYZXzYdwYpGnLmtDL", "3BpXnfJaUTiwXnJNe7Ej1rcbzqTTQUvLShZaWazebsVR",
    "5cjcW9wExnJJiqgLjq7DEG75Pm6JBgE1hNv4B2vHXUW6", "EHAAiTxcdDwQ3U4bU6YcMsQGaekdzLS3B5SmYo46kJtL",
    "5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD", "A7hAgCzFw14fejgCp387JUJRMNyz4j89JKnhtKU8piqW",
)
WSOL = "So11111111111111111111111111111111111111112"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"

BUY_V2 = bytes([184, 23, 238, 97, 103, 197, 211, 61])
SELL_V2 = bytes([93, 246, 130, 60, 231, 233, 64, 178])
BUY_CU, SELL_CU = 180_000, 120_000
FEE_ESTIMATE = 0.0125            # pump.fun protocol + creator fee on the SOL side (the simulation has the exact cost)

# buy_v2 account order (name, writable); sell_v2 is the same without global_volume_accumulator
BUY_ACCOUNTS = (
    ("global", False), ("base_mint", False), ("quote_mint", False), ("base_token_program", False),
    ("quote_token_program", False), ("associated_token_program", False), ("fee_recipient", True),
    ("associated_quote_fee_recipient", True), ("buyback_fee_recipient", True),
    ("associated_quote_buyback_fee_recipient", True), ("bonding_curve", True), ("associated_base_bonding_curve", True),
    ("associated_quote_bonding_curve", True), ("user", True), ("associated_base_user", True),
    ("associated_quote_user", True), ("creator_vault", True), ("associated_creator_vault", True),
    ("sharing_config", False), ("global_volume_accumulator", False), ("user_volume_accumulator", True),
    ("associated_user_volume_accumulator", True), ("fee_config", False), ("fee_program", False),
    ("system_program", False), ("event_authority", False), ("program", False),
)
SELL_ACCOUNTS = tuple(a for a in BUY_ACCOUNTS if a[0] != "global_volume_accumulator")


class NotHere(Exception):
    """This coin can't be traded with our own transaction: use PumpPortal."""


def _pk(s: str) -> Any:
    from solders.pubkey import Pubkey

    return Pubkey.from_string(s)


@lru_cache(maxsize=4096)
def pda(program: str, *seeds: bytes) -> str:
    from solders.pubkey import Pubkey

    return str(Pubkey.find_program_address(list(seeds), _pk(program))[0])


def ata(owner: str, mint: str, token_program: str) -> str:
    return pda(ATA_PROGRAM, bytes(_pk(owner)), bytes(_pk(token_program)), bytes(_pk(mint)))


def curve_address(mint: str) -> str:
    return pda(PUMP_PROGRAM, b"bonding-curve", bytes(_pk(mint)))


@dataclass
class Curve:
    v_tok: int
    v_sol: int
    real_tok: int
    real_sol: int
    complete: bool
    creator: str
    mayhem: bool
    quote_mint: str | None       # None: SOL


def parse_curve(raw: bytes) -> Curve:
    """BondingCurve: 8-byte discriminator, 5 u64 (virtual token, virtual quote, real token, real quote, supply),
    complete, creator, is_mayhem_mode, is_cashback_coin, quote_mint (all zeros for SOL), ..."""
    from solders.pubkey import Pubkey

    if len(raw) < 81:
        raise NotHere("curve account too short")
    v_tok, v_sol, r_tok, r_sol, _supply = struct.unpack_from("<5Q", raw, 8)
    complete = bool(raw[48])
    creator = str(Pubkey.from_bytes(raw[49:81]))
    mayhem = bool(raw[81]) if len(raw) > 81 else False
    quote = None
    if len(raw) >= 115 and any(raw[83:115]):
        quote = str(Pubkey.from_bytes(raw[83:115]))
    return Curve(v_tok, v_sol, r_tok, r_sol, complete, creator, mayhem, quote)


def tokens_for_sol(c: Curve, lamports: int) -> int:
    """Raw tokens a buy of `lamports` (fees included) gets on this curve."""
    net = int(lamports / (1 + FEE_ESTIMATE))
    return min(net * c.v_tok // (c.v_sol + net), c.real_tok)


def sol_for_tokens(c: Curve, tokens: int) -> int:
    """Lamports a sale of `tokens` brings, after fees."""
    gross = tokens * c.v_sol // (c.v_tok + tokens)
    return int(min(gross, c.real_sol) * (1 - FEE_ESTIMATE))


def accounts(mint: str, user: str, curve: Curve, base_program: str, sell: bool = False) -> list[Any]:
    from solders.instruction import AccountMeta

    bc = curve_address(mint)
    creator_vault = pda(PUMP_PROGRAM, b"creator-vault", bytes(_pk(curve.creator)))
    buyback = random.choice(BUYBACK_FEE_RECIPIENTS)
    uva = pda(PUMP_PROGRAM, b"user_volume_accumulator", bytes(_pk(user)))
    a = {
        "global": GLOBAL, "base_mint": mint, "quote_mint": WSOL, "base_token_program": base_program,
        "quote_token_program": TOKEN_PROGRAM, "associated_token_program": ATA_PROGRAM,
        "fee_recipient": FEE_RECIPIENT, "associated_quote_fee_recipient": ata(FEE_RECIPIENT, WSOL, TOKEN_PROGRAM),
        "buyback_fee_recipient": buyback,
        "associated_quote_buyback_fee_recipient": ata(buyback, WSOL, TOKEN_PROGRAM),
        "bonding_curve": bc, "associated_base_bonding_curve": ata(bc, mint, base_program),
        "associated_quote_bonding_curve": ata(bc, WSOL, TOKEN_PROGRAM), "user": user,
        "associated_base_user": ata(user, mint, base_program), "associated_quote_user": ata(user, WSOL, TOKEN_PROGRAM),
        "creator_vault": creator_vault, "associated_creator_vault": ata(creator_vault, WSOL, TOKEN_PROGRAM),
        "sharing_config": pda(FEE_PROGRAM, b"sharing-config", bytes(_pk(mint))),
        "global_volume_accumulator": pda(PUMP_PROGRAM, b"global_volume_accumulator"),
        "user_volume_accumulator": uva, "associated_user_volume_accumulator": ata(uva, WSOL, TOKEN_PROGRAM),
        "fee_config": pda(FEE_PROGRAM, b"fee_config", bytes(_pk(PUMP_PROGRAM))), "fee_program": FEE_PROGRAM,
        "system_program": SYSTEM_PROGRAM, "event_authority": EVENT_AUTHORITY, "program": PUMP_PROGRAM,
    }
    return [AccountMeta(_pk(a[name]), a[name] == user, w) for name, w in (SELL_ACCOUNTS if sell else BUY_ACCOUNTS)]


def create_ata_idempotent(payer: str, owner: str, mint: str, token_program: str) -> Any:
    """Associated token program, CreateIdempotent (1): payer, the ATA, owner, mint, system, token program."""
    from solders.instruction import AccountMeta, Instruction

    return Instruction(_pk(ATA_PROGRAM), bytes([1]), [
        AccountMeta(_pk(payer), True, True), AccountMeta(_pk(ata(owner, mint, token_program)), False, True),
        AccountMeta(_pk(owner), False, False), AccountMeta(_pk(mint), False, False),
        AccountMeta(_pk(SYSTEM_PROGRAM), False, False), AccountMeta(_pk(token_program), False, False)])


def _budget(cu: int, priority_fee_sol: float) -> list[Any]:
    from solders.instruction import Instruction

    micro = int(priority_fee_sol * 1e9 * 1e6 / cu)
    return [Instruction(_pk(COMPUTE_BUDGET), bytes([2]) + struct.pack("<I", cu), []),
            Instruction(_pk(COMPUTE_BUDGET), bytes([3]) + struct.pack("<Q", micro), [])]


def _tip(user: str, tip_lamports: int) -> list[Any]:
    if not tip_lamports:
        return []
    from solders.system_program import TransferParams, transfer

    from app.live import SENDER_TIP_ACCOUNTS

    return [transfer(TransferParams(from_pubkey=_pk(user), to_pubkey=_pk(random.choice(SENDER_TIP_ACCOUNTS)),
                                    lamports=int(tip_lamports)))]


def _sign(kp: Any, ixs: list[Any], blockhash: str) -> tuple[bytes, str]:
    from solders.hash import Hash
    from solders.message import MessageV0
    from solders.transaction import VersionedTransaction

    msg = MessageV0.try_compile(kp.pubkey(), ixs, [], Hash.from_string(blockhash))
    if hasattr(kp, "sign_message"):
        tx = VersionedTransaction(msg, [kp])
    else:                                   # a bare public key (checks by simulation only): unsigned
        from solders.signature import Signature

        tx = VersionedTransaction.populate(msg, [Signature.default()])
    raw = bytes(tx)
    if len(raw) > 1232:
        raise NotHere(f"transaction too big ({len(raw)} bytes)")
    return raw, base64.b64encode(raw).decode()


def build_buy(kp: Any, mint: str, curve: Curve, base_program: str, lamports: int, slippage_bps: int,
              priority_fee_sol: float, blockhash: str, tip_lamports: int = 0) -> tuple[bytes, str, int]:
    """Signed buy_v2 spending at most `lamports` * (1 + slippage): (bytes, base64, tokens asked for)."""
    from solders.instruction import Instruction

    check(curve)
    user = str(kp.pubkey())
    tokens = tokens_for_sol(curve, lamports)
    if tokens <= 0:
        raise NotHere("no tokens for that amount")
    max_cost = lamports * (10_000 + slippage_bps) // 10_000
    ixs = [*_budget(BUY_CU, priority_fee_sol),
           create_ata_idempotent(user, user, mint, base_program),
           Instruction(_pk(PUMP_PROGRAM), BUY_V2 + struct.pack("<QQ", tokens, max_cost),
                       accounts(mint, user, curve, base_program)),
           *_tip(user, tip_lamports)]
    raw, b64 = _sign(kp, ixs, blockhash)
    return raw, b64, tokens


def build_sell(kp: Any, mint: str, curve: Curve, base_program: str, tokens: int, slippage_bps: int,
               priority_fee_sol: float, blockhash: str, tip_lamports: int = 0) -> tuple[bytes, str, int]:
    """Signed sell_v2 of `tokens` raw, accepting `slippage` below the curve's payout: (bytes, base64, min out)."""
    from solders.instruction import Instruction

    check(curve)
    user = str(kp.pubkey())
    min_out = sol_for_tokens(curve, tokens) * (10_000 - slippage_bps) // 10_000
    ixs = [*_budget(SELL_CU, priority_fee_sol),
           Instruction(_pk(PUMP_PROGRAM), SELL_V2 + struct.pack("<QQ", tokens, max(min_out, 0)),
                       accounts(mint, user, curve, base_program, sell=True)),
           *_tip(user, tip_lamports)]
    raw, b64 = _sign(kp, ixs, blockhash)
    return raw, b64, min_out


def check(curve: Curve) -> None:
    if curve.complete:
        raise NotHere("graduated")
    if curve.quote_mint is not None:
        raise NotHere("not paired with SOL")
    if curve.mayhem:
        raise NotHere("mayhem coin")


async def read(rpc, mint: str) -> tuple[Curve, str, str]:
    """(curve, the mint's token program, a fresh blockhash) in one round trip. `rpc(method, params)`."""
    import asyncio

    accs, bh = await asyncio.gather(
        rpc("getMultipleAccounts", [[curve_address(mint), mint], {"encoding": "base64", "commitment": "processed"}]),
        rpc("getLatestBlockhash", [{"commitment": "confirmed"}]))
    vals = (accs or {}).get("value") or []
    if len(vals) != 2 or not vals[0] or not vals[1]:
        raise NotHere("no bonding curve")
    if vals[0].get("owner") != PUMP_PROGRAM:
        raise NotHere("curve not owned by pump.fun")
    curve = parse_curve(base64.b64decode(vals[0]["data"][0]))
    program = vals[1].get("owner")
    if program not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        raise NotHere(f"unknown token program {program}")
    return curve, program, ((bh or {}).get("value") or {})["blockhash"]
