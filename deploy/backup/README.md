# Backups and restore

`deploy/install_ops.sh` installs three systemd timers on the server:

| Timer | When | What |
|---|---|---|
| `alpha-backup` | daily 02:30 UTC | `pg_dump` + `backend/.env`, AES-256 encrypted with `BACKUP_PASSPHRASE`; 14 kept in `/var/backups/alpha-scanner`, 30 days on Cloudflare R2 when configured |
| `alpha-watchdog` | every 5 min | Telegram alert when a container, engine, the scanner, the site, the RPC, a wallet's SOL, the disk or the backups go wrong, and when it's fixed |
| `alpha-restore-test` | 1st of the month | restores the newest backup into a throwaway container and compares row counts |

Settings live in `/etc/alpha-backup.env` (root only). **Keep a copy of `BACKUP_PASSPHRASE` off the server**: the
backups can't be opened without it, and the server is exactly what you lose in a disaster.

## Run by hand

```bash
systemctl start alpha-backup && journalctl -u alpha-backup -n 5
bash deploy/backup/restore_test.sh
python3 deploy/monitor/watchdog.py
```

## Restore after a disaster (new server)

1. Install Docker, clone the repo to `/opt/alpha-scanner`.
2. Get the newest backup from R2 (`daily/alpha-*.tar.gz.enc`) and decrypt it:
   `openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in alpha-XXXX.tar.gz.enc | tar -xzf - -C /root/restore`
   (it asks for `BACKUP_PASSPHRASE`).
3. `cp /root/restore/backend.env /opt/alpha-scanner/backend/.env` (and `compose.env` to `.env` if present).
4. `docker compose up -d postgres`, then
   `docker compose exec -T postgres pg_restore -U alpha -d alpha --clean --if-exists --no-owner < /root/restore/db.dump`
5. `docker compose up -d --build`, then `bash deploy/install_ops.sh`.
