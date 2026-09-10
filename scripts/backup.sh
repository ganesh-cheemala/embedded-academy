#!/usr/bin/env sh
set -eu
BACKUP_DIR=${BACKUP_DIR:-backups}
RETENTION_DAYS=${BACKUP_RETENTION_DAYS:-14}
mkdir -p "$BACKUP_DIR"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
if [ -n "${DATABASE_URL:-}" ] && printf '%s' "$DATABASE_URL" | grep -Eq '^postgres(ql)?://'; then
  pg_dump --no-owner --no-privileges "$DATABASE_URL" | gzip > "$BACKUP_DIR/academy_${STAMP}.sql.gz"
else
  DB_PATH=${DB_PATH:-data/academy.db}
  sqlite3 "$DB_PATH" ".backup '$BACKUP_DIR/academy_${STAMP}.db'"
fi
if command -v find >/dev/null 2>&1; then find "$BACKUP_DIR" -type f -mtime "+$RETENTION_DAYS" -delete; fi
printf '%s\n' "Created backup for $STAMP (retention: ${RETENTION_DAYS} days)"
