"""POST /api/ai-analyze"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_analyst import AIAnalyst
from app.config import get_settings
from app.db import get_session
from app.repository import get_token, token_detail
from app.schemas import AIAnalysis, AIAnalyzeRequest
from app.scoring_engine import compute_alpha_score

router = APIRouter(prefix="/api", tags=["ai"])
Session = Annotated[AsyncSession, Depends(get_session)]


class _PerClientLimiter:
    """Sliding 60s window per client IP. In-process; put a gateway limit in front for multi-replica deploys."""

    def __init__(self) -> None:
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, client: str, per_minute: int) -> bool:
        now = time.monotonic()
        q = self.hits[client]
        while q and now - q[0] > 60.0:
            q.popleft()
        if len(q) >= per_minute:
            return False
        q.append(now)
        if len(self.hits) > 10_000:
            for k in [k for k, v in self.hits.items() if not v]:
                del self.hits[k]
        return True


_limiter = _PerClientLimiter()


@router.post("/ai-analyze", response_model=AIAnalysis)
async def ai_analyze(body: AIAnalyzeRequest, request: Request, session: Session) -> AIAnalysis:
    """3-bullet risk/reward analysis. Metrics may be supplied inline; otherwise the latest scan is used.
    Without LLM_API_KEY the deterministic `rules-v1` analysis is returned."""
    client_ip = request.client.host if request.client else "unknown"
    if not _limiter.allow(client_ip, get_settings().ai_requests_per_minute):
        raise HTTPException(status_code=429, detail="AI analysis rate limit exceeded; try again in a minute")

    metrics, security, score = body.metrics, body.security, body.score
    if metrics is None:
        token = await get_token(session, body.chain, body.address)
        if token is None:
            raise HTTPException(status_code=404, detail="Token not found; pass `metrics` to analyse an unscanned token")
        detail = await token_detail(session, token, history_points=1)
        metrics = detail.metrics
        security = security or detail.security
        score = score or detail.score
    if metrics.chain != body.chain:
        raise HTTPException(status_code=422, detail="metrics.chain does not match chain")
    if score is None:
        score = compute_alpha_score(metrics, security)

    analyst: AIAnalyst = request.app.state.analyst
    return await analyst.analyze(metrics, security, score)
