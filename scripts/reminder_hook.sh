#!/usr/bin/env bash
set -euo pipefail
: "${ACADEMY_BASE_URL:?Set ACADEMY_BASE_URL, e.g. https://academy.example.com}"
: "${ACADEMY_ADMIN_SESSION_COOKIE:?Set ACADEMY_ADMIN_SESSION_COOKIE for the scheduled service account}"
curl -fsS -X POST "$ACADEMY_BASE_URL/admin/reminders/run" -H "Cookie: $ACADEMY_ADMIN_SESSION_COOKIE" --data-urlencode "csrf_token=$ACADEMY_CSRF_TOKEN"
