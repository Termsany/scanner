import asyncio
import json
import tempfile

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.models import Base, PushSubscription


def _db(body):
    async def go():
        tmp = tempfile.mkdtemp()
        eng = create_async_engine(f"sqlite+aiosqlite:///{tmp}/push.db".replace("\\", "/"))
        async with eng.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        try:
            async with async_sessionmaker(eng, expire_on_commit=False)() as s:
                return await body(s)
        finally:
            await eng.dispose()
    return asyncio.run(go())


def test_generated_keys_work_with_vapid():
    from app.push import _vapid, generate_keys

    pub, priv = generate_keys()
    assert len(pub) == 87 and pub.startswith("B")       # uncompressed P-256 point, base64url
    assert _vapid(priv) is not None


def test_send_all_delivers_and_forgets_dead_endpoints(monkeypatch):
    import app.push as push

    class Gone(Exception):
        class response:
            status_code = 410

    sent = []

    def fake_webpush(info, payload, vapid, subject):
        if info["endpoint"].endswith("dead"):
            raise Gone("unsubscribed")
        sent.append((info["endpoint"], json.loads(payload)))

    monkeypatch.setattr(push, "_webpush", fake_webpush)
    pub, priv = push.generate_keys()
    s = Settings(live_vapid_public_key=pub, live_vapid_private_key=priv)

    async def body(sess):
        sess.add_all([PushSubscription(endpoint="https://push.test/ok", p256dh="p", auth="a"),
                      PushSubscription(endpoint="https://push.test/dead", p256dh="p", auth="a")])
        await sess.commit()
        import app.db as dbmod
        monkeypatch.setattr(dbmod, "SessionLocal", async_sessionmaker(sess.bind, expire_on_commit=False))
        n = await push.send_all(s, "opened $XYZ", "$20 in")
        left = (await sess.execute(select(PushSubscription.endpoint))).scalars().all()
        return n, left

    n, left = _db(body)
    assert n == 1 and sent[0][1]["title"] == "opened $XYZ"
    assert left == ["https://push.test/ok"]                 # the 410 subscription was removed


def test_push_disabled_without_keys():
    from app.push import send_all

    assert asyncio.run(send_all(Settings(), "t", "b")) == 0


def test_subscribe_endpoint_needs_password_and_stores_once(monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import live as live_api
    from app.config import get_settings
    from app.main import app

    s = get_settings()
    monkeypatch.setattr(s, "live_dashboard_password", "pw-push")
    live_api._fails.clear()
    body = {"endpoint": "https://fcm.test/abc", "keys": {"p256dh": "P", "auth": "A"}}
    with TestClient(app) as c:
        assert c.post("/api/live/push/subscribe", json=body).status_code == 401
        h = {"X-Live-Password": "pw-push"}
        assert c.post("/api/live/push/subscribe", json=body, headers=h).json() == {"subscribed": True}
        assert c.post("/api/live/push/subscribe", json=body, headers=h).status_code == 200   # idempotent
        assert c.post("/api/live/push/subscribe", json={**body, "endpoint": "http://x"}, headers=h).status_code == 400
        assert c.get("/api/live/push/key").status_code == 200
    live_api._fails.clear()


def test_engine_sends_label_in_telegram_and_push_on_open():
    import tests.test_live as tl
    from app.live import CopyEngine
    from app.models import Chain

    msgs, pushes = [], []

    async def tg(text):
        msgs.append(text)

    async def pu(title, body):
        pushes.append((title, body))

    x = tl.FakeExec()

    async def body(sess):
        eng = CopyEngine(x, tl._s(), notify=tg, push=pu)
        await eng.step(sess)
        t = tl._trade(mint="PUSHME")
        t.wallet_label = "DULX"
        sess.add(t)
        await sess.commit()
        await eng.step(sess)

    tl._db(body)
    assert any("Position opened" in m and "From: DULX" in m for m in msgs)
    assert len(pushes) == 1 and "DULX" in pushes[0][0] and "opened" in pushes[0][0]
