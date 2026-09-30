#!/usr/bin/env bash
# Consistent copy of the SQLite database into ./backups (kept 14 days).
# Optional off-site copy: add BACKUP_BUCKET=gs://your-bucket to .env
# Daily at 01:30 (crontab -e):
#   30 1 * * * /home/USER/carwash/deploy/backup.sh >> /home/USER/carwash/backups/backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backups

name="washbook-$(date +%Y-%m-%d-%H%M).db"
sudo docker compose exec -T app python -c "
import sqlite3
src = sqlite3.connect('/data/washbook.db')
dst = sqlite3.connect('/data/backup.db')
src.backup(dst)
dst.close(); src.close()
"
sudo docker compose cp app:/data/backup.db "backups/$name"
sudo docker compose exec -T app rm -f /data/backup.db
gzip -f "backups/$name"
find backups -name 'washbook-*.db.gz' -mtime +14 -delete

bucket="$(grep -E '^BACKUP_BUCKET=' .env | cut -d= -f2- || true)"
if [ -n "$bucket" ]; then
  gcloud storage cp "backups/$name.gz" "$bucket/"
fi
echo "$(date -Is) ok backups/$name.gz"
