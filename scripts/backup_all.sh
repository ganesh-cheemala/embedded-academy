#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
STAMP="$(date +%Y%m%d_%H%M%S)"
mkdir -p backups
if [[ -n "${DATABASE_URL:-}" ]]; then
  pg_dump "$DATABASE_URL" > "backups/academy_${STAMP}.sql"
else
  cp data/academy.db "backups/academy_${STAMP}.db"
fi
tar -czf "backups/academy_uploads_${STAMP}.tar.gz" uploads 2>/dev/null || true
echo "Backup created under $ROOT/backups"
