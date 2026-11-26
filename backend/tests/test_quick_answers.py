"""Fixed questions on Telegram, no AI (app/quick_answers.py)."""

from datetime import datetime, timedelta, timezone

from app.assistant import Assistant
from app.config import Settings
from app.models import LivePosition, WalletTrade, Chain
from app.quick_answers import HELP, norm, window
from tests.test_assistant import _Ctx
from tests.test_live import LEADER, _db


def test_spelling_and_time_windows():
    assert norm("كسبت كام النهاردة؟") == "كسبت كام النهارده"
    assert window(norm("خسرت كام امبارح"))[0] == "امبارح"
    assert window(norm("مكسب الأسبوع"))[0] == "آخر 7 أيام"
    assert window(norm("كسبت كام اخر 6 ساعات"))[0] == "آخر 6 ساعة"


def test_questions_get_answers_from_the_data():
    now = datetime.now(timezone.utc)

    async def body(sess):
        sess.add_all([
            LivePosition(mode="live", account="main", token_address="WINpump", symbol="WIN", leader_wallet=LEADER,
                         leader_trade_id=1, status="closed", cost_usd=30.0, pnl_usd=12.5, exit_reason="leader_sold",
                         tokens_raw="0", opened_at=now - timedelta(hours=3), closed_at=now - timedelta(hours=2)),
            LivePosition(mode="live", account="main", token_address="OLDpump", symbol="OLD", leader_wallet=LEADER,
                         leader_trade_id=2, status="closed", cost_usd=30.0, pnl_usd=-9.0, exit_reason="stop_loss",
                         tokens_raw="0", opened_at=now - timedelta(hours=40), closed_at=now - timedelta(hours=30)),
            WalletTrade(chain=Chain.solana, wallet=LEADER, tx_hash="t1", token_address="WINpump", symbol="WIN",
                        side="buy", usd_value=250.0, block_time=now - timedelta(hours=3)),
        ])
        await sess.commit()
        a = Assistant(Settings(live_mode="live"), session_factory=lambda: _Ctx(sess))
        a.tool_prices = None
        out = {}
        for q in ("كسبت كام النهارده؟", "خسرت كام امبارح", "اخر صفقات", "WIN عاملة ايه؟", "قواعد المحافظ",
                  "ايه اخبار الطقس"):
            out[q] = await a.ask(1, q)
        return out

    out = _db(body)
    assert "+$12.50" in out["كسبت كام النهارده؟"] and "1 صفقة" in out["كسبت كام النهارده؟"]
    assert "-$9.00" in out["خسرت كام امبارح"]
    assert "WIN" in out["اخر صفقات"] and "OLD" in out["اخر صفقات"]
    assert out["WIN عاملة ايه؟"].startswith("WIN:") and "اشترت" in out["WIN عاملة ايه؟"]
    assert out["قواعد المحافظ"]                      # no wallets in this test account: still an answer
    assert out["ايه اخبار الطقس"] == HELP
