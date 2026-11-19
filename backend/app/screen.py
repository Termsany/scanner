"""Scanner check of a token before the copy bot buys it.

Same data and scoring as the scanner itself: DexScreener market data + RugCheck security, normalised and scored
by compute_alpha_score. The copied wallet now holds the token, so it counts as a tracked-wallet hit, exactly as
the scanner would score it once it saw that wallet among the holders.

A token is rejected when:
  * the scanner's hard fail fires (honeypot, rugged, cannot sell, prohibitive sell tax)
  * mint or freeze authority is still enabled
  * the top 10 holders own more than LIVE_MAX_TOP10_PCT
  * its Alpha Score is below LIVE_MIN_ALPHA_SCORE
  * the check cannot run in time (LIVE_SCREEN_FAIL_CLOSED): no data, no buy

Results are cached a couple of minutes per token so both accounts (and repeat buys) share one lookup.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from app.config import Settings, get_settings
from app.models import Chain
from app.schemas import SmartMoneyInfo

log = logging.getLogger("screen")

CACHE_SECONDS = 120.0
TIMEOUT_SECONDS = 6.0


@dataclass
class ScreenResult:
    ok: bool
    reason: str
    score: int | None = None


class TokenScreener:
    def __init__(self, settings: Settings | None = None, dex=None, rugcheck=None) -> None:
        self.s = settings or get_settings()
        self._own_http = None
        if dex is None or rugcheck is None:
            from app.scanner_worker import DexScreenerClient, Http, RugCheckClient

            self._own_http = Http(self.s)
            dex = dex or DexScreenerClient(self._own_http)
            rugcheck = rugcheck or RugCheckClient(self._own_http, self.s.rugcheck_api_key)
        self.dex, self.rugcheck = dex, rugcheck
        self._cache: dict[str, tuple[float, ScreenResult]] = {}
        self._inflight: dict[str, asyncio.Future] = {}

    async def close(self) -> None:
        if self._own_http is not None:
            await self._own_http.close()

    async def check(self, mint: str, leader_label: str = "") -> ScreenResult:
        hit = self._cache.get(mint)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
        if mint in self._inflight:                       # the other account is checking it right now
            return await asyncio.shield(self._inflight[mint])
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[mint] = fut
        try:
            try:
                res = await asyncio.wait_for(self._check(mint, leader_label), TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                res = self._unavailable("security check timed out")
            except Exception as exc:
                log.warning("screen %s failed: %r", mint[:8], exc)
                res = self._unavailable("security check failed")
            if res.reason != "unavailable-retry":
                self._cache[mint] = (time.monotonic(), res)
            fut.set_result(res)
            return res
        finally:
            self._inflight.pop(mint, None)

    def _unavailable(self, why: str) -> ScreenResult:
        if self.s.live_screen_fail_closed:
            return ScreenResult(False, f"{why} (no data, no buy)")
        return ScreenResult(True, f"{why}; buying without the check")

    async def _check(self, mint: str, leader_label: str) -> ScreenResult:
        from app.scanner_worker import normalize_solana, pair_to_metrics, select_primary_pair
        from app.scoring_engine import compute_alpha_score

        pairs, rug = await asyncio.gather(self.dex.pairs_for_tokens(Chain.solana, [mint]), self.rugcheck.report(mint))
        pair = select_primary_pair(Chain.solana, mint, pairs or [])
        if pair is None:
            return ScreenResult(False, "no DEX pair")
        if not rug:
            return self._unavailable("RugCheck has no report")
        m = pair_to_metrics(Chain.solana, mint, pair, ["copy"])
        sec = normalize_solana(mint, m.pair_address, None, rug)
        sm = SmartMoneyInfo(tracked_wallet_hits=[leader_label or "copied wallet"])
        bd = compute_alpha_score(m, sec, sm)
        score = bd.total
        if bd.hard_fail:
            return ScreenResult(False, f"scanner hard fail: {bd.hard_fail}", score)
        if sec.is_mintable is True:
            return ScreenResult(False, "mint authority enabled", score)
        if sec.is_freezable is True:
            return ScreenResult(False, "freeze authority enabled", score)
        top10 = sec.top10_holders_pct
        if top10 is not None and top10 > self.s.live_max_top10_pct:
            return ScreenResult(False, f"top 10 holders own {top10:.0f}% (max {self.s.live_max_top10_pct:.0f}%)", score)
        if self.s.live_min_alpha_score is not None and score < self.s.live_min_alpha_score:
            return ScreenResult(False, f"Alpha Score {score} < {self.s.live_min_alpha_score}", score)
        return ScreenResult(True, f"Alpha Score {score}", score)
