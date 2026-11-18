"""Smart wallet registry: manual list + automatic discovery.

Sources
  manual  Operator-curated. Added with the CLI below or listed in data/smart_wallets.json
          (re-imported whenever the file changes). Never removed or disabled automatically.
  auto    Discovered from the scanner's own history: wallets that were top holders, at signal time,
          of several tokens that then rallied (see `discover`). Re-evaluated every
          SMART_AUTO_INTERVAL_MINUTES; wallets that stop qualifying are disabled, and a wallet the
          operator disabled is never re-enabled.

Scoring: a token whose top holders include an enabled wallet earns the 15-point smart-wallet
component (scoring_engine W1).

CLI (inside the container:  docker compose exec api python -m app.smart_money <command>)
  list [--all]                         enabled wallets (or all)
  add <chain> <address> [label...]     add / re-enable a manual wallet
  remove <chain> <address>             delete a manual wallet, or disable an auto one permanently
  enable <chain> <address>             re-enable a wallet (clears the operator's disable)
  discover                             run auto-discovery now and print what changed
  import <file.json>                   import {"wallets": [{"address","label","chains"}]}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models import Chain, MarketSnapshot, SecurityAudit, Signal, SmartWallet, Token, WalletSource
from app.repository import normalize_address, utc
from app.schemas import SecurityReport, SmartMoneyInfo

log = logging.getLogger("smart_money")

EVM_BURN = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
}
# Solana addresses that hold tokens but are not traders: the System Program (owner of burned /
# closed token accounts), the incinerator, and AMM authorities that own pooled supply.
SOLANA_NON_TRADERS = {
    "11111111111111111111111111111111",               # System Program
    "1nc1nerator11111111111111111111111111111111",    # incinerator (burn)
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",   # Raydium AMM v4 authority
    "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",   # Raydium CPMM authority
    "WLHv2UAZm6z4KyaaELi5pjdbJh6RESMva1Rnn8pJVVh",    # Meteora / DAMM authority
}
MIN_SIGNAL_AGE_HOURS = 6  # a signal younger than this counts only if it has already won


def eligible_holders(chain: Chain, holders: list[dict[str, Any]], max_pct: float) -> list[str]:
    """Wallet addresses from a holder list that could be traders (not pools, burns, contracts, devs)."""
    out: list[str] = []
    for h in holders or []:
        addr = str(h.get("address") or "")
        if not addr or h.get("is_locked") or (h.get("tag") or "") in {"pool", "insider"}:
            continue
        if chain != Chain.solana and (h.get("is_contract") or addr.lower() in EVM_BURN):
            continue
        if chain == Chain.solana and addr in SOLANA_NON_TRADERS:
            continue
        pct = float(h.get("pct") or 0.0)
        if pct <= 0 or pct > max_pct:
            continue
        out.append(normalize_address(chain, addr))
    return out


# ---------------------------------------------------------------------------
# Registry used by the scanner
# ---------------------------------------------------------------------------


class SmartWalletRegistry:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.wallets: dict[Chain, dict[str, str]] = {}
        self._file_mtime: float | None = None
        self._last_discovery: datetime | None = None
        self._last_birdeye: datetime | None = None

    @property
    def size(self) -> int:
        return sum(len(v) for v in self.wallets.values())

    async def refresh(self, session: AsyncSession) -> None:
        rows = (await session.execute(
            select(SmartWallet.chain, SmartWallet.address, SmartWallet.label).where(SmartWallet.enabled.is_(True))
        )).all()
        wallets: dict[Chain, dict[str, str]] = defaultdict(dict)
        for chain, addr, label in rows:
            wallets[chain][normalize_address(chain, addr)] = label or addr[:6]
        self.wallets = dict(wallets)

    async def sync_file(self, session: AsyncSession) -> int:
        """Import data/smart_wallets.json when it changed since the last sync."""
        path = Path(self.settings.smart_wallets_file)
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            return 0
        if self._file_mtime == mtime:
            return 0
        self._file_mtime = mtime
        try:
            n = await import_file(session, path)
            await session.commit()
            log.info("Imported %d manual smart wallets from %s", n, path)
            return n
        except (OSError, ValueError):
            log.exception("Could not import %s", path)
            return 0

    async def maybe_discover(self, session: AsyncSession) -> dict[str, int] | None:
        if not self.settings.smart_auto_enabled:
            return None
        now = datetime.now(timezone.utc)
        if self._last_discovery and now - self._last_discovery < timedelta(
            minutes=self.settings.smart_auto_interval_minutes
        ):
            return None
        self._last_discovery = now
        stats = await discover(session, self.settings)
        await session.commit()
        log.info("Smart wallet discovery: %s", stats)
        return stats

    async def maybe_discover_birdeye(self, session: AsyncSession) -> dict[str, Any] | None:
        if not self.settings.birdeye_api_key:
            return None
        now = datetime.now(timezone.utc)
        if self._last_birdeye and now - self._last_birdeye < timedelta(minutes=self.settings.birdeye_interval_minutes):
            return None
        self._last_birdeye = now
        from app.birdeye import discover_traders

        try:
            stats = await discover_traders(session, self.settings)
        finally:
            await session.commit()  # keep the call log even if discovery stopped early
        log.info("Birdeye trader discovery: %s", stats)
        return stats

    async def prepare_cycle(self, session: AsyncSession) -> None:
        await self.sync_file(session)
        await self.maybe_discover(session)
        await self.maybe_discover_birdeye(session)
        await self.refresh(session)

    def evaluate(self, chain: Chain, security: SecurityReport | None,
                 held_by: list[str] | None = None) -> SmartMoneyInfo:
        """Tracked wallets among the token's top holders, plus `held_by` from the direct on-chain check."""
        tracked = self.wallets.get(chain) or {}
        if not tracked:
            return SmartMoneyInfo(inferred=True, note="no tracked wallets on this chain")
        hits = set(held_by or [])
        if security is not None:
            hits |= {tracked[a] for h in security.top_holders
                     if (a := normalize_address(chain, h.address)) in tracked}
        note = "" if hits else ("no holder data" if security is None and not held_by else "")
        return SmartMoneyInfo(tracked_wallet_hits=sorted(hits), inferred=not hits, note=note)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@dataclass
class _WalletStats:
    wins: int = 0
    appearances: int = 0
    win_tokens: list[dict[str, Any]] = field(default_factory=list)


async def _peak_gains(session: AsyncSession, rows: list[tuple[Signal, Token]]) -> dict[int, float]:
    """Best price reached after each signal vs the signal price, in percent."""
    gains: dict[int, float] = {}
    for sig, tok in rows:
        if not sig.price_usd or sig.price_usd <= 0:
            continue
        peak = (await session.execute(
            select(func.max(MarketSnapshot.price_usd))
            .where(MarketSnapshot.token_id == tok.id, MarketSnapshot.captured_at >= sig.created_at)
        )).scalar()
        best = max(p for p in (peak, tok.price_usd, sig.price_usd) if p is not None)
        gains[sig.id] = (best / sig.price_usd - 1.0) * 100.0
    return gains


async def winner_tokens(session: AsyncSession, settings: Settings | None = None) -> list[tuple[Token, float]]:
    """Tokens whose price peaked >= SMART_AUTO_WIN_GAIN_PCT after their first signal in the lookback,
    newest signal first, with the peak gain in percent."""
    s = settings or get_settings()
    since = datetime.now(timezone.utc) - timedelta(days=s.smart_auto_lookback_days)
    rows = (await session.execute(
        select(Signal, Token).join(Token, Token.id == Signal.token_id)
        .where(Signal.created_at >= since).order_by(Signal.created_at.asc())
    )).all()
    first: dict[int, tuple[Signal, Token]] = {}
    for sig, tok in rows:
        first.setdefault(tok.id, (sig, tok))
    pairs = list(first.values())
    gains = await _peak_gains(session, pairs)
    won = [(tok, gains[sig.id], sig.created_at) for sig, tok in pairs
           if gains.get(sig.id, 0.0) >= s.smart_auto_win_gain_pct]
    won.sort(key=lambda x: x[2], reverse=True)
    return [(tok, gain) for tok, gain, _ in won]


def _audit_holders(tok: Token, audit: SecurityAudit | None) -> list[dict[str, Any]]:
    """Fallback for signals recorded before holder snapshots existed: the token's latest audit.
    Old Solana audits list SPL token accounts instead of wallets, so they are only used once
    re-fetched in the new format (marked in raw)."""
    if audit is None:
        return []
    if tok.chain == Chain.solana and not (audit.raw or {}).get("holders_are_owners"):
        return []
    return audit.top_holders or []


async def discover(session: AsyncSession, settings: Settings | None = None, checker=None) -> dict[str, int]:
    s = settings or get_settings()
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=s.smart_auto_lookback_days)

    # First signal per token inside the lookback window.
    all_rows = (await session.execute(
        select(Signal, Token).join(Token, Token.id == Signal.token_id)
        .where(Signal.created_at >= since).order_by(Signal.created_at.asc())
    )).all()
    first: dict[int, tuple[Signal, Token]] = {}
    for sig, tok in all_rows:
        first.setdefault(tok.id, (sig, tok))
    rows = list(first.values())
    gains = await _peak_gains(session, rows)

    audits = {a.token_id: a for a in (await session.execute(
        select(SecurityAudit).where(SecurityAudit.token_id.in_([t.id for _, t in rows] or [-1]))
    )).scalars()}

    stats: dict[tuple[Chain, str], _WalletStats] = defaultdict(_WalletStats)
    considered = 0
    for sig, tok in rows:
        gain = gains.get(sig.id)
        if gain is None:
            continue
        won = gain >= s.smart_auto_win_gain_pct
        age_h = (now - utc(sig.created_at)).total_seconds() / 3600
        if not won and age_h < MIN_SIGNAL_AGE_HOURS:
            continue  # too early to call it a loss
        holders = sig.holders or _audit_holders(tok, audits.get(tok.id))
        wallets = set(eligible_holders(tok.chain, holders, s.smart_auto_max_holder_pct))
        if not wallets:
            continue
        considered += 1
        for w in wallets:
            st = stats[(tok.chain, w)]
            st.appearances += 1
            if won:
                st.wins += 1
                st.win_tokens.append({"symbol": tok.symbol, "address": tok.address, "gain_pct": round(gain, 1)})

    qualified = {
        key for key, st in stats.items()
        if st.wins >= s.smart_auto_min_wins
        and st.wins / st.appearances >= s.smart_auto_min_win_rate
        and st.appearances <= s.smart_auto_max_appearances
    }

    existing = {(w.chain, normalize_address(w.chain, w.address)): w
                for w in (await session.execute(select(SmartWallet))).scalars()}
    added = enabled = disabled = 0
    for key, w in existing.items():
        st = stats.get(key)
        if st is not None:
            w.wins, w.appearances = st.wins, st.appearances
            w.win_tokens = sorted(st.win_tokens, key=lambda x: -x["gain_pct"])[:10]
        if w.source == WalletSource.auto:
            should = key in qualified and not w.disabled_by_user
            if should and not w.enabled:
                enabled += 1
            elif not should and w.enabled:
                disabled += 1
            w.enabled = should
            if st is not None:
                w.label = f"auto {st.wins}/{st.appearances}"
    rejected = 0
    new_keys = qualified - existing.keys()
    if new_keys:
        from app.wallet_check import WalletChecker

        own = checker is None
        checker = checker or WalletChecker(s)
        try:
            for key in sorted(new_keys, key=lambda k: k[1]):
                chain, addr = key
                st = stats[key]
                verdict = await checker.check(chain, addr)
                # A rejected address is stored disabled (sticky) with the reason, so it is not re-checked
                # or re-added every 30 minutes.
                session.add(SmartWallet(
                    chain=chain, address=addr, label=f"auto {st.wins}/{st.appearances}", source=WalletSource.auto,
                    enabled=verdict.ok, disabled_by_user=not verdict.ok, wins=st.wins, appearances=st.appearances,
                    win_tokens=sorted(st.win_tokens, key=lambda x: -x["gain_pct"])[:10],
                    stats={"activity_check": verdict.reason, **verdict.info},
                ))
                if verdict.ok:
                    added += 1
                else:
                    rejected += 1
                    log.info("auto wallet %s %s rejected: %s", chain.value, addr[:8], verdict.reason)
        finally:
            if own:
                await checker.close()
    await session.flush()
    return {"signals": len(rows), "tokens_considered": considered, "wallets_seen": len(stats),
            "qualified": len(qualified), "added": added, "rejected_bots": rejected, "re_enabled": enabled,
            "disabled": disabled}


# ---------------------------------------------------------------------------
# Manual management
# ---------------------------------------------------------------------------


async def upsert_manual(session: AsyncSession, chain: Chain, address: str, label: str = "") -> SmartWallet:
    addr = normalize_address(chain, address.strip())
    w = (await session.execute(
        select(SmartWallet).where(SmartWallet.chain == chain, SmartWallet.address == addr)
    )).scalar_one_or_none()
    if w is None:
        w = SmartWallet(chain=chain, address=addr, source=WalletSource.manual, label=label or addr[:6])
        session.add(w)
    else:
        w.source = WalletSource.manual
        if label:
            w.label = label
    w.enabled, w.disabled_by_user = True, False
    await session.flush()
    return w


async def import_file(session: AsyncSession, path: Path) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    n = 0
    for item in data.get("wallets", []):
        addr = str(item.get("address") or "").strip()
        if not addr or addr.upper().startswith("REPLACE"):
            continue
        for c in item.get("chains") or [c.value for c in Chain]:
            try:
                chain = Chain(c)
            except ValueError:
                continue
            if chain != Chain.solana and not addr.startswith("0x"):
                continue
            if chain == Chain.solana and addr.startswith("0x"):
                continue
            await upsert_manual(session, chain, addr, str(item.get("label") or ""))
            n += 1
    return n


async def remove(session: AsyncSession, chain: Chain, address: str) -> str:
    addr = normalize_address(chain, address.strip())
    w = (await session.execute(
        select(SmartWallet).where(SmartWallet.chain == chain, SmartWallet.address == addr)
    )).scalar_one_or_none()
    if w is None:
        return "not found"
    if w.source == WalletSource.manual:
        await session.delete(w)
        return "deleted"
    w.enabled, w.disabled_by_user = False, True
    return "disabled (auto wallets stay disabled so discovery does not re-add them)"


# ---------------------------------------------------------------------------
# Buy-alert watchlist (independent of scoring)
# ---------------------------------------------------------------------------

EVM_CHAINS = (Chain.ethereum, Chain.base, Chain.bsc)


def parse_track_target(chain_arg: str, address: str) -> tuple[list[Chain], str]:
    """Validate '<chain> <address>'. 'evm' means Ethereum + Base + BSC. Raises ValueError with a user message."""
    from app.solana_pda import pubkey_bytes
    from app.wallet_tracker import EVM_ADDRESS

    c = chain_arg.strip().lower()
    a = address.strip()
    if c in ("sol", "solana"):
        try:
            pubkey_bytes(a)
        except (ValueError, KeyError):
            raise ValueError("That is not a valid Solana address.") from None
        return [Chain.solana], a
    if c in ("evm", "eth", "ethereum", "base", "bsc"):
        if not EVM_ADDRESS.match(a):
            raise ValueError("That is not a valid 0x… address.")
        chains = list(EVM_CHAINS) if c == "evm" else [Chain.ethereum if c in ("eth", "ethereum") else Chain(c)]
        return chains, a.lower()
    raise ValueError("Chain must be solana, ethereum, base, bsc or evm.")


async def set_tracking(session: AsyncSession, chains: list[Chain], address: str, label: str, on: bool) -> int:
    """Turn buy alerts on/off. Creates a watch-only row (not used for scoring) when the wallet is unknown."""
    n = 0
    for chain in chains:
        addr = normalize_address(chain, address)
        w = (await session.execute(
            select(SmartWallet).where(SmartWallet.chain == chain, SmartWallet.address == addr)
        )).scalar_one_or_none()
        if w is None:
            if not on:
                continue
            w = SmartWallet(chain=chain, address=addr, source=WalletSource.manual, enabled=False,
                            label=label or f"{addr[:4]}…{addr[-4:]}")
            session.add(w)
        elif label and on:
            w.label = label
        if not on and w.source == WalletSource.manual and not w.enabled:
            await session.delete(w)  # watch-only row: nothing else depends on it
        else:
            w.track_buys = on
        n += 1
    await session.flush()
    return n


async def tracked_wallets(session: AsyncSession) -> list[SmartWallet]:
    return list((await session.execute(
        select(SmartWallet).where(SmartWallet.track_buys.is_(True)).order_by(SmartWallet.chain, SmartWallet.label)
    )).scalars())


async def _cli(argv: list[str] | None = None) -> None:
    from app.db import SessionLocal, init_db

    p = argparse.ArgumentParser(prog="python -m app.smart_money", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--all", action="store_true")
    for name in ("add", "remove", "enable"):
        sp = sub.add_parser(name)
        sp.add_argument("chain", choices=[c.value for c in Chain])
        sp.add_argument("address")
        if name == "add":
            sp.add_argument("label", nargs="*")
    sub.add_parser("discover")
    tr = sub.add_parser("track", help="buy alerts on: track <solana|ethereum|base|bsc|evm> <address> [label]")
    tr.add_argument("chain")
    tr.add_argument("address")
    tr.add_argument("label", nargs="*")
    ut = sub.add_parser("untrack", help="buy alerts off: untrack <chain|evm> <address>")
    ut.add_argument("chain")
    ut.add_argument("address")
    sub.add_parser("tracked", help="list wallets with buy alerts")
    au = sub.add_parser("audit", help="re-check enabled auto/birdeye wallets for bot/program/contract activity")
    au.add_argument("--apply", action="store_true", help="disable the ones that fail (default: report only)")
    be = sub.add_parser("birdeye", help="run Birdeye trader discovery now (uses the daily call budget)")
    be.add_argument("--force-seed", action="store_true", help="re-run today's chain-wide seed")
    be.add_argument("--prune", action="store_true", help="first drop unverified/rejected seed-only candidates")
    sub.add_parser("candidates", help="show Birdeye trader candidates and why they were rejected")
    imp = sub.add_parser("import")
    imp.add_argument("file", type=Path)
    a = p.parse_args(argv)

    await init_db()
    async with SessionLocal() as s:
        if a.cmd == "list":
            q = select(SmartWallet).order_by(SmartWallet.source, SmartWallet.wins.desc())
            if not a.all:
                q = q.where(SmartWallet.enabled.is_(True))
            rows = (await s.execute(q)).scalars().all()
            for w in rows:
                flag = "on " if w.enabled else ("off*" if w.disabled_by_user else "off")
                print(f"{flag:4} {w.source.value:6} {w.chain.value:8} {w.address:44} "
                      f"wins {w.wins}/{w.appearances:<3} {w.label}")
            print(f"{len(rows)} wallet(s)")
        elif a.cmd == "add":
            w = await upsert_manual(s, Chain(a.chain), a.address, " ".join(a.label))
            print(f"added {w.chain.value} {w.address} ({w.label})")
        elif a.cmd == "remove":
            print(await remove(s, Chain(a.chain), a.address))
        elif a.cmd == "enable":
            addr = normalize_address(Chain(a.chain), a.address)
            w = (await s.execute(select(SmartWallet).where(SmartWallet.chain == Chain(a.chain),
                                                           SmartWallet.address == addr))).scalar_one_or_none()
            if w is None:
                print("not found")
            else:
                w.enabled, w.disabled_by_user = True, False
                print("enabled")
        elif a.cmd in ("track", "untrack"):
            chains, addr = parse_track_target(a.chain, a.address)
            n = await set_tracking(s, chains, addr, " ".join(getattr(a, "label", []) or []), a.cmd == "track")
            print(f"{a.cmd}: {n} wallet/chain entr{'y' if n == 1 else 'ies'} updated")
        elif a.cmd == "tracked":
            for w in await tracked_wallets(s):
                print(f"{w.chain.value:8} {w.address:44} {w.label}  ({'scoring' if w.enabled else 'watch-only'})")
        elif a.cmd == "audit":
            import asyncio as _aio

            from app.wallet_check import WalletChecker

            checker = WalletChecker(get_settings().model_copy(update={"smart_activity_check_enabled": True}))
            rows = (await s.execute(select(SmartWallet).where(
                SmartWallet.enabled.is_(True), SmartWallet.source != WalletSource.manual))).scalars().all()
            failed = 0
            try:
                for w in rows:
                    v = await checker.check(w.chain, w.address)
                    flag = "OK " if v.ok else "BOT"
                    print(f"{flag} {w.source.value:7} {w.chain.value:8} {w.address:44} {w.label[:22]:22} {v.reason}")
                    if not v.ok:
                        failed += 1
                        if a.apply:
                            w.enabled, w.disabled_by_user = False, True
                            w.stats = {**(w.stats or {}), "activity_check": v.reason}
                        if w.track_buys:
                            print("    ^ on the owner's buy-alert list: left as is (tracking is the owner's call)")
                    await _aio.sleep(0.6)  # gentle on the public RPC
            finally:
                await checker.close()
            print(f"{len(rows)} checked, {failed} failed" + ("" if a.apply else " (report only; add --apply)"))
        elif a.cmd == "discover":
            print(json.dumps(await discover(s), indent=1))
        elif a.cmd == "birdeye":
            from app.birdeye import discover_traders

            from sqlalchemy import delete

            from app.models import TraderCandidate

            if a.prune:
                cands = (await s.execute(select(TraderCandidate).where(TraderCandidate.qualified.is_(False)))).scalars().all()
                junk = [c.id for c in cands if not c.winner_hits]
                if junk:
                    await s.execute(delete(TraderCandidate).where(TraderCandidate.id.in_(junk)))
                print(f"pruned {len(junk)} seed-only candidates")
            print(json.dumps(await discover_traders(s, force_seed=a.force_seed), indent=1, default=str))
        elif a.cmd == "candidates":
            from app.models import TraderCandidate

            rows = (await s.execute(select(TraderCandidate).order_by(TraderCandidate.qualified.desc(),
                                                                     TraderCandidate.best_pnl_usd.desc()))).scalars().all()
            for c in rows:
                st = c.summary or {}
                state = "OK " if c.qualified else ("-- " if c.verified_at else ".. ")
                print(f"{state} {c.chain.value:8} {c.address:44} best ${c.best_pnl_usd:>10,.0f} "
                      f"wr {st.get('win_rate', 0):.0%} 30d ${st.get('realized_usd_30d', 0):>10,.0f} "
                      f"{c.reject_reason or ''} {','.join(c.origins)[:40]}")
            print(f"{len(rows)} candidate(s)")
        elif a.cmd == "import":
            print(f"imported {await import_file(s, a.file)} wallet entries")
        await s.commit()


if __name__ == "__main__":
    logging.basicConfig(level="WARNING")
    asyncio.run(_cli())
