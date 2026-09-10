# Embedded Academy v10

A production-oriented FastAPI learning platform for the **70 Days Embedded Systems Course**.

## What v8 adds

- Password recovery with single-use, 30-minute reset links
- Admin-generated reset links for students
- Public certificate verification at `/verify`
- In-app reminder runner for inactive students
- Reminder scheduler hook: `scripts/reminder_hook.sh`
- Login throttling for repeated failed attempts
- Existing student/admin authentication, 70-day curriculum, resources, quizzes, assignments and submissions
- Certificate eligibility + downloadable PDF certificates
- Batch analytics, student progress and notification inbox
- Bulk CSV student onboarding
- SQLite local development and PostgreSQL deployment mode
- Optional SMTP email delivery for invitations and password resets
- Public certificate verification with QR-ready verification URLs
- Docker Compose + persistent database/uploads volumes
- Backup/restore tooling and production environment configuration

## Local development

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`.

Demo credentials are created on first run only:
- admin: `admin123` unless `ACADEMY_ADMIN_PASSWORD` is set
- student01: `student123` unless `ACADEMY_DEMO_PASSWORD` is set

Change these before real use.

## Account recovery and email delivery

`/forgot-password` creates a single-use reset link. When SMTP is configured, the link is emailed to the student's registered email address; otherwise the link is displayed for secure manual sharing.

Administrators can **Invite** a student from the Control Center. The invitation uses the same single-use password setup flow.

Set these variables for email delivery: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_TLS=1`, plus `ACADEMY_BASE_URL` for absolute links.

## Certificate verification

Public verification page:

`/verify?certificate_id=EA-2026-...`

The certificate page includes a verification link so an employer, mentor or evaluator can validate a certificate without logging in.

## Reminder runner

The Control Center contains a **Run reminder check** action that creates in-app reminders for active students who have not logged in for 3+ days.

For scheduled execution, use `scripts/reminder_hook.sh` from cron/systemd or another scheduler after providing an authenticated service session and `ACADEMY_BASE_URL`.

## PostgreSQL / Docker

Copy `.env.example` to `.env`, replace every placeholder, then run:

```bash
docker compose up -d --build
```

For a real deployment, terminate HTTPS at a reverse proxy, set `ACADEMY_BASE_URL` to the public HTTPS address, and set `ACADEMY_COOKIE_SECURE=1`.

## Backup

PostgreSQL:

```bash
DATABASE_URL='postgresql://...' ./scripts/backup.sh
```

SQLite:

```bash
./scripts/backup.sh
```

PostgreSQL restore:

```bash
DATABASE_URL='postgresql://...' ./scripts/restore_postgres.sh backups/academy_YYYYMMDDTHHMMSSZ.sql.gz
```

## Recommended production architecture

Client -> HTTPS reverse proxy -> FastAPI -> PostgreSQL
                                     |
                                     +-> persistent upload storage

Use managed PostgreSQL and object storage/CDN for large video/media workloads once the academy has a meaningful student volume.

## v10 hardening

- Application version `10.0` with a small idempotent migration runner (`migrations` table) for upgrade-safe schema indexes
- Production HTTP hardening with request IDs and HSTS when `ACADEMY_ENV=production`
- Friendly custom 404/500 pages
- Admin audit log at `/admin/audit`
- Nginx reverse-proxy configuration with upload-size and static-asset handling
- Docker Compose now uses an app + PostgreSQL + Nginx topology
- Backup retention support via `BACKUP_RETENTION_DAYS` (default 14)

### Deployment notes

For internet-facing deployment, put a real TLS certificate on the reverse proxy/load balancer. Set `ACADEMY_BASE_URL` to the public HTTPS URL, `ACADEMY_COOKIE_SECURE=1`, and restrict `ACADEMY_TRUSTED_HOSTS` to the actual hostnames. Do not keep the default database/admin passwords.

The reverse-proxy example listens on port 8000 for convenience. In a real server, bind it behind ports 80/443 at the host or use a managed load balancer with HTTPS termination.


## v12 launch-candidate additions
- Practical submission review with status + reviewer notes.
- CSV exports for students and full lesson progress.
- Admin summary JSON endpoint at `/api/summary`.
- Schema migration v3 adds review metadata safely on SQLite/PostgreSQL.
- v12 keeps v10 deployment, security, certificate, email and notification layers.


## v14 operational additions
- Temporary student credentials now force a password change at first login.
- Admin password resets also force a password change.
- Added launch-readiness signals in Control Center.
- Added deployment self-check and one-command deployment helper under `scripts/`.
- Added a launch checklist under `docs/LAUNCH_CHECKLIST.md`.


## Production
See `docs/DEPLOYMENT_V14.md` and `.env.production.example`. v14 introduces Admin / Instructor / Student roles and academy logo management.
