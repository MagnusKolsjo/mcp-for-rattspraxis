#!/bin/bash
# synk_daglig.sh — kör den dagliga synken av Domstolsverkets publiceringar
# och loggar till logs/synk-ÅÅÅÅ-MM-DD.log. Anropas av launchd (macOS) eller
# cron (Linux); installera med:
#   .venv/bin/python3 01_synka_publiceringar.py --installera-schema

set -euo pipefail

SERVER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SERVER_DIR"

LOG_DIR="$SERVER_DIR/logs"
mkdir -p "$LOG_DIR"
LOG_FIL="$LOG_DIR/synk-$(date +%Y-%m-%d).log"
exec >> "$LOG_FIL" 2>&1

echo
echo "===== $(date '+%Y-%m-%d %H:%M:%S') — daglig synk startar ====="

# .env exporteras så att PYTHON_SOKVAG och LOGGRADER_BEHALL_DAGAR syns här.
# Python-skriptet läser .env själv också.
if [ -f "$SERVER_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$SERVER_DIR/.env"
    set +a
fi
PYTHON="${PYTHON_SOKVAG:-$SERVER_DIR/.venv/bin/python3}"

find "$LOG_DIR" -name "synk-*.log" -mtime +"${LOGGRADER_BEHALL_DAGAR:-30}" -delete

echo "[$(date '+%H:%M:%S')] Steg 1: publiceringar"
"$PYTHON" "$SERVER_DIR/01_synka_publiceringar.py" || {
    echo "[$(date '+%H:%M:%S')] Steg 1 felade — avbryter"
    exit 1
}

echo "===== $(date '+%Y-%m-%d %H:%M:%S') — daglig synk klar ====="
