"""Telegram notes to the operator from the API process (sign-ups, key reveals, withdrawals)."""

from __future__ import annotations

import logging

import httpx

from app.config import get_settings

log = logging.getLogger("notify")


async def tell_admin(text: str) -> None:
    s = get_settings()
    if not (s.telegram_bot_token and s.telegram_chat_id):
        return
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage",
                         json={"chat_id": s.telegram_chat_id, "text": text, "disable_web_page_preview": True})
    except Exception:
        log.warning("admin notify failed", exc_info=True)
