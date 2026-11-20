"""Billing: fee + profit share above a high-water mark, collection from the account's wallet, holds."""

import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_tmp}/billing.db".replace("\\", "/"))
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("LLM_API_KEY", "")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("SMART_ACTIVITY_CHECK_ENABLED", "false")

from datetime import datetime, timedelta, timezone  # noqa: E402

import pytest  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.billing import add_months, charge  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402

ADMIN = {"X-Live-Password": "admin-pass-123"}
PAY_TO = "BCyNgSB1njYWrjHubBc56UXqDqzc4ixk8UHPhGbTQ5aQ"
T0 = datetime(2026, 1, 31, 12, tzinfo=timezone.utc)


def test_add_months_clamps_month_end():
    assert add_months(T0, 1) == datetime(2026, 2, 28, 12, tzinfo=timezone.utc)
    assert add_months(T0, 12) == datetime(2027, 1, 31, 12, tzinfo=timezone.utc)


def test_high_water_mark_sequence():
    fee, pct, hwm = 59.0, 15.0, 0.0
    # month 1: +$200 -> share 30
    share, total, hwm = charge(fee, pct, 200, hwm)
    assert (share, total, hwm) == (30.0, 89.0, 200)
    # month 2: loses $100 (cum 100) -> fee only, mark stays 200
    share, total, hwm = charge(fee, pct, 100, hwm)
    assert (share, total, hwm) == (0.0, 59.0, 200)
    # month 3: +$150 (cum 250) -> share only on the 50 above the old best
    share, total, hwm = charge(fee, pct, 250, hwm)
    assert (share, total, hwm) == (7.5, 66.5, 250)


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
    monkeypatch.setattr(s, "live_mode", "live")
    users._fails.clear()
    live_api._fails.clear()
    yield s
    users._fails.clear()
    live_api._fails.clear()


class FakeX:
    """Stands in for JupiterExecutor: SOL at $100."""
    sent: list = []

    def __init__(self, sol: float, fail: bool = False):
        self.sol, self.fail = sol, fail

    async def sol_price(self):
        return 100.0

    async def sol_balance(self):
        return self.sol

    async def transfer_sol(self, to, lamports):
        if self.fail:
            raise RuntimeError("timeout waiting for confirmation")
        FakeX.sent.append((to, lamports))
        return "SIG123"

    async def close(self):
        pass


def _approved(c, email):
    r = c.post("/api/me/register", json={"email": email, "password": "a-long-password", "accept_terms": True})
    assert r.status_code == 200, r.text
    uid = [u for u in c.get("/api/live/users", headers=ADMIN).json()["users"] if u["email"] == email][0]["id"]
    assert c.post(f"/api/live/users/{uid}/approve", headers=ADMIN, json={}).status_code == 200
    c.post("/api/me/logout")
    return uid


def test_billing_end_to_end(env):
    from sqlalchemy import select

    from app import billing
    from app.db import SessionLocal
    from app.models import Bill, LivePosition, TradingAccount

    notes: list = []

    async def notify(chat, text):
        notes.append((chat, text))

    with TestClient(app) as c:
        # off by default; turning it on needs a wallet
        assert c.get("/api/me/terms").json()["billing"] is False
        assert c.put("/api/live/billing/config", headers=ADMIN, json={"enabled": True}).status_code == 400
        assert c.put("/api/live/billing/config", json={"wallet": PAY_TO}).status_code == 401       # admin only
        uid = _approved(c, "bill@example.com")
        name = f"u{uid}"
        r = c.put("/api/live/billing/config", headers=ADMIN, json={"enabled": True, "wallet": PAY_TO})
        assert r.status_code == 200 and r.json()["config"]["fee_usd"] == 59 and r.json()["config"]["profit_pct"] == 15

        # sign-up now needs the terms accepted
        assert c.post("/api/me/register", json={"email": "x@example.com", "password": "a-long-password"}
                      ).status_code == 400

        async def scenario():
            async with SessionLocal() as s:
                row = (await s.execute(select(TradingAccount).where(TradingAccount.name == name))).scalars().first()
                anchor = row.billing_anchor
                anchor = anchor if anchor.tzinfo else anchor.replace(tzinfo=timezone.utc)
                assert anchor > datetime.now(timezone.utc) + timedelta(days=6)          # 7-day trial
                # trial profit $40 (never charged), then +$200 in month 1, -$100 in month 2
                for pnl, at in ((40, anchor - timedelta(days=1)), (200, anchor + timedelta(days=5)),
                                (-100, add_months(anchor, 1) + timedelta(days=3))):
                    s.add(LivePosition(mode="live", account=name, token_address="T", symbol="T", leader_wallet="L",
                                       leader_trade_id=1, status="closed", cost_usd=50, proceeds_usd=50 + pnl,
                                       pnl_usd=pnl, closed_at=at))
                await s.commit()
                s_ = get_settings()

                # month 1 ends: bill = 59 + 15% x 200 = 89; wallet has 0.5 SOL ($50): too little
                t1 = add_months(anchor, 1) + timedelta(hours=1)
                res = await billing.run_once(s, s_, lambda k: FakeX(0.5), notify, now=t1)
                assert res["made"] == 1 and res["short"] == 1 and res["paid"] == 0
                b1 = (await s.execute(select(Bill).where(Bill.account == name))).scalars().one()
                assert (b1.share_usd, b1.total_usd, b1.status) == (30.0, 89.0, "due")
                # within the hour: not retried; grace not over: no hold
                res = await billing.run_once(s, s_, lambda k: FakeX(5), notify, now=t1 + timedelta(minutes=30))
                assert res["paid"] == 0
                await s.refresh(row)
                assert not row.billing_hold
                # 4 days later still short: new buys held
                t2 = t1 + timedelta(days=4)
                await billing.run_once(s, s_, lambda k: FakeX(0.5), notify, now=t2)
                await s.refresh(row)
                assert row.billing_hold is True
                from app.accounts import to_account
                assert to_account(row, s_, None).paused                                 # engine stops buys

                # user tops up: paid, hold lifted. 89 / 100 = 0.89 SOL
                FakeX.sent.clear()
                res = await billing.run_once(s, s_, lambda k: FakeX(2.0), notify, now=t2 + timedelta(hours=2))
                assert res["paid"] == 1 and FakeX.sent == [(PAY_TO, 890_000_000)]
                await s.refresh(b1)
                await s.refresh(row)
                assert b1.status == "paid" and b1.signature == "SIG123" and not row.billing_hold
                assert any("paid" in t for _, t in notes)

                # month 2 (loss): fee only; transfer errors -> "check", never resent automatically
                t3 = add_months(anchor, 2) + timedelta(hours=1)
                res = await billing.run_once(s, s_, lambda k: FakeX(2.0, fail=True), notify, now=t3)
                assert res["made"] == 1 and res["check"] == 1
                b2 = (await s.execute(select(Bill).where(Bill.account == name).order_by(Bill.id.desc()))
                      ).scalars().first()
                assert (b2.share_usd, b2.total_usd, b2.status) == (0.0, 59.0, "check")
                FakeX.sent.clear()
                await billing.run_once(s, s_, lambda k: FakeX(2.0), notify, now=t3 + timedelta(hours=3))
                assert FakeX.sent == []
                return b2.id

        bill2 = c.portal.call(scenario)

        # admin: list, then confirm the "check" bill landed
        d = c.get("/api/live/billing", headers=ADMIN).json()
        assert d["totals"]["check"] == 1 and d["bills"][0]["email"] == "bill@example.com"
        assert c.post(f"/api/live/billing/bills/{bill2}", headers=ADMIN, json={"action": "waive"}).status_code == 200
        assert c.post(f"/api/live/billing/bills/{bill2}", headers=ADMIN, json={"action": "waive"}).status_code == 409

        # the user sees their bills and the running estimate, never the wallet key or leaders
        c.post("/api/me/login", json={"email": "bill@example.com", "password": "a-long-password"})
        me = c.get("/api/me").json()["account"]["billing"]
        assert me["fee_usd"] == 59 and len(me["bills"]) == 2 and me["current"]["fee_usd"] == 59
        assert me["bills"][0]["status"] == "waived"


def test_enabling_never_bills_past_months(env):
    from sqlalchemy import select

    from app import billing
    from app.db import SessionLocal
    from app.models import Bill, TradingAccount

    async def notify(chat, text):
        pass

    with TestClient(app) as c:
        c.put("/api/live/billing/config", headers=ADMIN, json={"enabled": False, "wallet": PAY_TO})
        uid = _approved(c, "old@example.com")
        name = f"u{uid}"

        async def age_it():
            async with SessionLocal() as s:
                row = (await s.execute(select(TradingAccount).where(TradingAccount.name == name))).scalars().first()
                row.created_at = datetime.now(timezone.utc) - timedelta(days=200)
                row.billing_anchor = None
                await s.commit()
        c.portal.call(age_it)
        assert c.put("/api/live/billing/config", headers=ADMIN, json={"enabled": True}).status_code == 200

        async def run():
            async with SessionLocal() as s:
                await billing.run_once(s, get_settings(), lambda k: FakeX(5), notify)
                return (await s.execute(select(Bill).where(Bill.account == name))).scalars().all()
        assert c.portal.call(run) == []
        c.put("/api/live/billing/config", headers=ADMIN, json={"enabled": False})


def test_coupon_discounts_the_fee_only():
    # 50% off $59 = $29.50 off; the 15% share of $200 profit stays $30
    share, total, hwm = charge(59.0, 15.0, 200, 0, 50)
    assert (share, total) == (30.0, 59.5)
    assert charge(59.0, 15.0, 0, 0, 100)[1] == 0.0


def test_coupons_admin_apply_and_months(env):
    from sqlalchemy import select

    from app import billing
    from app.db import SessionLocal
    from app.models import Bill, TradingAccount, User

    async def notify(chat, text):
        pass

    with TestClient(app) as c:
        assert c.post("/api/live/coupons", json={"code": "x", "percent": 10}).status_code == 401
        assert c.post("/api/live/coupons", headers=ADMIN, json={"code": "a b", "percent": 10}).status_code == 400
        assert c.post("/api/live/coupons", headers=ADMIN, json={"code": "half", "percent": 150}).status_code == 400
        r = c.post("/api/live/coupons", headers=ADMIN, json={"code": "half2", "percent": 50, "months": 2,
                                                            "max_uses": 2})
        assert r.status_code == 200 and r.json()["coupon"]["code"] == "HALF2"
        off = c.post("/api/live/coupons", headers=ADMIN, json={"code": "OLD", "percent": 20}).json()["coupon"]
        assert c.patch(f"/api/live/coupons/{off['id']}", headers=ADMIN, json={"active": False}).status_code == 200

        # bad code at sign-up: refused and nobody is created
        assert c.post("/api/me/register", json={"email": "cp0@example.com", "password": "a-long-password",
                                                "accept_terms": True, "coupon": "nope"}).status_code == 400
        # good code at sign-up (lower case works)
        r = c.post("/api/me/register", json={"email": "cp1@example.com", "password": "a-long-password",
                                             "accept_terms": True, "coupon": "half2"})
        assert r.status_code == 200, r.text
        assert c.post("/api/me/coupon", json={"code": "HALF2"}).status_code == 400       # one per user
        c.post("/api/me/logout")
        # from /me after sign-up; inactive code refused
        c.post("/api/me/register", json={"email": "cp2@example.com", "password": "a-long-password",
                                         "accept_terms": True})
        assert c.post("/api/me/coupon", json={"code": "OLD"}).status_code == 400
        assert c.post("/api/me/coupon", json={"code": "half2"}).status_code == 200
        c.post("/api/me/logout")
        c.post("/api/me/register", json={"email": "cp3@example.com", "password": "a-long-password",
                                         "accept_terms": True})
        assert c.post("/api/me/coupon", json={"code": "half2"}).json()["detail"] == "This code has been used up"
        c.post("/api/me/logout")
        lst = c.get("/api/live/coupons", headers=ADMIN).json()["coupons"]
        h = next(x for x in lst if x["code"] == "HALF2")
        assert h["uses"] == 2 and set(h["users"]) == {"cp1@example.com", "cp2@example.com"}

        uid = [u for u in c.get("/api/live/users", headers=ADMIN).json()["users"]
               if u["email"] == "cp1@example.com"][0]["id"]
        assert c.post(f"/api/live/users/{uid}/approve", headers=ADMIN, json={}).status_code == 200
        c.put("/api/live/billing/config", headers=ADMIN, json={"enabled": True, "wallet": PAY_TO})
        name = f"u{uid}"

        async def months():
            async with SessionLocal() as s:
                row = (await s.execute(select(TradingAccount).where(TradingAccount.name == name))).scalars().first()
                a = row.billing_anchor if row.billing_anchor.tzinfo else row.billing_anchor.replace(tzinfo=timezone.utc)
                await billing.run_once(s, get_settings(), lambda k: FakeX(10), notify,
                                       now=add_months(a, 3) + timedelta(hours=1))
                bills = (await s.execute(select(Bill).where(Bill.account == name).order_by(Bill.id))).scalars().all()
                u = await s.get(User, uid)
                return [(b.fee_usd, b.discount_usd, b.total_usd, b.coupon_code) for b in bills], u.coupon_months_left
        bills, left = c.portal.call(months)
        # 2 discounted months, then full price; no profit -> fee only
        assert bills == [(59.0, 29.5, 29.5, "HALF2"), (59.0, 29.5, 29.5, "HALF2"), (59.0, 0.0, 59.0, None)]
        assert left == 0
        c.put("/api/live/billing/config", headers=ADMIN, json={"enabled": False})
