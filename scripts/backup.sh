#!/usr/bin/env bash
# Ежедневный бэкап SQLite через `sqlite3 .backup` (безопасно на живой БД,
# в отличие от копирования файла). Хранит 14 последних копий. См. §16.
set -euo pipefail

DB_PATH="${DATABASE_PATH:-./data/bot.db}"
BACKUP_DIR="${BACKUP_DIR:-./data/backups}"
KEEP=14

mkdir -p "$BACKUP_DIR"

timestamp=$(date -u +%Y%m%d-%H%M%S)
dest="$BACKUP_DIR/bot-$timestamp.db"

sqlite3 "$DB_PATH" ".backup '$dest'"
echo "backed up $DB_PATH -> $dest"

# Оставить только $KEEP последних копий.
ls -1t "$BACKUP_DIR"/bot-*.db 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm --
