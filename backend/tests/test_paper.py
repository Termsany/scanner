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
        # + the two DULX3e copies (LEADER is DULX3e, its $500 buy fits both)
        assert by == {"wallet_follow", "wallet_mirror", "wallet_cluster", "dulx_all", "dulx_50_1000"} and opened == 5
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


def _usd_trade(i, usd, **kw):
    t = _trade(i, LEADER, **kw)
    t.usd_value = usd
    return t


def test_dulx_paper_size_filter_and_live_exit_rule():
    s = _settings()

    async def body(sess):
        await process_entries(sess, s)
        await sess.commit()
        sess.add(_usd_trade(1, 500, price=1.0))                       # $500 buy: both strategies
        sess.add(_usd_trade(2, 2000, price=1.0, mint="Big1111"))      # $2000: only dulx_all
        await sess.commit()
        await process_entries(sess, s)
        await sess.commit()
        rows = (await sess.execute(select(PaperPosition).where(PaperPosition.strategy.like("dulx%")))).scalars().all()
        assert sorted((r.strategy, r.token_address) for r in rows) == [
            ("dulx_50_1000", MINT), ("dulx_all", "Big1111"), ("dulx_all", MINT)]

        # 2x without a leader trim: half out, then the leader's partial sell is ignored, full exit closes
        await process_exits(sess, _prices({MINT: 2.1, "Big1111": 1.0}), s)
        await sess.commit()
        m = next(r for r in rows if r.strategy == "dulx_all" and r.token_address == MINT)
        await sess.refresh(m)
        assert m.exit_mode == "half" and abs(m.remaining_frac - 0.5) < 1e-9 and m.status == "open"
        sess.add(_usd_trade(3, 100, side="sell", price=2.5, pct_sold=40.0, remaining=6))
        await sess.commit()
        await process_exits(sess, _prices({MINT: 2.5, "Big1111": 1.0}), s)
        await sess.commit()
        await sess.refresh(m)
        assert abs(m.remaining_frac - 0.5) < 1e-9 and m.status == "open"
        sess.add(_usd_trade(4, 100, side="sell", price=3.0, pct_sold=100.0, remaining=0))
        await sess.commit()
        await process_exits(sess, _prices({MINT: 3.0, "Big1111": 1.0}), s)
        await sess.commit()
        await sess.refresh(m)
        assert m.status == "closed" and m.exit_reason == "leader_sold" and m.pnl_pct > 100

        # trim before 2x: follow the same share, no 50% at 2x afterwards
        b = next(r for r in rows if r.token_address == "Big1111")
        sess.add(_usd_trade(5, 100, side="sell", price=1.2, pct_sold=30.0, remaining=7, mint="Big1111"))
        await sess.commit()
        await process_exits(sess, _prices({"Big1111": 1.2}), s)
        await sess.commit()
        await sess.refresh(b)
        assert b.exit_mode == "follow" and abs(b.remaining_frac - 0.7) < 1e-9
        await process_exits(sess, _prices({"Big1111": 2.5}), s)
        await sess.commit()
        await sess.refresh(b)
        assert abs(b.remaining_frac - 0.7) < 1e-9                    # no half at 2x in follow mode
        return True

    assert _db(body)


def test_entries_use_the_market_price_now_and_never_below_the_wallet():
    s = _settings()

    async def body(sess):
        await process_entries(sess, s)
        await sess.commit()
        sess.add(_trade(1, LEADER, price=1.0))
        sess.add(_trade(2, OTHER, price=1.0, mint="Cheap111"))
        await sess.commit()
        # a few seconds later: MINT already ran +40%, Cheap111 dipped below the wallet's price
        await process_entries(sess, s, prices=_prices({MINT: 1.4, "Cheap111": 0.8}))
        await sess.commit()
        rows = (await sess.execute(select(PaperPosition).where(PaperPosition.strategy == "wallet_follow"))).scalars().all()
        return {r.token_address: r.entry_price for r in rows}

    got = _db(body)
    assert got[MINT] == 1.4 and got["Cheap111"] == 1.0


def test_follow_sees_a_leader_sell_made_before_the_paper_entry():
    s = _settings()

    async def body(sess):
        await process_entries(sess, s)
        await sess.commit()
        sess.add(_trade(1, LEADER, price=1.0))
        await sess.commit()
        sess.add(_trade(2, LEADER, side="sell", price=1.1, pct_sold=100.0, remaining=0))   # sold right away
        await sess.commit()
        await process_entries(sess, s)            # the paper engine only now opens the buy
        await sess.commit()
        await process_exits(sess, _prices({MINT: 3.0}), s)
        await sess.commit()
        p = (await sess.execute(select(PaperPosition).where(PaperPosition.strategy == "wallet_follow"))).scalars().one()
        return p

    p = _db(body)
    assert p.status == "closed" and p.exit_reason == "leader_sold"     # not a +200% take profit
    assert -4 < p.pnl_pct < 0                                          # in and straight out: fees only


def test_a_wild_price_jump_is_ignored_until_it_repeats():
    """2026-11-23: a wrong pair priced hSOL ($81) at $396,965 and booked +$470k of paper profit."""
    import app.paper as paper

    s = _settings()
    paper._suspect.clear()

    async def body(sess):
        tok = Token(chain=Chain.solana, address=MINT, symbol="TOK")
        sess.add(tok)
        await sess.flush()
        await process_entries(sess, s)
        await sess.commit()
        sess.add(Signal(token_id=tok.id, score=86, badge=Badge.strong_alpha, price_usd=1.0, payload={}))
        await sess.commit()
        await process_entries(sess, s)
        await sess.commit()
        await process_exits(sess, _prices({MINT: 4900.0}), s)    # 4900x: a bad read
        await sess.commit()
        rows = (await sess.execute(select(PaperPosition))).scalars().all()
        assert all(r.status == "open" and r.last_price == 1.0 for r in rows)
        await process_exits(sess, _prices({MINT: 1.05}), s)      # back to normal: nothing happened
        await sess.commit()
        assert all(r.status == "open" for r in rows)
        for _ in range(3):                                       # a real 30x that holds is accepted
            await process_exits(sess, _prices({MINT: 30.0}), s)
            await sess.commit()
        rows = (await sess.execute(select(PaperPosition))).scalars().all()
        return [r.exit_reason for r in rows]

    assert set(_db(body)) == {"take_profit"}


def test_reset_starts_a_strategy_from_zero_and_keeps_its_trades():
    from app.paper import reset_strategy

    s = _settings()

    async def body(sess):
        tok = Token(chain=Chain.solana, address=MINT, symbol="TOK")
        sess.add(tok)
        await sess.flush()
        await process_entries(sess, s)
        await sess.commit()
        sess.add(Signal(token_id=tok.id, score=86, badge=Badge.strong_alpha, price_usd=1.0, payload={}))
        await sess.commit()
        await process_entries(sess, s)
        await sess.commit()
        await process_exits(sess, _prices({MINT: 0.5}), s)       # both signal strategies stop out
        await sess.commit()
        before = {x["name"]: x for x in (await paper_summary(sess, settings=s))["strategies"]}
        await reset_strategy(sess, "signal_80")
        after = {x["name"]: x for x in (await paper_summary(sess, settings=s))["strategies"]}
        kept = len((await sess.execute(select(PaperPosition))).scalars().all())
        return before, after, kept

    before, after, kept = _db(body)
    assert before["signal_80"]["closed"] == 1 and after["signal_80"]["closed"] == 0
    assert after["signal_80"]["realized_usd"] == 0 and after["signal_80"]["reset_at"]
    assert after["signal_85"]["closed"] == 1 and after["signal_85"]["reset_at"] is None   # others untouched
    assert kept == 2                                                                       # nothing deleted


def test_restart_and_feed_lost_closes_count_and_flag_the_strategy():
    s = _settings()
    now = datetime.now(timezone.utc)

    def pos(i, reason, pnl_usd):
        return PaperPosition(strategy="curve_70", chain=Chain.solana, token_address=f"M{i}", symbol="T",
                             trigger="t", status="closed", size_usd=10.0, entry_time=now, entry_price=1.0,
                             fill_price=1.0, tp_pct=0.0, sl_pct=-25.0, max_hold_minutes=60,
                             exit_time=now, exit_reason=reason, pnl_usd=pnl_usd,
                             pnl_pct=pnl_usd * 10, remaining_frac=0.0, proceeds_usd=10.0 + pnl_usd)

    async def body(sess):
        sess.add_all([pos(1, "graduated", 5.0), pos(2, "stop_loss", -2.5),
                      pos(3, "restart", -8.0), pos(4, "feed_lost", 9.0)])
        await sess.commit()
        return next(x for x in (await paper_summary(sess, settings=s))["strategies"] if x["name"] == "curve_70")

    st = _db(body)
    # counted at their last price (leaving them out biases long holds upward), and the strategy is flagged
    assert st["closed"] == 4 and st["interrupted"] == 2 and st["interrupted_pct"] == 50.0 and st["unreliable"]
    assert st["wins"] == 2 and st["win_rate"] == 0.5 and st["realized_usd"] == 3.5
    assert st["invested_usd"] == 40.0 and st["return_pct"] == 8.75 and st["worst_pct"] == -80.0
    assert st["exit_reasons"] == {"graduated": 1, "stop_loss": 1, "restart": 1, "feed_lost": 1}


def test_custom_curve_strategies_show_on_the_paper_page_and_can_be_reset():
    from app.models import CurveCustom
    from app.paper import reset_strategy

    s = _settings()

    async def body(sess):
        row = CurveCustom(level=30.0, exit_at=70.0, sl_pct=-30.0, size_usd=10.0, max_hold_min=60.0)
        sess.add(row)
        await sess.flush()
        row.name = f"cc{row.id}"
        await sess.commit()
        got = {x["name"]: x for x in (await paper_summary(sess, settings=s))["strategies"]}
        await reset_strategy(sess, row.name)                       # custom names can be reset too
        return got[row.name]

    st = _db(body)
    assert st["custom"]["enabled"] and "buy at 30% sold" in st["title"] and "sell all at 70% sold" in st["title"]
    assert st["sl_pct"] == -30.0 and st["tp_pct"] > 150                                  # ~+160% from 30% to 70%
