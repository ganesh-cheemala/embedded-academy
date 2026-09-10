#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [[ ! -f .env ]]; then echo "Missing .env — copy .env.example to .env and set production values."; exit 1; fi
python3 scripts/self_check.py
docker compose pull
docker compose build --pull
docker compose up -d
echo "Embedded Academy is starting. Check: curl -fsS http://127.0.0.1:8000/health"
