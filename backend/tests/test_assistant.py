"""Free questions on Telegram (app/assistant.py): the tool loop, read-only tools on the bot's data, limits."""

import asyncio
from types import SimpleNamespace

from app.assistant import Assistant
from app.config import Settings
from app.models import LivePosition
from tests.test_live import LEADER, _db


class FakeClient:
    """Answers like Claude: first asks for a tool, then replies with text that uses the tool's result."""

    def __init__(self, tool="get_trades", args=None):
        self.calls = []
        self.tool, self.args = tool, args or {"hours": 24, "account": ""}
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    async def create(self, **kw):
        self.calls.append(kw)
        msgs = kw["messages"]
        last = msgs[-1]["content"]
        if isinstance(last, list) and last and last[0].get("type") == "tool_result":
            self.result = last[0]["content"]
            return SimpleNamespace(stop_reason="end_turn",
                                   content=[SimpleNamespace(type="text", text="كسبت $12.50 النهارده")])
        return SimpleNamespace(stop_reason="tool_use",
                               content=[SimpleNamespace(type="tool_use", id="t1", name=self.tool, input=self.args)])


def _assistant(session_factory, client, **kw):
    s = Settings(live_mode="live", assistant_daily_limit=kw.pop("limit", 200), **kw)
    return Assistant(s, session_factory=session_factory, client=client)


def test_a_question_runs_a_tool_and_answers_from_its_data():
    from datetime import datetime, timezone

    client = FakeClient()

    async def body(sess):
        sess.add(LivePosition(mode="live", account="main", token_address="MINTpump", symbol="WIN", leader_wallet=LEADER,
                              leader_trade_id=1, status="closed", cost_usd=30.0, pnl_usd=12.5, exit_reason="leader_sold",
                              tokens_raw="0", closed_at=datetime.now(timezone.utc)))
        await sess.commit()
        factory = lambda: _Ctx(sess)
        a = _assistant(factory, client)
        reply = await a.ask(1, "كسبت كام النهارده؟")
        again = await a.ask(1, "وامبارح؟")             # same chat: the history goes along
        return reply, again

    reply, _ = _db(body)
    assert reply == "كسبت $12.50 النهارده"
    assert '"symbol": "WIN"' in client.result and '"pnl_usd": 12.5' in client.result
    first = client.calls[0]
    assert first["model"] == "claude-opus-5-5" and first["fallbacks"] == "default"
    assert {t["name"] for t in first["tools"]} >= {"get_accounts", "get_trades", "lookup_coin"}
    conv = client.calls[2]["messages"]                      # one list, only appended to: q1, call, result, answer, q2...
    assert conv[0]["content"] == "كسبت كام النهارده؟" and conv[4]["content"] == "وامبارح؟" and len(conv) == 8


def test_daily_limit_and_no_key():
    async def go():
        a = _assistant(None, FakeClient(), limit=1)
        a.Session = None
        first = a._limit_ok()
        second = await a.ask(1, "سؤال")
        off = Assistant(Settings(anthropic_api_key=None), session_factory=lambda: None)
        return first, second, await off.ask(1, "سؤال"), off.enabled

    first, second, off, enabled = asyncio.run(go())
    assert first and "حد الأسئلة" in second and "ANTHROPIC_API_KEY" in off and not enabled


class _Ctx:
    def __init__(self, sess):
        self.sess = sess

    async def __aenter__(self):
        return self.sess

    async def __aexit__(self, *a):
        return False
