#!/bin/bash
# Cron-safe scan wrapper — runs independently of the web server.
# Logs to data/cron_scan.log (last 500 lines kept).

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOGFILE="$PROJECT_DIR/data/cron_scan.log"

cd "$PROJECT_DIR"

echo "--- $(date) ---" >> "$LOGFILE"
PYTHONPATH="$PROJECT_DIR" "$PROJECT_DIR/.venv/bin/python" scripts/run_scan.py >> "$LOGFILE" 2>&1

# Keep log from growing forever (tail last 500 lines)
tail -500 "$LOGFILE" > "$LOGFILE.tmp" && mv "$LOGFILE.tmp" "$LOGFILE"
