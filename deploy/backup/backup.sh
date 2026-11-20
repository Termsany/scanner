#!/usr/bin/env bash
# Daily encrypted backup of Alpha Scanner: the PostgreSQL database plus backend/.env (wallet keys and the key that
# decrypts the users' wallet keys). AES-256 with BACKUP_PASSPHRASE; kept 14 days on this server and, when R2 is
# configured, uploaded to Cloudflare R2 (kept 30 days there).
#
# Settings: /etc/alpha-backup.env (root only):
#   BACKUP_PASSPHRASE=...        # KEEP A COPY OFF THE SERVER: without it no backup can be opened
#   R2_ACCOUNT_ID=...  R2_ACCESS_KEY_ID=...  R2_SECRET_ACCESS_KEY=...  R2_BUCKET=...   (optional)
set -Eeuo pipefail
umask 077

APP=${ALPHA_DIR:-/opt/alpha-scanner}
LOCAL=/var/backups/alpha-scanner
STATUS=/var/lib/alpha-watchdog/backup.json
set -a; . /etc/alpha-backup.env; set +a
: "${BACKUP_PASSPHRASE:?BACKUP_PASSPHRASE missing in /etc/alpha-backup.env}"

tg() {   # Telegram the operator (token from the app's .env)
  local token chat
  token=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$APP/backend/.env" | cut -d= -f2- || true)
  chat=$(grep -E '^TELEGRAM_CHAT_ID=' "$APP/backend/.env" | cut -d= -f2- || true)
  [ -n "$token" ] && [ -n "$chat" ] && curl -s -m 15 -o /dev/null "https://api.telegram.org/bot$token/sendMessage" \
    --data-urlencode "chat_id=$chat" --data-urlencode "text=$1" || true
}
trap 'tg "🚨 Database backup FAILED at line $LINENO (see: journalctl -u alpha-backup)"' ERR

mkdir -p "$LOCAL" "$(dirname "$STATUS")"
TS=$(date -u +%Y%m%d-%H%M%S)
OUT="$LOCAL/alpha-$TS.tar.gz.enc"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

cd "$APP"
docker compose exec -T postgres pg_dump -U alpha -d alpha -Fc > "$WORK/db.dump"
[ -s "$WORK/db.dump" ] || { echo "empty dump"; false; }
cp backend/.env "$WORK/backend.env"
[ -f .env ] && cp .env "$WORK/compose.env"
git rev-parse HEAD > "$WORK/commit.txt"
date -u +%FT%TZ > "$WORK/created.txt"
tar -C "$WORK" -czf - . | openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass env:BACKUP_PASSPHRASE -out "$OUT"
SIZE=$(stat -c %s "$OUT")

# keep the newest 14 on this server
ls -1t "$LOCAL"/alpha-*.tar.gz.enc | tail -n +15 | xargs -r rm -f

R2_CONFIGURED=false
R2_OK=false
if [ -n "${R2_ACCOUNT_ID:-}" ] && [ -n "${R2_ACCESS_KEY_ID:-}" ] && [ -n "${R2_SECRET_ACCESS_KEY:-}" ] && [ -n "${R2_BUCKET:-}" ]; then
  R2_CONFIGURED=true
  export RCLONE_CONFIG_R2_TYPE=s3 RCLONE_CONFIG_R2_PROVIDER=Cloudflare RCLONE_CONFIG_R2_ACL=private \
         RCLONE_CONFIG_R2_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" \
         RCLONE_CONFIG_R2_ENDPOINT="https://$R2_ACCOUNT_ID.r2.cloudflarestorage.com" RCLONE_CONFIG_R2_NO_CHECK_BUCKET=true
  if rclone copyto "$OUT" "r2:$R2_BUCKET/daily/$(basename "$OUT")" --s3-no-check-bucket -q; then
    R2_OK=true
    rclone delete "r2:$R2_BUCKET/daily" --min-age 30d -q || true
  else
    tg "⚠️ Backup saved on the server, but the upload to Cloudflare R2 failed (check the R2 keys/bucket)"
  fi
fi

printf '{"at": "%s", "file": "%s", "size": %s, "r2_configured": %s, "r2": %s}\n' \
  "$(date -u +%FT%TZ)" "$(basename "$OUT")" "$SIZE" "$R2_CONFIGURED" "$R2_OK" > "$STATUS"
echo "backup ok: $OUT ($SIZE bytes), r2=$R2_OK"
