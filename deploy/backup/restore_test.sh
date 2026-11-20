#!/usr/bin/env bash
# Proves the newest backup can be opened and restored: decrypts it, restores the dump into a throwaway PostgreSQL
# container (nothing touches the live database), and compares row counts with the live database.
#   restore_test.sh [backup file]     default: newest file in /var/backups/alpha-scanner
# To restore for real after a disaster, see deploy/backup/README.md.
set -Eeuo pipefail
umask 077
APP=${ALPHA_DIR:-/opt/alpha-scanner}
set -a; . /etc/alpha-backup.env; set +a
FILE=${1:-$(ls -1t /var/backups/alpha-scanner/alpha-*.tar.gz.enc | head -1)}
WORK=$(mktemp -d)
NAME=alpha-restore-test-$$
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT

openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:BACKUP_PASSPHRASE -in "$FILE" | tar -C "$WORK" -xzf -
for f in db.dump backend.env commit.txt; do [ -s "$WORK/$f" ] || { echo "missing $f in backup"; exit 1; }; done
grep -q '^LIVE_KEY_ENCRYPTION_KEY=' "$WORK/backend.env" || echo "WARNING: backup has no LIVE_KEY_ENCRYPTION_KEY"

docker run -d --name "$NAME" -e POSTGRES_USER=alpha -e POSTGRES_PASSWORD=restoretest -e POSTGRES_DB=alpha \
  postgres:16-alpine >/dev/null
for _ in $(seq 1 30); do docker exec "$NAME" pg_isready -U alpha >/dev/null 2>&1 && break; sleep 1; done
sleep 2
docker exec -i "$NAME" pg_restore -U alpha -d alpha --no-owner < "$WORK/db.dump"

Q="select 'trading_accounts', count(*) from trading_accounts union all select 'users', count(*) from users
   union all select 'live_positions', count(*) from live_positions union all select 'audit_log', count(*) from audit_log"
echo "table              backup   live"
FAIL=0
while IFS='|' read -r t n; do
  live=$(cd "$APP" && docker compose exec -T postgres psql -U alpha -d alpha -tA -c "select count(*) from $t")
  printf '%-18s %6s %6s\n' "$t" "$n" "$live"
  [ "$n" -le "$live" ] || FAIL=1
done < <(docker exec "$NAME" psql -U alpha -d alpha -tA -F'|' -c "$Q")
[ "$FAIL" = 0 ] && echo "RESTORE TEST OK: $(basename "$FILE") from $(cat "$WORK/created.txt")" || { echo "RESTORE TEST: counts look wrong"; exit 1; }
