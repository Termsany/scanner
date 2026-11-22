"""Web push alerts for copy-trading events (new position opened, position reduced / closed).

Browsers receive these through the dashboard's service worker (frontend/public/sw.js), even when the /live
tab is closed. Subscriptions are stored in the `push_subscriptions` table; the owner enables them once on /live.

Keys (VAPID) are generated once on the server and kept in backend/.env:
    docker compose exec worker python -m app.push
    LIVE_VAPID_PUBLIC_KEY=...   (public, sent to the browser)
    LIVE_VAPID_PRIVATE_KEY=...  (secret, never leaves the server)
Without them, push is simply disabled and nothing else changes.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

from sqlalchemy import delete, select

from app.config import Settings, get_settings
from app.models import PushSubscription

log = logging.getLogger("push")


def generate_keys() -> tuple[str, str]:
    """(public, private) as base64url strings, in the formats the browser and pywebpush expect."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from py_vapid import Vapid

    v = Vapid()
    v.generate_keys()
    pub = v.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    pub_b64 = base64.urlsafe_b64encode(pub).rstrip(b"=").decode()
    scalar = v.private_key.private_numbers().private_value.to_bytes(32, "big")
    return pub_b64, base64.urlsafe_b64encode(scalar).rstrip(b"=").decode()


def _vapid(private_key: str):
    from py_vapid import Vapid

    return Vapid.from_string(private_key)


async def send_all(settings: Settings, title: str, body: str, url: str = "/live") -> int:
    """Send to every subscription. Returns how many accepted it; dead subscriptions are removed."""
    if not (settings.live_vapid_private_key and settings.live_vapid_public_key):
        return 0
    from app.db import SessionLocal

    async with SessionLocal() as s:
        rows = (await s.execute(select(PushSubscription))).scalars().all()
        subs = [(r.id, {"endpoint": r.endpoint, "keys": {"p256dh": r.p256dh, "auth": r.auth}}) for r in rows]
    if not subs:
        return 0
    payload = json.dumps({"title": title, "body": body, "url": url})
    vapid = _vapid(settings.live_vapid_private_key)
    sent, dead = 0, []
    for sub_id, info in subs:
        try:
            await asyncio.to_thread(_webpush, info, payload, vapid, settings.live_vapid_subject)
            sent += 1
        except Exception as exc:  # noqa: BLE001 - one bad endpoint must not stop the others
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                dead.append(sub_id)            # the browser unsubscribed: forget it
            else:
                log.warning("push to %s failed: %r", info["endpoint"][:40], exc)
    if dead:
        async with SessionLocal() as s:
            await s.execute(delete(PushSubscription).where(PushSubscription.id.in_(dead)))
            await s.commit()
    return sent


def _webpush(info: dict[str, Any], payload: str, vapid, subject: str) -> None:
    from pywebpush import webpush

    webpush(subscription_info=info, data=payload, vapid_private_key=vapid,
            vapid_claims={"sub": subject}, ttl=300)


async def run_test(settings: Settings | None = None) -> int:
    s = settings or get_settings()
    return await send_all(s, "🔔 Alpha Scanner", "Alerts are working. You'll get one when a copy trade opens or closes.")


if __name__ == "__main__":   # docker compose exec worker python -m app.push
    pub, priv = generate_keys()
    print(f"LIVE_VAPID_PUBLIC_KEY={pub}")
    print(f"LIVE_VAPID_PRIVATE_KEY={priv}")
