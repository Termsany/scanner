"""Copy-trading accounts stored in the database (table trading_accounts), edited from the /accounts page.

On first use the table is seeded from the environment (LIVE_* settings and LIVE_ACCOUNTS), so existing accounts keep
running unchanged and keep their keys in the environment. Accounts made on the page get a new wallet (or an imported
key) stored encrypted with LIVE_KEY_ENCRYPTION_KEY; the key is never sent back, except once when generated.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.live import PUBKEY, Account, Leader, _get, _key, accounts_from_settings, load_keypair
from app.models import Chain, SmartWallet, TradingAccount, WalletSource

NAME_RE = re.compile(r"[a-z0-9_]{1,24}")
WALLET_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")
MAX_LEADERS = 25


class AccountError(ValueError):
    pass


# ---- private keys -------------------------------------------------------------
def _fernet(s: Settings):
    from cryptography.fernet import Fernet

    if not s.live_key_encryption_key:
        raise AccountError("LIVE_KEY_ENCRYPTION_KEY is not set on the server")
    try:
        return Fernet(s.live_key_encryption_key.encode())
    except Exception:
        raise AccountError("LIVE_KEY_ENCRYPTION_KEY is not a valid Fernet key") from None


def encrypt_key(s: Settings, secret: str) -> str:
    return _fernet(s).encrypt(secret.encode()).decode()


def decrypt_key(s: Settings, token: str) -> str:
    return _fernet(s).decrypt(token.encode()).decode()


def new_wallet() -> tuple[str, str]:
    """(pubkey, base58 secret) of a fresh Solana keypair; the secret imports into Phantom as a private key."""
    from solders.keypair import Keypair

    kp = Keypair()
    return str(kp.pubkey()), str(kp)


def private_key_for(row: TradingAccount, s: Settings) -> str | None:
    if row.key_source == "db":
        return decrypt_key(s, row.key_enc) if row.key_enc else None
    for a in accounts_from_settings(s):
        if a.name == row.name:
            return a.private_key
    return None


# ---- rows <-> engine accounts ----------------------------------------------------
def clean_leaders(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out, seen = [], set()
    for d in raw or []:
        w = str(d.get("wallet", "")).strip()
        if not WALLET_RE.fullmatch(w):
            raise AccountError(f"not a Solana wallet address: {w[:50]!r}")
        if w in seen:
            continue
        seen.add(w)
        size = d.get("size_usd")
        if size is not None:
            size = float(size)
            if not 0 < size <= 100_000:
                raise AccountError(f"entry size for {w[:6]} must be above $0")
        chase = d.get("max_chase_pct")
        if chase is not None and chase != "":
            chase = float(chase)
            if not 0 <= chase <= 10_000:
                raise AccountError(f"max chase for {w[:6]} must be between 0% and 10000%")
        else:
            chase = None
        out.append({"wallet": w, "label": str(d.get("label") or "")[:40],
                    "unconditional": bool(d.get("unconditional")), "new_tokens_only": bool(d.get("new_tokens_only")),
                    "size_usd": size, "enabled": d.get("enabled") is not False, "max_chase_pct": chase,
                    "quick_exit": bool(d.get("quick_exit")), "ignore_sells": bool(d.get("ignore_sells"))})
    if len(out) > MAX_LEADERS:
        raise AccountError(f"at most {MAX_LEADERS} smart wallets per account")
    return out


def check_limits(size: float, max_open: float, max_loss: float, take_capital: float | None,
                 max_hold: int | None) -> None:
    if not 0 < size <= 100_000:
        raise AccountError("entry size must be above $0")
    if max_open < size:
        raise AccountError("trading budget (max open) must be at least one entry")
    if max_loss <= 0:
        raise AccountError("max daily loss must be above $0")
    if take_capital is not None and not 10 <= take_capital <= 10_000:
        raise AccountError("take-capital target must be between 10% and 10000%")
    if max_hold is not None and not 1 <= max_hold <= 24 * 90:
        raise AccountError("max hold must be between 1 and 2160 hours")


def to_account(row: TradingAccount, s: Settings, private_key: str | None) -> Account:
    leaders = {d["wallet"]: Leader(d["wallet"], d.get("label") or "", bool(d.get("unconditional")),
                                   bool(d.get("new_tokens_only")), d.get("size_usd"), d.get("enabled") is not False,
                                   d.get("max_chase_pct"), bool(d.get("quick_exit")), bool(d.get("ignore_sells")))
               for d in row.leaders or []}
    return Account(row.name, row.size_usd, row.max_open_usd, row.max_daily_loss_usd, list(leaders), private_key,
                   leaders=leaders, paused=bool(row.paused or row.billing_hold), take_capital_at_pct=row.take_capital_at_pct,
                   max_hold_hours=row.max_hold_hours)


async def seed(session: AsyncSession, s: Settings) -> None:
    """First run: copy the environment's accounts into the table (keys stay in the environment)."""
    if (await session.execute(select(func.count(TradingAccount.id)))).scalar():
        return
    for a in accounts_from_settings(s):
        session.add(TradingAccount(
            name=a.name, key_source="env", pubkey=await _get(session, _key(PUBKEY, a.name)),
            size_usd=a.size_usd, max_open_usd=a.max_open_usd, max_daily_loss_usd=a.max_daily_loss_usd,
            take_capital_at_pct=s.live_take_capital_at_pct,
            leaders=[{"wallet": w, "label": "", "unconditional": w in s.live_unconditional_wallets,
                      "new_tokens_only": w in s.live_new_tokens_only_wallets, "size_usd": None}
                     for w in a.wallets],
        ))
    await session.commit()


async def rows(session: AsyncSession, s: Settings) -> list[TradingAccount]:
    await seed(session, s)
    return list((await session.execute(select(TradingAccount).order_by(TradingAccount.id))).scalars().all())


async def load_accounts(session: AsyncSession, s: Settings) -> list[Account]:
    """Enabled accounts, ready for the engine (with their private keys and the owner's Telegram chat)."""
    from app.models import User

    linked = (await session.execute(
        select(User).where(User.tg_chat_id.is_not(None), User.status == "active"))).scalars().all()
    chats = {u.id: u.tg_chat_id for u in linked}
    langs = {u.id: ("en" if u.lang == "en" else "ar") for u in linked}
    out = []
    for r in await rows(session, s):
        if r.enabled:
            try:
                key = private_key_for(r, s)
            except Exception:
                key = None                     # wrong encryption key: account stays idle, the worker says why
            a = to_account(r, s, key)
            a.user_chat_id = chats.get(r.user_id) if r.user_id else None
            a.user_lang = langs.get(r.user_id, "ar") if r.user_id else "ar"
            out.append(a)
    return out


async def ensure_tracked(session: AsyncSession, wallets: list[str]) -> None:
    """The copy engine acts on the tracker's trades: every leader must be on its watchlist."""
    for w in wallets:
        sw = (await session.execute(select(SmartWallet).where(
            SmartWallet.chain == Chain.solana, SmartWallet.address == w))).scalars().first()
        if sw is None:
            session.add(SmartWallet(chain=Chain.solana, address=w, label="copied", source=WalletSource.manual,
                                    enabled=False, track_buys=True))
        elif not sw.track_buys:
            sw.track_buys = True


def public(row: TradingAccount, open_positions: int = 0) -> dict[str, Any]:
    return {
        "name": row.name, "enabled": row.enabled, "paused": row.paused, "key_source": row.key_source,
        "pubkey": row.pubkey, "size_usd": row.size_usd, "max_open_usd": row.max_open_usd,
        "max_daily_loss_usd": row.max_daily_loss_usd, "take_capital_at_pct": row.take_capital_at_pct,
        "max_hold_hours": row.max_hold_hours, "leaders": row.leaders or [], "open_positions": open_positions,
        "user_id": row.user_id, "cap_size_usd": row.cap_size_usd, "cap_open_usd": row.cap_open_usd,
        "cap_loss_usd": row.cap_loss_usd, "key_revealed": bool(row.key_revealed),
        "billing_hold": bool(row.billing_hold),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
