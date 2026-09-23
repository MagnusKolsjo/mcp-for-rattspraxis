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

# .env läses inte in med source: värden som DATABASE_URL kan innehålla &, $
# och citattecken som bash skulle tolka. Python-skriptet läser .env själv
# med python-dotenv. Här behövs bara två enkla värden, som plockas ut som
# text utan att köras. En variabel som redan finns i miljön har företräde.
las_env() {
    local nyckel="$1" rad varde
    [ -f "$SERVER_DIR/.env" ] || return 0
    rad="$(grep -E "^[[:space:]]*${nyckel}=" "$SERVER_DIR/.env" | tail -n 1 || true)"
    varde="${rad#*=}"
    varde="${varde%\"}"; varde="${varde#\"}"
    varde="${varde%\'}"; varde="${varde#\'}"
    printf '%s' "$varde"
}

PYTHON="${PYTHON_SOKVAG:-$(las_env PYTHON_SOKVAG)}"
PYTHON="${PYTHON:-$SERVER_DIR/.venv/bin/python3}"
BEHALL_DAGAR="${LOGGRADER_BEHALL_DAGAR:-$(las_env LOGGRADER_BEHALL_DAGAR)}"
BEHALL_DAGAR="${BEHALL_DAGAR:-30}"

find "$LOG_DIR" -name "synk-*.log" -mtime +"$BEHALL_DAGAR" -delete

echo "[$(date '+%H:%M:%S')] Steg 1: publiceringar"
"$PYTHON" "$SERVER_DIR/01_synka_publiceringar.py" || {
    echo "[$(date '+%H:%M:%S')] Steg 1 felade — avbryter"
    exit 1
}

echo "===== $(date '+%Y-%m-%d %H:%M:%S') — daglig synk klar ====="
