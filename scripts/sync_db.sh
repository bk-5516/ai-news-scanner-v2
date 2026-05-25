#!/bin/bash
# Pull the live SQLite DB from Railway down to your local data/news.db.
#
# Usage:
#   RAILWAY_APP_URL=https://your-app.railway.app \
#   BACKUP_TOKEN=your_secret_token \
#   ./scripts/sync_db.sh
#
# Or set them in your shell profile to avoid repeating:
#   export RAILWAY_APP_URL=https://your-app.railway.app
#   export BACKUP_TOKEN=your_secret_token

set -e

if [[ -z "$RAILWAY_APP_URL" || -z "$BACKUP_TOKEN" ]]; then
  echo "Error: RAILWAY_APP_URL and BACKUP_TOKEN must be set."
  echo "Usage: RAILWAY_APP_URL=https://... BACKUP_TOKEN=... ./scripts/sync_db.sh"
  exit 1
fi

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$PROJECT_DIR/data/news.db"
BACKUP="$DEST.bak"

# Keep a backup of the previous local DB
if [[ -f "$DEST" ]]; then
  cp "$DEST" "$BACKUP"
  echo "Backed up existing DB to $BACKUP"
fi

echo "Downloading from $RAILWAY_APP_URL ..."
curl -fS "${RAILWAY_APP_URL}/api/backup?token=${BACKUP_TOKEN}" -o "$DEST"
echo "Synced to $DEST"
echo "Article count: $(sqlite3 "$DEST" 'SELECT COUNT(*) FROM articles WHERE is_duplicate=0')"
