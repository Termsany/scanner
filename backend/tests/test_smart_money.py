import asyncio
import tempfile
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.db import _add_missing_columns
from app.models import Badge, Base, Chain, MarketSnapshot, Signal, SmartWallet, Token, WalletSource
from app.schemas import HolderEntry, SecurityReport
from app.smart_money import SmartWalletRegistry, discover, eligible_holders, remove, upsert_manual

NOW = datetime.now(timezone.utc)
S = Settings(smart_auto_min_wins=2, smart_auto_min_win_rate=0.5, smart_auto_win_gain_pct=50.0)


def h(addr, pct=2.0, **kw):
    return {"address": addr, "pct": pct, "tag": None, "is_locked": False, **kw}


async def _seed(session):
    """W1 held A,B (both won). W2 held A,C (1/2). W3 held B,C,D (B,D won -> 2/3).
    POOL is tagged pool, DEV holds 35% -> both ignored."""
    plan = {
        "A": (True, [h("W1"), h("W2"), {"address": "POOL", "pct": 30, "tag": "pool", "is_locked": True}]),
        "B": (True, [h("W1"), h("W3"), h("DEV", 35)]),
        "C": (False, [h("W2"), h("W3")]),
        "D": (True, [h("W3")]),
    }
    for sym, (won, holders) in plan.items():
        t = Token(chain=Chain.solana, address=f"mint{sym}", symbol=sym, name=sym, price_usd=1.0,
                  discovery_sources=[], alpha_score=80, badge=Badge.strong_alpha)
        session.add(t)
        await session.flush()
        at = NOW - timedelta(hours=12)
        session.add(Signal(token_id=t.id, score=80, badge=Badge.strong_alpha, price_usd=1.0, payload={},
                           holders=holders, created_at=at))
        session.add(MarketSnapshot(token_id=t.id, price_usd=1.8 if won else 0.9, captured_at=at + timedelta(hours=1)))
    await session.commit()


def _run(coro_fn):
    async def go():
        tmp = tempfile.mkdtemp()
        eng = create_async_engine(f"sqlite+aiosqlite:///{tmp}/sm.db".replace("\\", "/"))
        async with eng.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(eng, expire_on_commit=False)
        try:
            async with Session() as s:
                await _seed(s)
                await coro_fn(s)
        finally:
            await eng.dispose()

    asyncio.run(go())


def test_discovery_qualifies_repeat_winners_only():
    async def body(s):
        stats = await discover(s, S)
        await s.commit()
        assert stats["qualified"] == 2 and stats["added"] == 2
        rows = {w.address: w for w in (await s.execute(select(SmartWallet))).scalars()}
        assert set(rows) == {"W1", "W3"}
        assert (rows["W1"].wins, rows["W1"].appearances, rows["W1"].label) == (2, 2, "auto 2/2")
        assert (rows["W3"].wins, rows["W3"].appearances) == (2, 3)
        assert rows["W3"].win_tokens[0]["gain_pct"] == 80.0 and rows["W3"].source == WalletSource.auto

    _run(body)


def test_user_disable_is_sticky_and_manual_survives():
    async def body(s):
        await discover(s, S)
        assert "disabled" in await remove(s, Chain.solana, "W1")
        await upsert_manual(s, Chain.solana, "W2", "My fund")
        await s.commit()
        await discover(s, S)
        await s.commit()
        rows = {w.address: w for w in (await s.execute(select(SmartWallet))).scalars()}
        assert rows["W1"].enabled is False and rows["W1"].disabled_by_user is True
        assert rows["W2"].enabled is True and rows["W2"].source == WalletSource.manual
        assert rows["W2"].label == "My fund" and (rows["W2"].wins, rows["W2"].appearances) == (1, 2)
        assert await remove(s, Chain.solana, "W2") == "deleted"

    _run(body)


def test_registry_matches_holders():
    async def body(s):
        await discover(s, S)
        await s.commit()
        reg = SmartWalletRegistry(S)
        await reg.refresh(s)
        sec = SecurityReport(chain=Chain.solana, address="x", providers=["rugcheck"],
                             top_holders=[HolderEntry(address="W3", pct=3.0), HolderEntry(address="ZZ", pct=1.0)])
        info = reg.evaluate(Chain.solana, sec)
        assert info.tracked_wallet_hits == ["auto 2/3"] and info.inferred is False
        assert reg.evaluate(Chain.base, sec).tracked_wallet_hits == []

    _run(body)


def test_eligible_holders_filters_infrastructure():
    holders = [h("0xAbc"), h("0x000000000000000000000000000000000000dEaD"), h("0xC", is_contract=True),
               h("0xBig", 25), {"address": "0xIns", "pct": 1, "tag": "insider"}]
    assert eligible_holders(Chain.base, holders, 20.0) == ["0xabc"]


def test_missing_nullable_column_is_added():
    tmp = tempfile.mkdtemp()
    eng = create_engine(f"sqlite:///{tmp}/m.db".replace("\\", "/"))
    Base.metadata.create_all(eng)
    with eng.begin() as c:
        c.execute(text("ALTER TABLE signals DROP COLUMN holders"))
        assert "holders" not in {col["name"] for col in inspect(c).get_columns("signals")}
        _add_missing_columns(c)
        assert "holders" in {col["name"] for col in inspect(c).get_columns("signals")}
    eng.dispose()
