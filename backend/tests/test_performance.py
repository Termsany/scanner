import asyncio
import tempfile
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.models import Badge, Base, Chain, MarketSnapshot, Signal, SignalOutcome, Token
from app.performance import compute_from_snapshots, compute_outcome, performance_summary, update_outcomes

T0 = 1_800_000_000
M = 15 * 60


def candles(prices_by_quarter: list[tuple[float, float, float]]) -> list[list[float]]:
    """[(high, low, close)] per 15-minute candle starting at T0."""
    return [[T0 + i * M, c, h, l, c, 0] for i, (h, l, c) in enumerate(prices_by_quarter)]


def test_compute_outcome_horizons_peak_trough():
    # 1.0 entry; rises to 2.0 high at candle 10 (~2h30), dips to 0.6 low later, closes 1.2 by 24h
    qs = [(1.05, 0.95, 1.0)] * 96
    qs[3] = (1.3, 1.0, 1.25)          # closes 1.25 at the end of hour 1
    qs[10] = (2.0, 1.4, 1.8)
    qs[23] = (1.6, 1.4, 1.5)          # hour 6 close
    qs[60] = (0.9, 0.6, 0.7)
    qs[95] = (1.3, 1.1, 1.2)          # last candle inside 24h
    out = compute_outcome(1.0, T0, candles(qs), now=T0 + 30 * 3600)
    assert out["horizon_hours"] == 24
    assert (out["r_1h"], out["r_6h"], out["r_24h"]) == (25.0, 50.0, 20.0)
    assert out["peak_pct"] == 100.0 and out["trough_pct"] == -40.0 and out["peak_minutes"] == 157


def test_compute_outcome_partial_and_empty():
    qs = [(1.1, 0.9, 1.0)] * 30
    out = compute_outcome(1.0, T0, candles(qs), now=T0 + 3 * 3600)
    assert out["horizon_hours"] == 1 and "r_6h" not in out
    assert compute_outcome(1.0, T0, [], now=T0 + 7200) == {"horizon_hours": 0}


class FakeGecko:
    def __init__(self, series):
        self.series = series
        self.calls = 0

    async def ohlcv(self, chain, pool, **kw):
        self.calls += 1
        return self.series.get(pool, [])


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


def test_update_outcomes_schedule_and_summary():
    async def body(s):
        now = datetime.now(timezone.utc)
        series = {}
        for i, (score, hours_ago, close) in enumerate([(92, 7, 1.5), (82, 7, 0.5), (77, 2, 1.1), (85, 0.5, 1.0)]):
            t = Token(chain=Chain.solana, address=f"mint{i}", symbol=f"T{i}", name="", pair_address=f"pool{i}",
                      discovery_sources=[], alpha_score=score, badge=Badge.strong_alpha)
            s.add(t)
            await s.flush()
            created = now - timedelta(hours=hours_ago)
            s.add(Signal(token_id=t.id, score=score, badge=Badge.strong_alpha, price_usd=1.0, payload={},
                         created_at=created))
            t0 = int(created.timestamp())
            series[f"pool{i}"] = [[t0 + k * 900, 1.0, max(1.0, close) * 1.1, min(1.0, close) * 0.9, close, 0]
                                  for k in range(int(hours_ago * 4) + 1)]
        await s.commit()
        gecko = FakeGecko(series)
        st = await update_outcomes(s, gecko, Settings(perf_max_calls_per_run=10))
        await s.commit()
        assert st == {"due": 3, "calls": 3, "measured": 3, "from_snapshots": 0}  # 30-min-old signal not due yet
        rows = {o.symbol: o for o in (await s.execute(select(SignalOutcome))).scalars()}
        assert rows["T0"].horizon_hours == 6 and rows["T0"].r_6h == 50.0
        assert rows["T1"].r_6h == -50.0 and rows["T2"].horizon_hours == 1
        # nothing new is due until a signal crosses its next horizon
        assert (await update_outcomes(s, gecko, Settings()))["due"] == 0
        p = await performance_summary(s, hours=24)
        assert p["measured"] == 3 and p["signals_in_window"] == 4
        by = {b["label"]: b for b in p["by_score"]}
        assert by["90+"]["median_6h"] == 50.0 and by["80–89"]["median_6h"] == -50.0 and by["75–79"]["n"] == 1
        assert p["overall"]["positive_6h_pct"] == 50.0 and p["best"][0]["symbol"] == "T0"
    _db(body)


def test_rate_limit_is_not_an_attempt_and_stops_the_run():
    class Flaky(FakeGecko):
        async def ohlcv(self, chain, pool, **kw):
            self.calls += 1
            raise RuntimeError("ohlcv unavailable")

    async def body(s):
        now = datetime.now(timezone.utc)
        for i in range(3):
            t = Token(chain=Chain.solana, address=f"m{i}", symbol=f"X{i}", name="", pair_address=f"p{i}",
                      discovery_sources=[], alpha_score=80, badge=Badge.strong_alpha)
            s.add(t)
            await s.flush()
            s.add(Signal(token_id=t.id, score=80, badge=Badge.strong_alpha, price_usd=1.0, payload={},
                         created_at=now - timedelta(hours=2)))
        await s.commit()
        g = Flaky({})
        st = await update_outcomes(s, g, Settings(perf_max_calls_per_run=10))
        await s.commit()
        assert g.calls == 1 and st["measured"] == 0           # no more candle calls after the first failure
        rows = (await s.execute(select(SignalOutcome))).scalars().all()
        failed = [o for o in rows if o.error]
        assert len(failed) == 1 and failed[0].attempts == 0 and "unavailable" in failed[0].error
    _db(body)



def test_compute_from_snapshots_and_gap_rejection():
    # one snapshot per minute for 7h: +80% peak at minute 90, 1h price 1.2, 6h price 0.9
    snaps = []
    for m in range(0, 7 * 60):
        p = 1.0 + (0.8 if m == 90 else 0.0) + (0.2 if 55 <= m <= 65 else 0) - (0.1 if 355 <= m <= 365 else 0)
        snaps.append((T0 + m * 60, p))
    out = compute_from_snapshots(1.0, T0, snaps, now=T0 + 7 * 3600)
    assert out["horizon_hours"] == 6 and out["r_1h"] == 20.0 and out["r_6h"] == -10.0
    assert out["peak_pct"] == 80.0 and out["peak_minutes"] == 90
    gappy = [x for x in snaps if not (T0 + 2 * 3600 <= x[0] <= T0 + 3 * 3600)]   # 1h hole: could hide a crash
    assert compute_from_snapshots(1.0, T0, gappy, now=T0 + 7 * 3600) is None
    assert compute_from_snapshots(1.0, T0, snaps[:10], now=T0 + 7 * 3600) is None  # does not reach 1h


def test_update_prefers_snapshots_and_spends_no_calls():
    async def body(s):
        now = datetime.now(timezone.utc)
        t = Token(chain=Chain.solana, address="mS", symbol="SNAP", name="", pair_address="pS",
                  discovery_sources=[], alpha_score=88, badge=Badge.strong_alpha)
        s.add(t)
        await s.flush()
        created = now - timedelta(hours=2)
        s.add(Signal(token_id=t.id, score=88, badge=Badge.strong_alpha, price_usd=1.0, payload={}, created_at=created))
        for m in range(0, 121):
            s.add(MarketSnapshot(token_id=t.id, price_usd=1.5 if m >= 60 else 1.0,
                                 captured_at=created + timedelta(minutes=m)))
        await s.commit()
        g = FakeGecko({})
        st = await update_outcomes(s, g, Settings())
        await s.commit()
        o = (await s.execute(select(SignalOutcome))).scalars().one()
        assert g.calls == 0 and st["from_snapshots"] == 1
        assert (o.source, o.horizon_hours, o.r_1h) == ("snapshots", 1, 50.0)
    _db(body)
