"""AI Token Analyst: 3-bullet risk/reward summary from a token's metrics.

Uses any OpenAI-compatible Chat Completions endpoint (OpenAI, Gemini's OpenAI endpoint, OpenRouter,
a local vLLM/Ollama server...). When no API key is configured, or the model call fails, it returns a
deterministic rule-based analysis built from the score breakdown, labelled `model="rules-v1"`, so the
inspector drawer always has something truthful to show.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from pydantic import ValidationError

from app.cache import Cache
from app.config import Settings, get_settings
from app.models import Badge
from app.schemas import AIAnalysis, MarketMetrics, ScoreBreakdown, ScoreComponent, SecurityReport

log = logging.getLogger(__name__)

RULES_MODEL = "rules-v1"
VERDICTS = {"BULLISH", "NEUTRAL", "AVOID"}

SYSTEM_PROMPT = """You are a senior on-chain analyst reviewing a freshly scanned DEX token.
You receive JSON with market metrics, a security audit and a deterministic "Alpha Score" breakdown.

Respond with ONLY a JSON object, no prose, in exactly this shape:
{"verdict": "BULLISH" | "NEUTRAL" | "AVOID",
 "reward": string,
 "risk": string,
 "watch": string,
 "confidence": number}

Field rules (each field at most 28 words, concrete and numeric: cite the figures you rely on):
- "reward": the single strongest reason the token could perform.
- "risk": the single most serious DOWNSIDE. It must describe something that can go wrong (weakness,
  failed check, missing data, concentration, thin liquidity, overheated momentum, young pair age, etc.).
  It must NOT restate a strength. Every token has a risk; new DEX tokens are inherently volatile.
- "watch": the concrete level or event that would invalidate the setup.

Other rules:
- If "hard_fail" is not null, or the token is a honeypot, verdict MUST be "AVOID".
- Missing data (listed in "data_gaps") is a risk, never a positive.
- confidence is 0-1 and must be <= 0.5 when more than two data_gaps exist.
- Do not give position sizes, price targets or financial advice. Do not invent data not in the input."""


def build_payload(m: MarketMetrics, sec: SecurityReport | None, score: ScoreBreakdown | None) -> dict[str, Any]:
    """Compact, model-friendly view (keeps prompt small and cache keys stable)."""

    def comps(cat) -> list[dict[str, Any]]:
        return [{"check": c.label, "points": c.points, "max": c.max_points, "detail": c.detail} for c in cat.components]

    payload: dict[str, Any] = {
        "token": {"chain": m.chain.value, "symbol": m.symbol, "name": m.name, "dex": m.dex_id,
                  "age_minutes": round(m.age_minutes) if m.age_minutes is not None else None},
        "market": {
            "price_usd": m.price_usd, "liquidity_usd": m.liquidity_usd, "market_cap_usd": m.effective_mcap,
            "volume_usd": {"m5": m.volume_m5, "h1": m.volume_h1, "h24": m.volume_h24},
            "price_change_pct": {"m5": m.price_change_m5, "h1": m.price_change_h1, "h24": m.price_change_h24},
            "txns_h1": {"buys": m.buys_h1, "sells": m.sells_h1},
            "txns_h24": {"buys": m.buys_h24, "sells": m.sells_h24},
        },
    }
    if sec is not None:
        payload["security"] = {
            "providers": sec.providers, "honeypot": sec.is_honeypot, "mintable": sec.is_mintable,
            "freezable": sec.is_freezable, "buy_tax_pct": sec.buy_tax, "sell_tax_pct": sec.sell_tax,
            "lp_locked_pct": sec.lp_locked_pct, "top10_holders_pct": sec.top10_holders_pct,
            "holder_count": sec.holder_count,
            "risks": [{"name": r.name, "level": r.level} for r in sec.risks[:8]],
        }
    if score is not None:
        payload["alpha_score"] = {
            "total": score.total, "badge": score.badge.value, "hard_fail": score.hard_fail,
            "security": {"points": score.security.points, "max": score.security.max_points,
                         "checks": comps(score.security)},
            "momentum": {"points": score.momentum.points, "max": score.momentum.max_points,
                         "checks": comps(score.momentum)},
            "smart_money": {"points": score.smart_money.points, "max": score.smart_money.max_points,
                            "checks": comps(score.smart_money)},
            "data_gaps": score.data_gaps,
        }
    return payload


def rules_analysis(score: ScoreBreakdown | None, sec: SecurityReport | None) -> AIAnalysis:
    """Deterministic fallback derived only from the score breakdown."""
    if score is None:
        return AIAnalysis(verdict="NEUTRAL", confidence=0.2, model=RULES_MODEL,
                          bullets=["Token has not been scored yet; no basis for a view.",
                                   "Security audit and momentum data are unavailable.",
                                   "Wait for the next scan cycle before acting."])

    all_comps: list[ScoreComponent] = [*score.security.components, *score.momentum.components,
                                       *score.smart_money.components]
    strengths = sorted((c for c in all_comps if c.points > 0), key=lambda c: c.points, reverse=True)
    weaknesses = sorted((c for c in all_comps if c.points < c.max_points),
                        key=lambda c: c.max_points - c.points, reverse=True)

    if score.hard_fail:
        verdict = "AVOID"
    elif score.badge == Badge.strong_alpha:
        verdict = "BULLISH"
    elif score.badge == Badge.moderate:
        verdict = "NEUTRAL"
    else:
        verdict = "AVOID"

    if score.hard_fail:
        b1 = f"Hard fail: {score.hard_fail.replace('_', ' ').lower()} - score forced to 0 regardless of momentum."
    elif strengths:
        b1 = f"Strength: {strengths[0].detail or strengths[0].label} (+{strengths[0].points:g}/{strengths[0].max_points:g})."
    else:
        b1 = "No scoring component passed; there is no measurable edge."

    danger = [r.name for r in (sec.risks if sec else []) if r.level == "danger"]
    if danger:
        b2 = f"Risk: {', '.join(danger[:3])} flagged by the security audit."
    elif weaknesses:
        w = weaknesses[0]
        b2 = f"Risk: {w.detail or w.label} ({w.points:g}/{w.max_points:g})."
    else:
        b2 = "Risk: all checks passed, but new DEX tokens remain highly volatile."

    if score.data_gaps:
        b3 = f"Watch: missing data ({', '.join(g.split('.')[-1] for g in score.data_gaps[:3])}); re-check after next scan."
    else:
        mom = score.momentum
        b3 = f"Watch: momentum {mom.points:g}/{mom.max_points:g}; a drop below 60 overall invalidates the setup."

    confidence = 0.35 if len(score.data_gaps) > 2 else round(min(0.9, 0.4 + score.total / 200), 2)
    return AIAnalysis(verdict=verdict, bullets=[b1, b2, b3], confidence=confidence, model=RULES_MODEL)


class AIAnalyst:
    def __init__(self, cache: Cache, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.cache = cache
        self._client = None
        if self.settings.llm_api_key:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                api_key=self.settings.llm_api_key,
                base_url=self.settings.llm_base_url,
                timeout=self.settings.llm_timeout_seconds,
                max_retries=1,
            )

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def analyze(
        self, metrics: MarketMetrics, security: SecurityReport | None, score: ScoreBreakdown | None
    ) -> AIAnalysis:
        payload = build_payload(metrics, security, score)
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:24]
        key = f"ai:v2:{metrics.chain.value}:{metrics.address}:{digest}"

        cached = await self.cache.get_json(key)
        if cached:
            try:
                return AIAnalysis.model_validate(cached)
            except ValidationError:
                pass

        rules = rules_analysis(score, security)
        result = None
        if self._client:
            content = await self._call_llm(payload)
            if content is not None:
                result = self._parse(content, rules)
        if result is None:
            result = rules
        elif score is not None and score.hard_fail:
            result.verdict = "AVOID"  # never let the model override a hard fail

        await self.cache.set_json(key, result.model_dump(mode="json"), self.settings.ai_cache_ttl_seconds)
        return result

    async def _call_llm(self, payload: dict[str, Any]) -> str | None:
        assert self._client is not None
        try:
            resp = await self._client.chat.completions.create(
                model=self.settings.llm_model,
                temperature=0.2,
                max_tokens=self.settings.llm_max_tokens,
                response_format={"type": "json_object"},
                extra_body=self.settings.llm_extra_body or None,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(payload, default=str)},
                ],
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception:
            log.warning("LLM analysis failed; using rules fallback", exc_info=True)
            return None

    def _parse(self, content: str, rules: AIAnalysis) -> AIAnalysis | None:
        """Parse {"verdict","reward","risk","watch","confidence"} into ordered bullets [reward, risk, watch].

        A slot the model leaves empty is filled from the deterministic rules analysis, so the dashboard's
        Reward / Risk / Watch labels always sit next to the right kind of statement."""
        if content.startswith("```"):  # some providers wrap JSON in fences despite json mode
            content = content.strip("`")
            content = content[content.find("{"):]
        try:
            data = json.loads(content[content.find("{"): content.rfind("}") + 1])
        except (ValueError, TypeError):
            log.warning("LLM returned non-JSON: %.200s", content)
            return None
        if not isinstance(data, dict):
            return None
        verdict = str(data.get("verdict", "")).strip().upper()
        slots = [_text(data.get(k)) for k in SLOTS]
        if verdict not in VERDICTS or not any(slots):
            log.warning("LLM output failed validation: %.200s", content)
            return None
        filled = [s or rules.bullets[i] for i, s in enumerate(slots)]
        missing = [SLOTS[i] for i, s in enumerate(slots) if not s]
        if missing:
            log.info("LLM omitted %s; filled from rules", ", ".join(missing))
        try:
            confidence = float(data.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        model = self.settings.llm_model + (" + rules" if missing else "")
        return AIAnalysis(verdict=verdict, bullets=filled, confidence=min(max(confidence, 0.0), 1.0), model=model)


SLOTS = ("reward", "risk", "watch")


def _text(v: Any) -> str:
    """Field as a clean single string; lists (some models return one) are joined."""
    if isinstance(v, list):
        v = "; ".join(str(x).strip() for x in v if str(x).strip())
    return str(v).strip() if v is not None else ""
