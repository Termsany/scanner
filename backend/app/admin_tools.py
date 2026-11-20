"""Operator recovery from the server shell (inside the api container):

    docker compose exec api python -m app.admin_tools reset-2fa <admin email>

Turns off the admin's two-step codes (phone lost); sign in with email + password, then turn them on again.
"""

from __future__ import annotations

import asyncio
import sys


async def _reset_2fa(email: str) -> int:
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import User
    from app.users import audit, end_all_sessions

    async with SessionLocal() as s:
        u = (await s.execute(select(User).where(User.email == email.strip().lower(), User.role == "admin"))
             ).scalars().first()
        if u is None:
            print(f"no admin with email {email}")
            return 1
        u.totp_enabled, u.totp_secret_enc, u.totp_last_step = False, None, None
        await end_all_sessions(s, u.id)
        await audit(s, "shell", "admin_2fa_reset", u.email)
        await s.commit()
    print(f"two-step codes turned off for {email}; every session signed out")
    return 0


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "reset-2fa":
        return asyncio.run(_reset_2fa(sys.argv[2]))
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
