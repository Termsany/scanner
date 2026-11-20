"""Admin email login: set up once from the shared password, then the shared password stops working."""

import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_tmp}/admin.db".replace("\\", "/"))
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("LLM_API_KEY", "")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("SMART_ACTIVITY_CHECK_ENABLED", "false")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402

OLD = {"X-Live-Password": "old-shared-pass-1"}


def test_admin_email_login_replaces_the_shared_password(monkeypatch):
    from app import users
    from app.api import live as live_api

    s = get_settings()
    monkeypatch.setattr(s, "live_dashboard_password", "old-shared-pass-1")
    monkeypatch.setattr(s, "auth_cookie_secure", False)
    monkeypatch.setattr(s, "users_enabled", True)
    monkeypatch.setattr(s, "users_registration_open", True)
    users._fails.clear()
    live_api._fails.clear()
    with TestClient(app) as c:
        # before setup: the shared password works, nothing else does
        assert c.get("/api/live/admin/status").json() == {"has_admin": False}
        assert c.get("/api/live/overview").status_code == 401
        assert c.get("/api/live/users", headers=OLD).status_code == 200

        # a user who signed up cannot become the admin by email, and a user session is not admin
        c.post("/api/me/register", json={"email": "user@example.com", "password": "a-long-password"})
        assert c.get("/api/live/users").status_code == 401
        c.post("/api/me/logout")

        bad = {"current_password": "wrong", "email": "boss@example.com", "password": "a-new-long-pass"}
        assert c.post("/api/live/admin/setup", json=bad).status_code == 401
        same = {"current_password": "old-shared-pass-1", "email": "boss@example.com", "password": "old-shared-pass-1"}
        assert c.post("/api/live/admin/setup", json=same).status_code == 400
        taken = {"current_password": "old-shared-pass-1", "email": "user@example.com", "password": "a-new-long-pass"}
        assert c.post("/api/live/admin/setup", json=taken).status_code == 400

        ok = {"current_password": "old-shared-pass-1", "email": "Boss@Example.com", "password": "a-new-long-pass"}
        r = c.post("/api/live/admin/setup", json=ok)
        assert r.status_code == 200 and r.json()["email"] == "boss@example.com"
        assert c.get("/api/live/admin/status").json() == {"has_admin": True}
        assert c.get("/api/live/admin/me").json() == {"email": "boss@example.com", "via": "session", "totp_enabled": False}
        assert c.get("/api/live/overview").status_code == 200                 # signed in by the cookie
        users_list = c.get("/api/live/users").json()["users"]
        assert all(u["email"] != "boss@example.com" for u in users_list)      # the admin is not a user
        assert c.post("/api/live/admin/setup", json=ok).status_code == 409    # only once

        # sign out: the shared password no longer opens anything
        c.post("/api/me/logout")
        assert c.get("/api/live/users", headers=OLD).status_code == 401
        assert c.get("/api/live/users").status_code == 401

        # sign in with email; change the password; old one stops working
        r = c.post("/api/me/login", json={"email": "boss@example.com", "password": "a-new-long-pass"})
        assert r.status_code == 200 and r.json()["role"] == "admin"
        assert c.get("/api/live/accounts").status_code == 200
        r = c.post("/api/me/password", json={"current": "a-new-long-pass", "new": "another-long-pass"})
        assert r.status_code == 200, r.text
        c.post("/api/me/logout")
        assert c.post("/api/me/login", json={"email": "boss@example.com", "password": "a-new-long-pass"}).status_code == 401
        assert c.post("/api/me/login", json={"email": "boss@example.com", "password": "another-long-pass"}).status_code == 200
        assert c.get("/api/live/admin/me").json()["email"] == "boss@example.com"

        # the test modules share one database: remove the admin so later tests use the shared password again
        async def drop():
            from sqlalchemy import delete

            from app.db import SessionLocal
            from app.models import User

            async with SessionLocal() as ses:
                await ses.execute(delete(User).where(User.role == "admin"))
                await ses.commit()
        c.portal.call(drop)
        assert c.get("/api/live/admin/status").json() == {"has_admin": False}
    users._fails.clear()
    live_api._fails.clear()


def test_admin_two_step_code_and_passkey_endpoints(monkeypatch):
    import time

    from cryptography.fernet import Fernet

    from app import users
    from app.api import live as live_api
    from app.users import _totp

    s = get_settings()
    monkeypatch.setattr(s, "live_dashboard_password", "old-shared-pass-1")
    monkeypatch.setattr(s, "auth_cookie_secure", False)
    monkeypatch.setattr(s, "users_enabled", True)
    monkeypatch.setattr(s, "live_key_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(s, "site_url", "http://localhost:3000")
    users._fails.clear()
    live_api._fails.clear()
    with TestClient(app) as c:
        r = c.post("/api/live/admin/setup", json={"current_password": "old-shared-pass-1",
                                                  "email": "two@example.com", "password": "a-new-long-pass"})
        assert r.status_code == 200
        secret = c.post("/api/me/2fa/setup", json={"password": "a-new-long-pass"}).json()["secret"]
        step = int(time.time()) // 30
        assert c.post("/api/me/2fa/enable", json={"code": _totp(secret, step)}).status_code == 200
        assert c.get("/api/live/admin/me").json()["totp_enabled"] is True
        c.post("/api/me/logout")

        # password alone is not enough any more; a wrong code fails; the next code works
        r = c.post("/api/me/login", json={"email": "two@example.com", "password": "a-new-long-pass"})
        assert r.status_code == 401 and r.json()["detail"] == "Two-step code required"
        assert c.get("/api/live/accounts").status_code == 401
        r = c.post("/api/me/login", json={"email": "two@example.com", "password": "a-new-long-pass", "code": "000000"})
        assert r.status_code == 401
        r = c.post("/api/me/login", json={"email": "two@example.com", "password": "a-new-long-pass",
                                          "code": _totp(secret, step + 1)})
        assert r.status_code == 200 and r.json()["role"] == "admin"
        assert c.get("/api/live/accounts").status_code == 200

        # a reset link changes the password but does not sign the admin in (the code is still needed)
        from app.users import new_reset_link

        async def link():
            from sqlalchemy import select

            from app.db import SessionLocal
            from app.models import User
            async with SessionLocal() as ses:
                u = (await ses.execute(select(User).where(User.email == "two@example.com"))).scalars().first()
                url = await new_reset_link(ses, u, by="admin")
                u.totp_last_step = None          # lets the test reuse a current code below
                await ses.commit()
                return url.split("token=")[1]
        token = c.portal.call(link)
        c.post("/api/me/logout")
        r = c.post("/api/me/reset", json={"token": token, "password": "reset-long-pass-1"})
        assert r.status_code == 200 and r.json()["signin"] is True
        assert c.get("/api/live/accounts").status_code == 401

        # passkeys: options need the password; login options are public; a bad request is refused
        r = c.post("/api/me/login", json={"email": "two@example.com", "password": "reset-long-pass-1",
                                          "code": _totp(secret, int(time.time()) // 30)})
        assert r.status_code == 200
        assert c.post("/api/me/passkeys/register/options", json={"password": "wrong"}).status_code == 401
        r = c.post("/api/me/passkeys/register/options", json={"password": "reset-long-pass-1"})
        assert r.status_code == 200
        o = r.json()["options"]
        assert o["rp"]["id"] == "localhost" and o["authenticatorSelection"]["userVerification"] == "required"
        bad = {"state": r.json()["state"], "credential": {"id": "x", "rawId": "x", "type": "public-key",
                                                         "response": {"clientDataJSON": "e30", "attestationObject": "AA"}}}
        assert c.post("/api/me/passkeys/register", json=bad).status_code == 400
        assert c.post("/api/me/passkeys/register", json=bad).status_code == 400        # state used once
        assert c.get("/api/me/passkeys").json() == {"passkeys": []}
        r = c.post("/api/me/passkeys/login/options")
        assert r.status_code == 200 and r.json()["options"]["userVerification"] == "required"
        r = c.post("/api/me/passkeys/login", json={"state": r.json()["state"], "credential": {"id": "nope"}})
        assert r.status_code == 401

        async def drop():
            from sqlalchemy import delete

            from app.db import SessionLocal
            from app.models import User
            async with SessionLocal() as ses:
                await ses.execute(delete(User).where(User.role == "admin"))
                await ses.commit()
        c.portal.call(drop)
    users._fails.clear()
    live_api._fails.clear()
