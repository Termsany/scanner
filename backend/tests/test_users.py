"""Registered users: sign-up, approval, their own page, and that smart wallets never leak to them."""

import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_tmp}/users.db".replace("\\", "/"))
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("LLM_API_KEY", "")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("SMART_ACTIVITY_CHECK_ENABLED", "false")

import pytest  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402

LEADER = "BCyNgSB1njYWrjHubBc56UXqDqzc4ixk8UHPhGbTQ5aQ"
ADMIN = {"X-Live-Password": "admin-pass-123"}


@pytest.fixture()
def env(monkeypatch):
    from app import users
    from app.api import live as live_api

    s = get_settings()
    monkeypatch.setattr(s, "live_dashboard_password", "admin-pass-123")
    monkeypatch.setattr(s, "auth_cookie_secure", False)
    monkeypatch.setattr(s, "live_key_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(s, "users_enabled", True)
    monkeypatch.setattr(s, "users_registration_open", True)
    users._fails.clear()
    live_api._fails.clear()
    yield s
    users._fails.clear()


def _register(c, email, pw="a-long-password"):
    return c.post("/api/me/register", json={"email": email, "password": pw, "name": "Test"})


def test_password_hashing():
    from app.users import check_password, hash_password

    h = hash_password("correct horse battery")
    assert "correct" not in h and check_password("correct horse battery", h)
    assert not check_password("wrong", h) and not check_password("x", "garbage")


def test_register_login_logout_and_rules(env):
    with TestClient(app) as c:
        assert _register(c, "bad-email").status_code == 400
        assert _register(c, "a@x.io", "short").status_code == 400
        r = _register(c, "Alice@Example.com")
        assert r.status_code == 200 and r.json()["status"] == "pending"
        me = c.get("/api/me").json()
        assert me["user"]["email"] == "alice@example.com" and me["account"] is None    # waits for approval
        assert _register(c, "alice@example.com").status_code == 400                    # taken
        c.post("/api/me/logout")
        assert c.get("/api/me").status_code == 401
        assert c.post("/api/me/login", json={"email": "alice@example.com", "password": "nope-nope-nope"}
                      ).status_code == 401
        assert c.post("/api/me/login", json={"email": "ALICE@example.com", "password": "a-long-password"}
                      ).status_code == 200
        assert c.get("/api/me").status_code == 200
        assert c.patch("/api/me/account", json={"size_usd": 5}).status_code == 403     # not approved yet


def test_login_lockout(env):
    with TestClient(app) as c:
        _register(c, "bob@example.com")
        c.post("/api/me/logout")
        for _ in range(8):
            c.post("/api/me/login", json={"email": "bob@example.com", "password": "wrong-password"})
        r = c.post("/api/me/login", json={"email": "bob@example.com", "password": "a-long-password"})
        assert r.status_code == 429


def test_approval_gives_account_and_smart_wallets_never_leak(env):
    with TestClient(app) as c:
        _register(c, "carol@example.com")
        uid = [u for u in c.get("/api/live/users", headers=ADMIN).json()["users"]
               if u["email"] == "carol@example.com"][0]["id"]
        assert c.get("/api/live/users").status_code == 401                               # admin only
        r = c.post(f"/api/live/users/{uid}/approve", headers=ADMIN, json={
            "leaders": [{"wallet": LEADER, "unconditional": True}], "size_usd": 10, "max_open_usd": 100,
            "max_daily_loss_usd": 40, "cap_size_usd": 20, "cap_open_usd": 200})
        assert r.status_code == 200, r.text
        acct = r.json()["account"]
        assert acct["name"] == f"u{uid}" and acct["pubkey"] and acct["key_revealed"] is False

        me = c.get("/api/me")
        assert me.status_code == 200
        body = me.text
        assert LEADER not in body and "leader" not in body.lower() and "unconditional" not in body
        a = me.json()["account"]
        assert a["pubkey"] == acct["pubkey"] and a["settings"]["caps"]["size_usd"] == 20

        # settings within the admin caps
        assert c.patch("/api/me/account", json={"size_usd": 25}).status_code == 400
        r = c.patch("/api/me/account", json={"size_usd": 15, "max_open_usd": 150, "paused": True})
        assert r.status_code == 200 and r.json()["settings"]["size_usd"] == 15 and r.json()["settings"]["paused"]
        assert LEADER not in r.text

        # the key: password needed, shown once
        assert c.post("/api/me/account/reveal-key", json={"password": "wrong-password"}).status_code == 401
        r = c.post("/api/me/account/reveal-key", json={"password": "a-long-password"})
        assert r.status_code == 200 and r.json()["pubkey"] == acct["pubkey"]
        from app.live import load_keypair

        assert str(load_keypair(r.json()["secret"]).pubkey()) == acct["pubkey"]
        assert c.post("/api/me/account/reveal-key", json={"password": "a-long-password"}).status_code == 409

        # withdrawals need live mode and a valid address
        assert c.post("/api/me/account/withdraw", json={"password": "a-long-password", "to": "nope"}
                      ).status_code == 400

        # the admin's audit log saw it all
        actions = [e["action"] for e in c.get("/api/live/audit", headers=ADMIN).json()["entries"]]
        assert {"register", "user_approved", "settings", "key_revealed"} <= set(actions)
        assert "secret" not in str(c.get("/api/live/audit", headers=ADMIN).json())


def test_block_signs_out_and_pauses(env):
    with TestClient(app) as c:
        _register(c, "dave@example.com")
        uid = [u for u in c.get("/api/live/users", headers=ADMIN).json()["users"]
               if u["email"] == "dave@example.com"][0]["id"]
        c.post(f"/api/live/users/{uid}/approve", headers=ADMIN, json={})
        assert c.get("/api/me").status_code == 200
        assert c.post(f"/api/live/users/{uid}/block", headers=ADMIN, json={"blocked": True}).status_code == 200
        assert c.get("/api/me").status_code == 401                                       # session ended
        assert c.post("/api/me/login", json={"email": "dave@example.com", "password": "a-long-password"}
                      ).status_code == 403
        accts = c.get("/api/live/accounts", headers=ADMIN).json()["accounts"]
        assert [a for a in accts if a["user_id"] == uid][0]["paused"] is True


def test_user_cannot_sell_someone_elses_position(env):
    import asyncio

    from app.db import SessionLocal
    from app.models import LivePosition

    with TestClient(app) as c:
        _register(c, "erin@example.com")
        uid = [u for u in c.get("/api/live/users", headers=ADMIN).json()["users"]
               if u["email"] == "erin@example.com"][0]["id"]
        c.post(f"/api/live/users/{uid}/approve", headers=ADMIN, json={})

        async def add():
            async with SessionLocal() as s:
                p = LivePosition(mode="live", account="main", token_address="M", symbol="M", leader_wallet=LEADER,
                                 leader_trade_id=1, status="open", cost_usd=10)
                s.add(p)
                await s.commit()
                return p.id
        pid = c.portal.call(add) if hasattr(c, "portal") and c.portal else asyncio.run(add())
        assert c.post(f"/api/me/positions/{pid}/sell", json={"share": 1.0}).status_code == 404
