#!/usr/bin/env sh
set -eu
[ -n "${DATABASE_URL:-}" ] || { echo "DATABASE_URL is required" >&2; exit 1; }
FILE=${1:-}
[ -f "$FILE" ] || { echo "Usage: $0 backup.sql.gz" >&2; exit 1; }
gunzip -c "$FILE" | psql "$DATABASE_URL"
