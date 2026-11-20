#!/usr/bin/env bash
# Installs the host-side ops of Alpha Scanner (idempotent): daily encrypted backups and the 5-minute watchdog,
# as systemd timers. Run as root from the repo: bash deploy/install_ops.sh
set -euo pipefail
APP=$(cd "$(dirname "$0")/.." && pwd)

command -v rclone >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y -q rclone >/dev/null
mkdir -p /var/lib/alpha-watchdog /var/backups/alpha-scanner
chmod 700 /var/backups/alpha-scanner

if [ ! -f /etc/alpha-backup.env ]; then
  umask 077
  printf '# Alpha Scanner backups. KEEP A COPY OF BACKUP_PASSPHRASE OFF THIS SERVER.\nBACKUP_PASSPHRASE=%s\n# Cloudflare R2 (optional): fill in to upload backups off the server\nR2_ACCOUNT_ID=\nR2_ACCESS_KEY_ID=\nR2_SECRET_ACCESS_KEY=\nR2_BUCKET=\n' \
    "$(openssl rand -base64 33 | tr -d '\n')" > /etc/alpha-backup.env
  echo "created /etc/alpha-backup.env with a new BACKUP_PASSPHRASE"
fi
chmod 600 /etc/alpha-backup.env

cat > /etc/systemd/system/alpha-backup.service <<EOF
[Unit]
Description=Alpha Scanner encrypted database backup
After=docker.service
[Service]
Type=oneshot
Environment=ALPHA_DIR=$APP
ExecStart=/bin/bash $APP/deploy/backup/backup.sh
EOF
cat > /etc/systemd/system/alpha-backup.timer <<EOF
[Unit]
Description=Daily Alpha Scanner backup
[Timer]
OnCalendar=*-*-* 02:30:00 UTC
RandomizedDelaySec=10m
Persistent=true
[Install]
WantedBy=timers.target
EOF
cat > /etc/systemd/system/alpha-watchdog.service <<EOF
[Unit]
Description=Alpha Scanner watchdog (Telegram alerts)
[Service]
Type=oneshot
Environment=ALPHA_DIR=$APP
ExecStart=/usr/bin/python3 $APP/deploy/monitor/watchdog.py
TimeoutStartSec=180
EOF
cat > /etc/systemd/system/alpha-watchdog.timer <<EOF
[Unit]
Description=Alpha Scanner watchdog every 5 minutes
[Timer]
OnBootSec=3min
OnUnitActiveSec=5min
[Install]
WantedBy=timers.target
EOF
cat > /etc/systemd/system/alpha-restore-test.service <<EOF
[Unit]
Description=Alpha Scanner monthly restore test of the newest backup
[Service]
Type=oneshot
Environment=ALPHA_DIR=$APP
ExecStart=/bin/bash -c '$APP/deploy/backup/restore_test.sh || { T=\$(grep ^TELEGRAM_BOT_TOKEN= $APP/backend/.env | cut -d= -f2-); C=\$(grep ^TELEGRAM_CHAT_ID= $APP/backend/.env | cut -d= -f2-); curl -s -o /dev/null "https://api.telegram.org/bot\$T/sendMessage" --data-urlencode chat_id=\$C --data-urlencode "text=🚨 Monthly backup restore test FAILED (journalctl -u alpha-restore-test)"; exit 1; }'
EOF
cat > /etc/systemd/system/alpha-restore-test.timer <<EOF
[Unit]
Description=Monthly Alpha Scanner restore test
[Timer]
OnCalendar=*-*-01 04:00:00 UTC
Persistent=true
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now alpha-backup.timer alpha-watchdog.timer alpha-restore-test.timer >/dev/null
systemctl list-timers 'alpha-*' --no-pager
