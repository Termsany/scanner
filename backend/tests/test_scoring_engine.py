from app.models import Badge, Chain
from app.schemas import MarketMetrics, RiskItem, SecurityReport, SmartMoneyInfo
from app.scoring_engine import compute_alpha_score


def metrics(**kw) -> MarketMetrics:
    base = dict(chain=Chain.solana, address="Mint111", symbol="TST", liquidity_usd=100_000, volume_h1=80_000,
                buys_h1=300, sells_h1=150, price_change_m5=2.0, price_change_h1=12.0, market_cap=1_000_000,
                price_usd=0.001)
    base.update(kw)
    return MarketMetrics(**base)


def safe_sec(**kw) -> SecurityReport:
    base = dict(chain=Chain.solana, address="Mint111", providers=["rugcheck"], is_honeypot=False,
                is_mintable=False, is_freezable=False, lp_locked_pct=100, top10_holders_pct=18)
    base.update(kw)
    return SecurityReport(**base)


def test_perfect_token_scores_100():
    s = compute_alpha_score(metrics(), safe_sec(), SmartMoneyInfo(tracked_wallet_hits=["Fund A"]))
    assert s.total == 100 and s.badge == Badge.strong_alpha and not s.data_gaps


def test_honeypot_forces_zero():
    s = compute_alpha_score(metrics(), safe_sec(is_honeypot=True), SmartMoneyInfo(tracked_wallet_hits=["A"]))
    assert s.total == 0 and s.hard_fail == "HONEYPOT" and s.badge == Badge.high_risk


def test_rugged_and_sell_tax_hard_fail():
    assert compute_alpha_score(metrics(), safe_sec(risks=[RiskItem(name="Rugged", level="danger")])).total == 0
    assert compute_alpha_score(metrics(), safe_sec(sell_tax=60)).hard_fail == "PROHIBITIVE_SELL_TAX"


def test_missing_security_earns_nothing_and_reports_gaps():
    s = compute_alpha_score(metrics(), None)
    assert s.security.points == 0 and "security_report" in s.data_gaps


def test_mint_authority_fails_contract_block():
    s = compute_alpha_score(metrics(), safe_sec(is_mintable=True))
    assert s.security.components[0].points == 0 and s.security.points == 20


def test_zero_sells_gets_no_buy_pressure_points():
    s = compute_alpha_score(metrics(buys_h1=200, sells_h1=0), safe_sec())
    assert s.momentum.components[1].points == 0


def test_vol_liq_bands():
    pts = lambda v: compute_alpha_score(metrics(volume_h1=v), safe_sec()).momentum.components[0].points
    assert pts(1_000) == 0                 # 0.01x
    assert pts(15_000) == 7.5              # 0.15x -> halfway up ramp
    assert pts(100_000) == 15              # 1x optimal
    assert pts(400_000) == 7.5             # 4x -> halfway down decay
    assert pts(900_000) == 0               # 9x wash trading


def test_pump_and_dump_divergence():
    s = compute_alpha_score(metrics(price_change_h1=120, price_change_m5=-18), safe_sec())
    assert s.momentum.components[2].points == 0
    s = compute_alpha_score(metrics(price_change_h1=10, price_change_m5=-2), safe_sec())
    assert s.momentum.components[2].points == 5


def test_mcap_liq_band_and_fdv_fallback():
    s = compute_alpha_score(metrics(market_cap=None, fdv=2_000_000), safe_sec())
    assert s.smart_money.components[1].points == 15
    s = compute_alpha_score(metrics(market_cap=10_000_000), safe_sec())  # 100x
    assert s.smart_money.components[1].points == 0


def test_badges():
    # no smart wallets (heuristic: avg trade 80k/450 = $178 -> 0) -> 85 -> STRONG
    assert compute_alpha_score(metrics(), safe_sec()).total == 85
    s = compute_alpha_score(metrics(), safe_sec(lp_locked_pct=10, top10_holders_pct=50))
    assert s.total == 65 and s.badge == Badge.moderate
