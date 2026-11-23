"""Our own pump.fun curve transactions (app/pump_tx.py): layout, sizes, what is refused, and the live path that
uses them first and falls back to PumpPortal."""

import asyncio
import base64
import struct

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from app import pump_tx
from tests.test_live import MINT_PUMP, _direct_exec

CREATOR = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"      # any valid key
BLOCKHASH = "4sGjMW1sUnHzSxGspuhpqLDx6wiyjNtZAMdL4VZHirAn"


def curve_bytes(v_sol=40.0, v_tok=800e6, complete=False, mayhem=False, quote=None) -> bytes:
    raw = bytearray(125)
    struct.pack_into("<5Q", raw, 8, int(v_tok * 1e6), int(v_sol * 1e9), int((v_tok - 279.9e6) * 1e6),
                     int((v_sol - 30) * 1e9), 10**15)
    raw[48] = int(complete)
    raw[49:81] = bytes(Pubkey.from_string(CREATOR))
    raw[81] = int(mayhem)
    if quote:
        raw[83:115] = bytes(Pubkey.from_string(quote))
    return bytes(raw)


def test_curve_parse_and_what_is_left_to_pumpportal():
    c = pump_tx.parse_curve(curve_bytes())
    assert c.creator == CREATOR and c.quote_mint is None and not c.complete and not c.mayhem
    pump_tx.check(c)
    for bad in (curve_bytes(complete=True), curve_bytes(mayhem=True), curve_bytes(quote=CREATOR)):
        try:
            pump_tx.check(pump_tx.parse_curve(bad))
            raise AssertionError("should be refused")
        except pump_tx.NotHere:
            pass


def test_buy_and_sell_layout_size_and_tip():
    kp = Keypair()
    c = pump_tx.parse_curve(curve_bytes())
    user = str(kp.pubkey())
    buy = pump_tx.accounts(MINT_PUMP, user, c, pump_tx.TOKEN_2022_PROGRAM)
    sell = pump_tx.accounts(MINT_PUMP, user, c, pump_tx.TOKEN_2022_PROGRAM, sell=True)
    assert len(buy) == 27 and len(sell) == 26
    assert [m.is_signer for m in buy].count(True) == 1 and buy[13].pubkey == kp.pubkey() and buy[13].is_signer
    assert [m.is_writable for m in buy] == [w for _, w in pump_tx.BUY_ACCOUNTS]
    assert str(buy[10].pubkey) == pump_tx.curve_address(MINT_PUMP)
    assert str(buy[16].pubkey) == pump_tx.pda(pump_tx.PUMP_PROGRAM, b"creator-vault", bytes(Pubkey.from_string(CREATOR)))

    raw, b64, tokens = pump_tx.build_buy(kp, MINT_PUMP, c, pump_tx.TOKEN_2022_PROGRAM, 100_000_000, 1500, 0.0005,
                                         BLOCKHASH, tip_lamports=50_000)
    tx = VersionedTransaction.from_bytes(raw)
    assert len(raw) <= 1232 and base64.b64decode(b64) == raw and tx.verify_with_results() == [True]
    keys = list(tx.message.account_keys)
    progs = [str(keys[i.program_id_index]) for i in tx.message.instructions]
    assert progs == [pump_tx.COMPUTE_BUDGET, pump_tx.COMPUTE_BUDGET, pump_tx.ATA_PROGRAM, pump_tx.PUMP_PROGRAM,
                     pump_tx.SYSTEM_PROGRAM]                                       # budget, ATA, buy, Sender tip
    data = bytes(tx.message.instructions[3].data)
    assert data[:8] == pump_tx.BUY_V2 and struct.unpack("<QQ", data[8:]) == (tokens, 115_000_000)
    assert 0 < tokens < 100_000_000 / 1e9 * 800e6 / 40 * 1e6                    # under the fee-free amount
    micro = struct.unpack("<Q", bytes(tx.message.instructions[1].data)[1:])[0]
    assert abs(micro * pump_tx.BUY_CU / 1e6 - 500_000) < 2                          # 0.0005 SOL priority in all

    raw, _, min_out = pump_tx.build_sell(kp, MINT_PUMP, c, pump_tx.TOKEN_2022_PROGRAM, 1_000_000_000, 1500, 0.0005,
                                         BLOCKHASH)
    tx = VersionedTransaction.from_bytes(raw)
    data = bytes(tx.message.instructions[2].data)
    assert data[:8] == pump_tx.SELL_V2 and struct.unpack("<QQ", data[8:]) == (1_000_000_000, min_out) and min_out > 0
    assert len(tx.message.instructions) == 3                                       # no tip asked: none added


def _local_exec(sim):
    x, calls = _direct_exec(sim=sim, live_local_pump_tx=True)
    rpc_calls = []

    async def rpc(method, params):
        rpc_calls.append(method)
        if method == "getMultipleAccounts":
            return {"value": [
                {"owner": pump_tx.PUMP_PROGRAM, "data": [base64.b64encode(curve_bytes()).decode(), "base64"]},
                {"owner": pump_tx.TOKEN_2022_PROGRAM, "data": ["", "base64"]}]}
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": BLOCKHASH, "lastValidBlockHeight": 1}}
        return None

    x._rpc = rpc
    x.sender_url = None
    return x, calls


def test_live_buy_uses_our_own_transaction_first():
    from app import live

    live._LOCAL_BAD.pop(MINT_PUMP, None)

    async def go():
        x, calls = _local_exec(sim=(100_000_000, int(1.7e9), None))
        try:
            f = await x.buy(MINT_PUMP, 30.0, max_price=0.31)
        finally:
            await x.close()
        return f, calls

    f, calls = asyncio.run(go())
    assert f.signature == "SIGD" and calls["pp"] == 0 and calls["sent"] == 1
    assert f.timing.get("local") == 1 and "local_build" in f.timing


def test_live_buy_falls_back_to_pumpportal_when_the_simulation_refuses_ours():
    from app import live

    live._LOCAL_BAD.pop(MINT_PUMP, None)

    async def go():
        # ours: an account error (a pump.fun upgrade, say); PumpPortal's: fine
        x, calls = _local_exec(sim=[(0, 0, {"InstructionError": [3, {"Custom": 3012}]}),
                                    (100_000_000, int(1.7e9), None)])
        try:
            f = await x.buy(MINT_PUMP, 30.0, max_price=0.31)
        finally:
            await x.close()
        return f, calls

    f, calls = asyncio.run(go())
    assert f.signature == "SIGD" and calls["pp"] == 1 and calls["sent"] == 1
    assert MINT_PUMP in live._LOCAL_BAD                     # PumpPortal for this coin for a while
    live._LOCAL_BAD.pop(MINT_PUMP, None)
