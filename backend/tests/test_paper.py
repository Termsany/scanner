import asyncio
import tempfile
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.models import Badge, Base, Chain, PaperPosition, Signal, Token, WalletTrade
from app.paper import paper_summary, pnl, process_entries, process_exits

LEADER = "DULX3e6vt1ZpHq3AdCUBZLPZ1CrhApdNKcewMPxZQRXf"
OTHER = "Other1111111111111111111111111111111111111"
MINT = "Mint111111111111111111111111111111111111111"


def _db(body):
    async def go():
        tmp = tempfile.mkdtemp()
        eng = create_async_engine(f"sqlite+aiosqlite:///{tmp}/p.db".replace("\\", "/"))
        async with eng.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        try:
            async with async_sessionmaker(eng, expire_on_commit=False)() as s:
                return await body(s)
        finally:
            await eng.dispose()
    return asyncio.run(go())


def _settings(**kw):
    return Settings(paper_size_usd=100.0, paper_slippage_pct=1.5, paper_mirror_wallets=[LEADER], **kw)


def _trade(i, wallet, side="buy", price=1.0, pct_sold=None, remaining=None, mint=MINT):
    now = datetime.now(timezone.utc)
    return WalletTrade(chain=Chain.solana, wallet=wallet, wallet_label=wallet[:4], token_address=mint, symbol="TOK",
                       tx_hash=f"tx{i}", amount=10, usd_value=500, price_usd=price, side=side, kind=side,
                       pct_sold=pct_sold, remaining_amount=remaining, block_time=now, detected_at=now)


def _prices(table):
    async def fetch(chain, addrs):
        return {a: table[a] for a in addrs if a in table}
    return fetch


def test_pnl_charges_slippage_both_ways():
    usd, pct = pnl(100, 1.0, 1.0, 1.5)
    assert -3.1 < usd < -2.9 and abs(pct - usd) < 1e-9


def test_no_backfill_then_follow_cluster_and_mirror():
    s = _settings()

    async def body(sess):
        sess.add(_trade(1, LEADER))           # before the engine ever ran: not traded
        await sess.commit()
        assert await process_entries(sess, s) == 0
        await sess.commit()

        sess.add(_trade(2, LEADER))
        sess.add(_trade(3, OTHER))
        await sess.commit()
        opened = await process_entries(sess, s)
        await sess.commit()
        rows = (await sess.execute(select(PaperPosition))).scalars().all()
        by = {r.strategy for r in rows}
        # follow (one per token), mirror (leader only), cluster (2 wallets in 10 min, counting the pre-run buy)
        assert by == {"wallet_follow", "wallet_mirror", "wallet_cluster"} and opened == 3
        assert sum(r.strategy == "wallet_mirror" for r in rows) == 1
        return True

    assert _db(body)


def test_mirror_partial_then_full_sell_and_follow_exit():
    s = _settings()

    async def body(sess):
        await process_entries(sess, s)        # set cursors
        await sess.commit()
        sess.add(_trade(1, LEADER, price=1.0))
        await sess.commit()
        await process_entries(sess, s)
        await sess.commit()

        # leader sells half at 2x
        sess.add(_trade(2, LEADER, side="sell", price=2.0, pct_sold=50.0, remaining=10))
        await sess.commit()
        await process_exits(sess, _prices({MINT: 2.0}), s)
        await sess.commit()
        pos = {p.strategy: p for p in (await sess.execute(select(PaperPosition))).scalars().all()}
        m = pos["wallet_mirror"]
        assert m.status == "open" and abs(m.remaining_frac - 0.5) < 1e-9 and m.proceeds_usd > 90
        assert pos["wallet_follow"].status == "closed" and pos["wallet_follow"].exit_reason == "leader_sold"

        # leader sells the rest at 3x
        sess.add(_trade(3, LEADER, side="sell", price=3.0, pct_sold=100.0, remaining=0))
        await sess.commit()
        await process_exits(sess, _prices({MINT: 3.0}), s)
        await sess.commit()
        await sess.refresh(m)
        assert m.status == "closed" and m.exit_reason == "leader_sold"
        # half at 2x + half at 3x, minus slippage: about +146%
        assert 140 < m.pnl_pct < 150

        summ = await paper_summary(sess, settings=s)
        mir = next(x for x in summ["strategies"] if x["name"] == "wallet_mirror")
        assert mir["closed"] == 1 and mir["wins"] == 1 and summ["mirror_wallets"] == [LEADER]
        return True

    assert _db(body)


def test_mirror_ignores_tp_sl_but_follow_hits_them():
    s = _settings()

    async def body(sess):
        await process_entries(sess, s)
        await sess.commit()
        sess.add(_trade(1, LEADER, price=1.0))
        await sess.commit()
        await process_entries(sess, s)
        await sess.commit()
        await process_exits(sess, _prices({MINT: 1.6}), s)
        await sess.commit()
        pos = {p.strategy: p for p in (await sess.execute(select(PaperPosition))).scalars().all()}
        assert pos["wallet_follow"].exit_reason == "take_profit"
        assert pos["wallet_mirror"].status == "open" and pos["wallet_mirror"].peak_pct > 59
        return True

    assert _db(body)


def test_signals_stop_loss_and_no_price():
    s = _settings(paper_no_price_minutes=60)

    async def body(sess):
        tok = Token(chain=Chain.solana, address=MINT, symbol="TOK")
        tok2 = Token(chain=Chain.solana, address="Mint2", symbol="TWO")
        sess.add_all([tok, tok2])
        await sess.flush()
        await process_entries(sess, s)
        await sess.commit()
        sess.add(Signal(token_id=tok.id, score=86, badge=Badge.strong_alpha, price_usd=1.0, payload={}))
        sess.add(Signal(token_id=tok2.id, score=81, badge=Badge.strong_alpha, price_usd=1.0, payload={}))
        await sess.commit()
        assert await process_entries(sess, s) == 3   # 80 + 85 for TOK, 80 for TWO
        await sess.commit()

        two = (await sess.execute(select(PaperPosition).where(PaperPosition.token_address == "Mint2"))).scalar_one()
        two.last_priced_at = datetime.now(timezone.utc) - timedelta(minutes=61)
        await sess.commit()
        await process_exits(sess, _prices({MINT: 0.7}), s)
        await sess.commit()
        rows = (await sess.execute(select(PaperPosition))).scalars().all()
        reasons = {(r.token_address, r.strategy): r.exit_reason for r in rows}
        assert reasons[(MINT, "signal_80")] == "stop_loss" and reasons[(MINT, "signal_85")] == "stop_loss"
        assert reasons[("Mint2", "signal_80")] == "no_price"
        return True

    assert _db(body)
