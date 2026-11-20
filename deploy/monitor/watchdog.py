#!/usr/bin/env python3
"""Host watchdog for Alpha Scanner (runs every 5 min from a systemd timer, outside Docker).

Telegram the operator when something stops or runs low, once per problem (repeated every REPEAT_HOURS while it
lasts) and again when it is fixed. Standard library only: it must keep working when the app's containers don't.

Checks: containers up and healthy, copy-trading engines stepping (heartbeat), scanner cycles, the public site,
the Solana RPC, SOL left in each active trading wallet, disk space, and how old the last backup is.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

APP = os.environ.get("ALPHA_DIR", "/opt/alpha-scanner")
STATE = os.environ.get("WATCHDOG_STATE", "/var/lib/alpha-watchdog/state.json")
BACKUP_STATUS = "/var/lib/alpha-watchdog/backup.json"
SITE = os.environ.get("WATCHDOG_SITE", "https://scanner.da3m.io/healthz")
SERVICES = ("api", "worker", "postgres", "redis", "dashboard")
HEARTBEAT_MAX_MIN = 6           # engines write every minute
SCANNER_MAX_MIN = 20
LOW_SOL = float(os.environ.get("WATCHDOG_LOW_SOL", "0.03"))
DISK_MAX_PCT = 85
BACKUP_MAX_HOURS = 27
REPEAT_HOURS = 6


def env_file(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


CFG = env_file(f"{APP}/backend/.env")


def sh(args: list[str], timeout: int = 60) -> str:
    return subprocess.run(args, cwd=APP, capture_output=True, text=True, timeout=timeout, check=True).stdout


def sql(query: str) -> list[list[str]]:
    out = sh(["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "alpha", "-d", "alpha", "-tA", "-F", "|",
              "-c", query])
    return [line.split("|") for line in out.splitlines() if line.strip()]


def http(url: str, body: dict | None = None, timeout: int = 15) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json",
                                                          "User-Agent": "alpha-watchdog"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:  # noqa: BLE001
        return 0, type(e).__name__


def telegram(text: str) -> None:
    token, chat = CFG.get("TELEGRAM_BOT_TOKEN"), CFG.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print(text)
        return
    http(f"https://api.telegram.org/bot{token}/sendMessage",
         {"chat_id": chat, "text": text, "disable_web_page_preview": True})


def age_min(iso: str) -> float:
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() / 60


def checks() -> dict[str, str]:
    """{problem id: message} for everything wrong right now."""
    bad: dict[str, str] = {}

    # containers
    try:
        rows = [json.loads(line) for line in sh(["docker", "compose", "ps", "-a", "--format", "json"]).splitlines()
                if line.strip()]
        seen = {r.get("Service"): r for r in rows}
        for svc in SERVICES:
            r = seen.get(svc)
            if r is None or r.get("State") != "running":
                bad[f"svc:{svc}"] = f"Container {svc} is not running ({(r or {}).get('Status', 'missing')})"
            elif r.get("Health") == "unhealthy":
                bad[f"svc:{svc}"] = f"Container {svc} is unhealthy"
    except Exception as e:  # noqa: BLE001
        bad["docker"] = f"Can't read Docker status: {type(e).__name__}"
        return bad

    db_ok = "svc:postgres" not in bad
    mode = CFG.get("LIVE_MODE", "off")
    if db_ok:
        try:
            beats = {("main" if k == "live:heartbeat" else k.split(":", 2)[2]): v
                     for k, v in sql("select key, value from tracker_cursors where key like 'live:heartbeat%'")}
            accts = sql("select name, paused, coalesce(pubkey,''), "
                        "case when key_source='db' then (key_enc is not null)::text else 'env' end, "
                        "coalesce(json_array_length(leaders::json),0) "
                        "from trading_accounts where enabled")
            pubkeys = {("main" if k == "live:pubkey" else k.split(":", 2)[2]): v
                       for k, v in sql("select key, value from tracker_cursors where key like 'live:pubkey%'")}
            if mode in ("live", "dry"):
                for name, paused, pub, has_key, n_leaders in accts:
                    if int(n_leaders) == 0 or has_key == "false":
                        continue                       # nothing to run: idle on purpose
                    beat = beats.get(name)
                    if beat is None or age_min(beat) > HEARTBEAT_MAX_MIN:
                        when = "never" if beat is None else f"{age_min(beat):.0f} min ago"
                        bad[f"beat:{name}"] = f"Copy-trading account [{name}] stopped (last step: {when})"
                # SOL left in active wallets
                rpc = CFG.get("LIVE_RPC_URL") or CFG.get("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com"
                for name, paused, pub, has_key, n_leaders in accts:
                    pub = pubkeys.get(name) or pub
                    if mode != "live" or paused == "t" or not pub or int(n_leaders) == 0:
                        continue
                    code, body = http(rpc, {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [pub]})
                    if code == 200:
                        sol = json.loads(body).get("result", {}).get("value", 0) / 1e9
                        if sol < LOW_SOL:
                            bad[f"sol:{name}"] = (f"Wallet of [{name}] has only {sol:.4f} SOL: buys stop and sells "
                                                  f"may fail without fees")
            last = sql("select coalesce(max(finished_at)::text,'') from scanner_runs")
            if last and last[0][0] and age_min(last[0][0]) > SCANNER_MAX_MIN:
                bad["scanner"] = f"Scanner has not finished a cycle for {age_min(last[0][0]):.0f} min"
        except Exception as e:  # noqa: BLE001
            bad["db"] = f"Can't query the database: {type(e).__name__}"

    # public site
    code, _ = http(SITE)
    if code != 200:
        bad["site"] = f"Site health check failed ({code or 'no answer'}): {SITE}"

    # Solana RPC used for trading
    rpc = CFG.get("LIVE_RPC_URL")
    if rpc and mode == "live":
        code, body = http(rpc, {"jsonrpc": "2.0", "id": 1, "method": "getHealth"})
        if code != 200 or '"ok"' not in body:
            bad["rpc"] = f"Solana RPC is not healthy (HTTP {code or 'no answer'}): trades may fail"

    # disk
    du = shutil.disk_usage("/")
    pct = du.used / du.total * 100
    if pct > DISK_MAX_PCT:
        bad["disk"] = f"Disk {pct:.0f}% full ({du.free / 1e9:.1f} GB free)"

    # backups
    try:
        st = json.load(open(BACKUP_STATUS))
        hours = age_min(st["at"]) / 60
        if hours > BACKUP_MAX_HOURS:
            bad["backup"] = f"Last database backup is {hours:.0f} h old"
        elif st.get("r2") is False and st.get("r2_configured"):
            bad["backup_r2"] = "Last backup was saved on the server but the upload to R2 failed"
    except FileNotFoundError:
        bad["backup"] = "No database backup has run yet"
    except Exception as e:  # noqa: BLE001
        bad["backup"] = f"Can't read the backup status: {type(e).__name__}"
    return bad


def main() -> int:
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    try:
        state = json.load(open(STATE))
    except Exception:  # noqa: BLE001
        state = {}
    now = time.time()
    try:
        bad = checks()
    except Exception as e:  # noqa: BLE001
        bad = {"watchdog": f"Watchdog check crashed: {type(e).__name__}: {e}"}
    new_state = {}
    for pid, msg in bad.items():
        prev = state.get(pid)
        if prev is None:
            telegram(f"🚨 {msg}")
            new_state[pid] = {"since": now, "alerted": now}
        elif now - prev["alerted"] > REPEAT_HOURS * 3600:
            mins = (now - prev["since"]) / 60
            telegram(f"🚨 Still: {msg} (for {mins / 60:.1f} h)")
            new_state[pid] = {"since": prev["since"], "alerted": now}
        else:
            new_state[pid] = prev
    for pid, prev in state.items():
        if pid not in bad:
            telegram(f"✅ Fixed: {pid.split(':', 1)[-1] if ':' in pid else pid} is OK again "
                     f"(after {(now - prev['since']) / 60:.0f} min)")
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(new_state, f)
    os.replace(tmp, STATE)
    print(json.dumps({"problems": bad}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
