#!/bin/bash
# Pull the live SQLite DB from Railway and save a timestamped local backup.
#
# Credentials (set once in your shell profile or ~/.ai_news_scanner_env):
#   export RAILWAY_APP_URL=https://ai-news-scanner-v2-production.up.railway.app
#   export BACKUP_TOKEN=<your token>
#
# Or pass them inline:
#   RAILWAY_APP_URL=... BACKUP_TOKEN=... ./scripts/sync_db.sh

set -e

ENV_FILE="$HOME/.ai_news_scanner_env"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

if [[ -z "$RAILWAY_APP_URL" || -z "$BACKUP_TOKEN" ]]; then
  echo "Error: RAILWAY_APP_URL and BACKUP_TOKEN must be set."
  echo "Either set them in $ENV_FILE or pass them inline."
  exit 1
fi

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="$PROJECT_DIR/data"
CURRENT="$DATA_DIR/news.db"
TIMESTAMP=$(date +%Y-%m-%d_%H%M%S)
ARCHIVE="$DATA_DIR/backups/news_${TIMESTAMP}.db"

mkdir -p "$DATA_DIR/backups"

echo "Downloading from $RAILWAY_APP_URL ..."
curl -fS "${RAILWAY_APP_URL}/api/backup?token=${BACKUP_TOKEN}" -o "$ARCHIVE"

COUNT=$(sqlite3 "$ARCHIVE" 'SELECT COUNT(*) FROM articles WHERE is_duplicate=0' 2>/dev/null || echo "?")
echo "Saved: $ARCHIVE ($COUNT articles)"

# Update the symlink / copy to news.db so local server always has latest
cp "$ARCHIVE" "$CURRENT"
echo "Updated $CURRENT"

# Keep only the last 14 backups
ls -t "$DATA_DIR/backups"/news_*.db 2>/dev/null | tail -n +15 | xargs rm -f --
echo "Done."
