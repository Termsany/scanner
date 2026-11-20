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
        assert c.get("/api/live/admin/me").json() == {"email": "boss@example.com", "via": "session"}
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
    users._fails.clear()
    live_api._fails.clear()
