#!/bin/bash
# Nightly backup of Banana Patterns: the database, .env and config/.
#   scripts/backup.sh                  back up now (safe while the app runs)
#   scripts/backup.sh --install-cron   run it every night at 23:30 (server time, IST)
#   scripts/backup.sh --list           show the backups kept
#
# Each run writes ~/banana-backups/YYYY-MM-DD/ (outside the repo, never committed):
#   bananapatterns.db.gz   SQLite's own online backup, checked with PRAGMA integrity_check
#   env                    a copy of .env (secrets: the folder is private, chmod 700 / 600)
#   config.tar.gz          config/
# The last BP_BACKUP_KEEP (14) days are kept. A failure is written to logs/backup.log and sent to Telegram
# (the bot and chat from .env or the app's settings); a success is silent.
#
# Overrides: BP_BACKUP_DIR (default ~/banana-backups), BP_BACKUP_KEEP (14), BP_DB_PATH (data/bananapatterns.db).
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
DEST="${BP_BACKUP_DIR:-$HOME/banana-backups}"
KEEP="${BP_BACKUP_KEEP:-14}"
DB="${BP_DB_PATH:-data/bananapatterns.db}"
LOG="logs/backup.log"
mkdir -p logs

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG"; }

env_value() {   # the value of KEY in .env, without sourcing it
    [ -f .env ] || return 0
    sed -n "s/^$1=//p" .env | tail -1 | tr -d '\r' | sed 's/^["'\'']//; s/["'\'']$//'
}

alert() {       # tell Telegram (best effort; never fails the script)
    local token chat
    token="$(env_value TELEGRAM_BOT_TOKEN)"
    chat="$(env_value TELEGRAM_CHAT_ID)"
    if [ -z "$chat" ] && [ -f "$DB" ] && command -v sqlite3 >/dev/null 2>&1; then
        chat="$(sqlite3 -readonly "$DB" "SELECT json_extract(value, '\$.chat_id') FROM app_settings \
                WHERE key = 'config.alerts'" 2>/dev/null || true)"
    fi
    [ -n "$chat" ] || chat="$(cat "$DEST/.chat_id" 2>/dev/null || true)"     # saved by the last good run
    [ -n "$token" ] && [ -n "$chat" ] || return 0
    curl -s -m 20 -o /dev/null "https://api.telegram.org/bot$token/sendMessage" \
        --data-urlencode "chat_id=$chat" --data-urlencode "text=⚠️ Banana backup FAILED on $(hostname): $1
See $ROOT/$LOG" || true
}

fail() { log "FAILED: $1"; alert "$1"; exit 1; }

install_cron() {
    local line="30 23 * * * $ROOT/scripts/backup.sh >> $ROOT/$LOG 2>&1"
    command -v crontab >/dev/null 2>&1 || { echo "crontab not found"; exit 1; }
    if crontab -l 2>/dev/null | grep -Fq "$ROOT/scripts/backup.sh"; then
        echo "Already scheduled:"; crontab -l | grep -F "$ROOT/scripts/backup.sh"; return 0
    fi
    (crontab -l 2>/dev/null || true; echo "$line") | crontab -
    echo "Scheduled every night at 23:30 ($(date +%Z)):"; echo "  $line"
}

case "${1:-}" in
    --install-cron) install_cron; exit 0 ;;
    --list) ls -1d "$DEST"/20??-??-?? 2>/dev/null | while read -r d; do du -sh "$d"; done; exit 0 ;;
    "") ;;
    *) sed -n '2,5p' "$0"; exit 1 ;;
esac

command -v sqlite3 >/dev/null 2>&1 || fail "sqlite3 not installed (sudo apt-get install -y sqlite3)"
[ -s "$DB" ] || fail "database not found or empty: $DB"

DAY="$(date +%F)"
OUT="$DEST/$DAY"
TMP="$DEST/.tmp-$DAY-$$"
umask 077
mkdir -p "$DEST" "$TMP"
chmod 700 "$DEST"
trap 'rm -rf "$TMP"' EXIT

# SQLite's online backup: a consistent copy even while the app writes (WAL mode)
sqlite3 "$DB" ".timeout 30000" ".backup '$TMP/bananapatterns.db'" 2>>"$LOG" || fail "sqlite3 .backup failed"
sqlite3 "$TMP/bananapatterns.db" "PRAGMA journal_mode = DELETE" >/dev/null 2>&1 || true   # one self-contained file
# intraday 1-minute candles (⚡ Intraday Flow) can be re-fetched from Kite: left out to keep backups small
sqlite3 "$TMP/bananapatterns.db" "DELETE FROM candles_intraday; VACUUM" >/dev/null 2>>"$LOG" || true
check="$(sqlite3 -readonly "$TMP/bananapatterns.db" "PRAGMA integrity_check" 2>&1 | head -1 || true)"
[ "$check" = "ok" ] || fail "integrity check on the copy: ${check:-no answer}"
tables="$(sqlite3 -readonly "$TMP/bananapatterns.db" "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'" \
          2>/dev/null || true)"
[ "${tables:-0}" -gt 0 ] || fail "the copy has no tables"
chat="$(sqlite3 -readonly "$TMP/bananapatterns.db" "SELECT json_extract(value, '\$.chat_id') FROM app_settings \
        WHERE key = 'config.alerts'" 2>/dev/null || true)"
[ -z "$chat" ] || echo "$chat" > "$DEST/.chat_id"           # so a failure alert still finds the chat
rm -f "$TMP"/bananapatterns.db-wal "$TMP"/bananapatterns.db-shm
gzip -9 "$TMP/bananapatterns.db" || fail "gzip failed"

[ -f .env ] && cp .env "$TMP/env"
[ -d config ] && tar -czf "$TMP/config.tar.gz" config

rm -rf "$OUT"
mv "$TMP" "$OUT"

# keep the newest $KEEP days
ls -1d "$DEST"/20??-??-?? 2>/dev/null | sort -r | tail -n +"$((KEEP + 1))" | while read -r old; do rm -rf "$old"; done

log "OK $OUT ($(du -sh "$OUT" | cut -f1), $tables tables, kept $(ls -1d "$DEST"/20??-??-?? | wc -l | tr -d ' ') days)"
