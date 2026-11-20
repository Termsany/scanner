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

        # withdrawals need two-step codes first
        assert c.post("/api/me/account/withdraw", json={"password": "a-long-password", "to": "nope"}
                      ).status_code == 403

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


# ---- forgot password, two-step codes, withdrawal lock ------------------------------------
def _approved(c, email):
    _register(c, email)
    uid = [u for u in c.get("/api/live/users", headers=ADMIN).json()["users"] if u["email"] == email][0]["id"]
    c.post(f"/api/live/users/{uid}/approve", headers=ADMIN, json={})
    return uid


def _code(secret, offset=0):
    import time

    from app.users import _totp
    return _totp(secret, int(time.time() // 30) + offset)


def test_forgot_and_reset_password_locks_withdrawals(env, monkeypatch):
    from app.api import me as me_api

    notes = []

    async def tell(text):
        notes.append(text)
    monkeypatch.setattr(me_api, "tell_admin", tell)
    with TestClient(app) as c:
        uid = _approved(c, "frank@example.com")
        c.post("/api/me/logout")
        r = c.post("/api/me/forgot", json={"email": "nobody@example.com"})
        assert r.status_code == 200 and not any("nobody@" in n for n in notes)   # same answer, nothing sent
        r = c.post("/api/me/forgot", json={"email": "FRANK@example.com"})
        assert r.status_code == 200 and r.json() == c.post("/api/me/forgot", json={"email": "x@y.io"}).json()
        link = [n for n in notes if "asked to reset" in n][0].split("\n")[-1]
        token = link.split("token=")[1]
        assert link.startswith("https://") and "/reset?token=" in link
        assert c.post("/api/me/reset", json={"token": "bad", "password": "new-long-password"}).status_code == 400
        assert c.post("/api/me/reset", json={"token": token, "password": "short"}).status_code == 400
        r = c.post("/api/me/reset", json={"token": token, "password": "new-long-password"})
        assert r.status_code == 200 and r.json()["withdraw_lock_hours"] == 24
        assert c.post("/api/me/reset", json={"token": token, "password": "another-password"}).status_code == 400
        assert c.get("/api/me").json()["security"]["withdraw_lock_hours"] > 23            # signed in, locked
        c.post("/api/me/logout")
        assert c.post("/api/me/login", json={"email": "frank@example.com", "password": "a-long-password"}
                      ).status_code == 401
        assert c.post("/api/me/login", json={"email": "frank@example.com", "password": "new-long-password"}
                      ).status_code == 200
        # admin link works too
        link = c.post(f"/api/live/users/{uid}/reset-link", headers=ADMIN).json()["link"]
        assert c.post("/api/me/reset", json={"token": link.split("token=")[1], "password": "third-long-password"}
                      ).status_code == 200


def test_two_step_codes_setup_use_once_and_disable(env, monkeypatch):
    from app.api import me as me_api

    async def tell(text):
        pass
    monkeypatch.setattr(me_api, "tell_admin", tell)
    with TestClient(app) as c:
        uid = _approved(c, "gina@example.com")
        assert c.post("/api/me/2fa/setup", json={"password": "wrong-password"}).status_code == 401
        r = c.post("/api/me/2fa/setup", json={"password": "a-long-password"})
        assert r.status_code == 200 and r.json()["qr"].startswith("data:image/svg+xml") and "otpauth://" in r.json()["uri"]
        secret = r.json()["secret"]
        assert c.get("/api/me").json()["security"]["totp_enabled"] is False                # not before a code
        assert c.post("/api/me/2fa/enable", json={"code": "123456" if _code(secret) != "123456" else "654321"}
                      ).status_code == 401
        assert c.post("/api/me/2fa/enable", json={"code": _code(secret)}).status_code == 200
        assert c.get("/api/me").json()["security"]["totp_enabled"] is True
        assert secret not in c.get("/api/live/users", headers=ADMIN).text

        # key reveal now needs a code; the code used to enable can't be reused
        assert c.post("/api/me/account/reveal-key", json={"password": "a-long-password"}).status_code == 401
        assert c.post("/api/me/account/reveal-key", json={"password": "a-long-password", "code": _code(secret)}
                      ).status_code == 401                                                    # same step: spent
        r = c.post("/api/me/account/reveal-key", json={"password": "a-long-password", "code": _code(secret, 1)})
        assert r.status_code == 200 and r.json()["secret"]

        # withdrawal: code checked, then live mode required (tests run dry)
        r = c.post("/api/me/account/withdraw", json={"password": "a-long-password", "to": LEADER, "code": "000000"})
        assert r.status_code == 401
        # admin resets 2FA (lost phone): off + withdrawals locked
        assert c.post(f"/api/live/users/{uid}/reset-2fa", headers=ADMIN).status_code == 200
        sec = c.get("/api/me").json()["security"]
        assert sec["totp_enabled"] is False and sec["withdraw_lock_hours"] > 23
        entries = c.get("/api/live/audit?q=gina", headers=ADMIN).json()["entries"]
        assert {"2fa_reset", "user_approved"} <= {e["action"] for e in entries}
        assert all("gina" in (e["target"] + e["actor"]) or e["actor"].startswith("user:") for e in entries)


def test_withdraw_lock_blocks_even_with_code(env, monkeypatch):
    from app.api import me as me_api
    from app.users import lock_withdrawals  # noqa: F401

    async def tell(text):
        pass
    monkeypatch.setattr(me_api, "tell_admin", tell)
    monkeypatch.setattr(env, "live_mode", "live")
    with TestClient(app) as c:
        _approved(c, "hank@example.com")
        secret = c.post("/api/me/2fa/setup", json={"password": "a-long-password"}).json()["secret"]
        c.post("/api/me/2fa/enable", json={"code": _code(secret)})
        c.post("/api/me/2fa/disable", json={"password": "a-long-password", "code": _code(secret, 1)})
        secret = c.post("/api/me/2fa/setup", json={"password": "a-long-password"}).json()["secret"]
        c.post("/api/me/2fa/enable", json={"code": _code(secret)})
        r = c.post("/api/me/account/withdraw", json={"password": "a-long-password", "to": LEADER,
                                                     "code": _code(secret, 1)})
        assert r.status_code == 423 and "locked" in r.json()["detail"]             # turning 2FA off locked it


# ---- Telegram link + performance --------------------------------------------------------
def test_telegram_link_through_bot_and_user_safe_commands(env, monkeypatch):
    from app.api import me as me_api
    from app.telegram_alerts import TelegramAlerts

    async def tell(text):
        pass
    monkeypatch.setattr(me_api, "tell_admin", tell)
    monkeypatch.setattr(env, "telegram_bot_token", "123:abc")
    monkeypatch.setattr(env, "telegram_chat_id", "999")          # the owner's chat
    me_api._bot_name[:] = ["alpha_test_bot"]
    with TestClient(app) as c:
        _approved(c, "ivy@example.com")
        assert c.get("/api/me").json()["telegram"]["linked"] is False
        r = c.post("/api/me/telegram/link")
        assert r.status_code == 200 and r.json()["url"].startswith("https://t.me/alpha_test_bot?start=link_")
        code = r.json()["url"].split("start=link_")[1]
        bot = TelegramAlerts(env)
        assert "invalid" in c.portal.call(bot.handle_command, "/start link_wrongcode", 4242)
        reply = c.portal.call(bot.handle_command, f"/start link_{code}", 4242)
        assert "Linked" in reply and "ivy@example.com" in reply
        assert "invalid" in c.portal.call(bot.handle_command, f"/start link_{code}", 4343)    # one use
        assert c.get("/api/me").json()["telegram"]["linked"] is True
        # a linked user's chat only gets user commands: no wallets, no tracking, no help listing owner tools
        for cmd in ("/wallets", "/tracked", "/help", "/start", "/live", "/track So1aNa"):
            out = c.portal.call(bot.handle_command, cmd, 4242)
            assert "/me" in out and "wallet" not in out.lower().replace("trading wallet", "")
        summary = c.portal.call(bot.handle_command, "/me", 4242)
        assert "Your last 24 h" in summary and "/me" in summary
        assert "stop" in c.portal.call(bot.handle_command, "/stop", 4242).lower()
        assert c.get("/api/me").json()["telegram"]["linked"] is False
        c.portal.call(bot.close)


def test_stats_in_me(env):
    import asyncio
    from datetime import datetime, timedelta, timezone

    from app.db import SessionLocal
    from app.models import LivePosition

    with TestClient(app) as c:
        uid = _approved(c, "jack@example.com")
        name = f"u{uid}"
        now = datetime.now(timezone.utc)

        async def add():
            async with SessionLocal() as s:
                for sym, cost, pnl, d in (("A", 10, 15, 0), ("B", 10, -4, 1), ("C", 10, 5, 1), ("D", 10, -10, 40)):
                    s.add(LivePosition(mode=env.live_mode if env.live_mode in ("dry", "live") else "off",
                                       account=name, token_address=sym, symbol=sym, leader_wallet=LEADER,
                                       leader_trade_id=1, status="closed", cost_usd=cost, proceeds_usd=cost + pnl,
                                       pnl_usd=pnl, closed_at=now - timedelta(days=d)))
                await s.commit()
        c.portal.call(add)
        st = c.get("/api/me").json()["account"]["stats"]
        assert st["trades"] == 4 and st["wins"] == 2 and st["win_rate"] == 50
        assert st["total_pnl"] == 6 and st["best"]["symbol"] == "A" and st["worst"]["symbol"] == "D"
        assert len(st["daily"]) == 30 and st["daily"][-1]["pnl"] == 15 and st["daily"][-2]["pnl"] == 1
        assert LEADER not in c.get("/api/me").text
