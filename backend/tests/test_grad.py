"""Paper graduation trades: pool lookup, fills after our delay, the +20% / +50% ladder, stop loss, max hold,
the confirm rule, and rows written across separate DB sessions (as the worker does)."""

import asyncio
import base64
import tempfile

from solders.pubkey import Pubkey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.grad import PUMPSWAP, WSOL, PaperGrad, decode_pool, find_pool
from app.models import Base, PaperPosition

MINT = str(Pubkey.new_unique())
BV, QV = str(Pubkey.new_unique()), str(Pubkey.new_unique())


def S(**kw):
    base = dict(grad_paper_enabled=True, grad_size_usd=30.0, grad_latency_s=2.0, grad_fee_pct=1.0,
                grad_slippage_pct=1.0, grad_sl_pct=-20.0, grad_tp1_pct=20.0, grad_tp1_frac=0.5, grad_tp2_pct=50.0,
                grad_max_hold_s=1800.0, grad_confirm_s=60.0, grad_confirm_min_pct=5.0, grad_max_open=40)
    base.update(kw)
    return Settings(**base)


def pool_b64(base_mint=MINT, quote_mint=WSOL):
    raw = bytearray(301)
    for off, key in ((43, base_mint), (75, quote_mint), (107, str(Pubkey.new_unique())), (139, BV), (171, QV)):
        raw[off:off + 32] = bytes(Pubkey.from_string(key))
    return base64.b64encode(bytes(raw)).decode()


def price(g, sol, now, tokens=1_000_000.0, mint=MINT):
    g.on_vault(mint, "base", tokens, now)
    g.on_vault(mint, "quote", sol, now)


def test_pool_is_found_in_the_migration_accounts():
    p = decode_pool(pool_b64())
    assert p["base_mint"] == MINT and p["quote_mint"] == WSOL and p["base_vault"] == BV and p["quote_vault"] == QV
    accts = [("A", None), ("B", {"owner": "other", "data": [pool_b64()]}),
             ("C", {"owner": PUMPSWAP, "data": [base64.b64encode(b"x" * 50).decode()]}),        # config, too short
             ("D", {"owner": PUMPSWAP, "data": [pool_b64(quote_mint=str(Pubkey.new_unique()))]}),  # not SOL
             ("E", {"owner": PUMPSWAP, "data": [pool_b64()]})]
    assert find_pool(accts, MINT) == {"pool": "E", **p}
    assert find_pool(accts, str(Pubkey.new_unique())) is None


def test_fill_after_delay_then_ladder_exit():
    g = PaperGrad(S())
    assert g.on_migration(MINT, 0.0) and not g.on_migration(MINT, 0.5)
    price(g, 100.0, 1.0)                                    # 1e-4 SOL / token
    assert not g.watch[MINT].held                           # our delay is 2 s
    g.tick(2.0)
    pos = g.watch[MINT].held["grad_all"]
    assert abs(pos["market"] - 1e-4) < 1e-15
    price(g, 121.0, 10.0)                                   # +21%: half sold
    assert pos["tp1"] and abs(pos["frac"] - 0.5) < 1e-9 and g.watch[MINT].held
    price(g, 151.0, 20.0)                                   # +51%: the rest
    c = g.closed[-1]
    assert c["reason"] == "take_profit" and not g.watch[MINT].held
    fill = 1e-4 * 1.02
    exp = 15 / fill * 1.21e-4 * 0.98 + 15 / fill * 1.51e-4 * 0.98 - 30
    assert abs(c["pnl"] - exp) < 1e-6 and c["pnl"] > 0


def test_stop_loss_and_max_hold():
    g = PaperGrad(S())
    g.on_migration(MINT, 0.0)
    price(g, 100.0, 0.5)
    g.tick(2.0)
    price(g, 79.0, 5.0)                                     # -21%
    assert g.closed[-1]["reason"] == "stop_loss" and g.closed[-1]["pnl"] < -6

    g2 = PaperGrad(S())
    g2.on_migration(MINT, 0.0)
    price(g2, 100.0, 0.5)
    g2.tick(2.0)
    price(g2, 125.0, 10.0)                                  # half out at +25%
    g2.tick(1803.0)                                         # 30 min: the rest at the last price
    c = g2.closed[-1]
    assert c["reason"] == "time" and c["pnl"] > 0


def test_confirm_buys_only_if_still_rising():
    g = PaperGrad(S())
    g.on_migration(MINT, 0.0)
    price(g, 100.0, 1.0)                                    # first price
    price(g, 110.0, 50.0)
    g.tick(60.0)                                            # +10% at 60 s: decided
    assert "grad_confirm" in g.watch[MINT].pending
    g.tick(62.0)
    assert set(g.watch[MINT].held) == {"grad_all", "grad_confirm"}

    flat = PaperGrad(S())
    flat.on_migration(MINT, 0.0)
    price(flat, 100.0, 1.0)
    price(flat, 102.0, 50.0)                                # only +2%
    flat.tick(60.0)
    assert "grad_confirm" in flat.watch[MINT].decided and "grad_confirm" not in flat.watch[MINT].pending


def test_unpriced_tokens_are_dropped():
    g = PaperGrad(S())
    g.on_migration(MINT, 0.0)
    assert g.tick(30.0) == []
    assert g.tick(125.0) == [MINT] and MINT not in g.watch   # no pool / price after 2 min


def test_rows_across_sessions_open_partial_and_closed():
    async def go():
        tmp = tempfile.mkdtemp()
        eng = create_async_engine(f"sqlite+aiosqlite:///{tmp}/g.db".replace("\\", "/"))
        async with eng.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        mk = async_sessionmaker(eng, expire_on_commit=False)
        try:
            g = PaperGrad(S(), sol_usd=100.0)
            g.on_migration(MINT, 0.0)
            price(g, 100.0, 0.5)
            g.tick(2.0)
            async with mk() as ses:
                await g.flush(ses)                          # opened
            price(g, 125.0, 10.0)                           # +25%: half sold, still open
            async with mk() as ses:
                await g.flush(ses)
            async with mk() as ses:
                mid = (await ses.execute(select(PaperPosition))).scalars().one()
            price(g, 75.0, 20.0)                            # -25%: stop on the rest
            async with mk() as ses:
                await g.flush(ses)
            async with mk() as ses:
                end = (await ses.execute(select(PaperPosition))).scalars().one()
            return mid, end
        finally:
            await eng.dispose()

    mid, end = asyncio.run(go())
    assert mid.status == "open" and mid.strategy == "grad_all" and abs(mid.remaining_frac - 0.5) < 1e-9
    assert mid.proceeds_usd > 15 and mid.pnl_usd > 0
    assert abs(mid.entry_price - 1e-4 * 100) < 1e-12                       # priced in USD
    assert end.status == "closed" and end.exit_reason == "stop_loss" and end.remaining_frac == 0.0
    fill = 1e-4 * 1.02
    exp = 15 / fill * 1.25e-4 * 0.98 + 15 / fill * 0.75e-4 * 0.98 - 30
    assert abs(end.pnl_usd - exp) < 1e-6


def test_price_waits_for_both_vaults_of_the_same_slot():
    g = PaperGrad(S())
    g.on_migration(MINT, 0.0)
    g.on_vault(MINT, "base", 1_000_000.0, 1.0, slot=10)
    g.on_vault(MINT, "quote", 100.0, 1.0, slot=10)
    assert abs(g.watch[MINT].price - 1e-4) < 1e-15
    g.on_vault(MINT, "base", 1_300_000.0, 2.0, slot=11)     # a sell: base moved first, quote not yet
    assert abs(g.watch[MINT].price - 1e-4) < 1e-15           # no fake -23% in between
    g.on_vault(MINT, "quote", 77.0, 2.0, slot=11)
    assert abs(g.watch[MINT].price - 77.0 / 1_300_000.0) < 1e-15
