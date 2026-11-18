import asyncio
import tempfile
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.models import Badge, Base, Chain, Signal, SignalOutcome, Token
from app.performance import compute_outcome, performance_summary, update_outcomes

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
        assert st == {"due": 3, "calls": 3, "measured": 3}      # the 30-minute-old signal is not due yet
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
