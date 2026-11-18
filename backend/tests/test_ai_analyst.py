import json

from app.ai_analyst import AIAnalyst, rules_analysis
from app.cache import Cache
from app.config import Settings
from app.models import Chain
from app.schemas import MarketMetrics, SecurityReport
from app.scoring_engine import compute_alpha_score

M = MarketMetrics(chain=Chain.solana, address="Mint", symbol="T", liquidity_usd=100_000, volume_h1=80_000,
                  buys_h1=300, sells_h1=150, price_change_m5=2, price_change_h1=12, market_cap=1_000_000,
                  price_usd=0.001)
SEC = SecurityReport(chain=Chain.solana, address="Mint", providers=["t"], is_honeypot=False, is_mintable=False,
                     is_freezable=False, lp_locked_pct=100, top10_holders_pct=18)
SCORE = compute_alpha_score(M, SEC)
RULES = rules_analysis(SCORE, SEC)
analyst = AIAnalyst(Cache(None), Settings(llm_api_key=None, llm_model="test-model"))


def parse(obj) -> object:
    return analyst._parse(obj if isinstance(obj, str) else json.dumps(obj), RULES)


def test_fields_map_to_ordered_bullets():
    a = parse({"verdict": "bullish", "reward": "R", "risk": "K", "watch": "W", "confidence": 0.7})
    assert a.bullets == ["R", "K", "W"] and a.verdict == "BULLISH" and a.model == "test-model"


def test_missing_risk_is_filled_from_rules():
    a = parse({"verdict": "BULLISH", "reward": "R", "watch": "W", "confidence": 0.7})
    assert a.bullets[0] == "R" and a.bullets[2] == "W"
    assert a.bullets[1] == RULES.bullets[1] and a.bullets[1].startswith("Risk:")
    assert a.model == "test-model + rules"


def test_list_values_are_joined_and_fences_stripped():
    raw = '```json\n{"verdict": "NEUTRAL", "reward": ["a", "b"], "risk": "K", "watch": "W"}\n```'
    a = parse(raw)
    assert a.bullets[0] == "a; b" and a.verdict == "NEUTRAL"


def test_invalid_output_returns_none():
    assert parse("not json") is None
    assert parse({"verdict": "MOON", "reward": "R", "risk": "K", "watch": "W"}) is None
    assert parse({"verdict": "BULLISH"}) is None
    assert parse('["list"]') is None


def test_confidence_clamped():
    assert parse({"verdict": "AVOID", "reward": "R", "risk": "K", "watch": "W", "confidence": 7}).confidence == 1.0
