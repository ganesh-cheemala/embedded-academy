# KCI Academy v14 — Production Deployment

## Recommended topology

Internet → DNS → HTTPS reverse proxy → FastAPI → PostgreSQL

The bundled Docker Compose stack uses Nginx as the reverse proxy. Caddyfile is included as an optional alternative when you want automatic HTTPS certificate management.

## 1. Server prerequisites

- Linux VPS with Docker Engine + Docker Compose plugin
- Domain/subdomain pointed to the VPS public IP
- A dedicated production `.env`

## 2. Configure production

Copy `.env.production.example` to `.env` and set:

- `ACADEMY_BASE_URL=https://your-real-domain`
- `ACADEMY_TRUSTED_HOSTS=your-real-domain`
- a long random `ACADEMY_SECRET`
- a strong `POSTGRES_PASSWORD`
- a strong `ACADEMY_ADMIN_PASSWORD`
- `ACADEMY_COOKIE_SECURE=1`
- SMTP settings if email is required

Never commit `.env` to source control.

## 3. Start

```bash
chmod +x scripts/*.sh
docker compose up -d --build
curl -fsS http://127.0.0.1:8000/health
```

## 4. HTTPS

The included Nginx container is a reverse proxy template. For production TLS, terminate HTTPS at Nginx with your certificate files, or use the included Caddyfile with a Caddy container/service.

Do not leave the application reachable through an unrelated public port once your real reverse proxy is in front of it.

## 5. First login

Open `/login` and use the admin password from `ACADEMY_ADMIN_PASSWORD`. Immediately change the admin password from the account profile after adding a profile page for admin if desired; the initial password is only a bootstrap credential.

Create Instructor accounts from Admin → Staff & access.

## 6. First batch

1. Configure academy name, course title and contact email.
2. Upload final academy logo.
3. Import students from CSV.
4. Send invitations.
5. Publish Day 01 before sharing the link.
6. Add video/resources/assignment/quiz content.
7. Verify certificate branding and `/verify`.
8. Confirm `/health` is healthy.
9. Take a backup.

## 7. Backup

Use the bundled backup scripts on a schedule. Keep at least one backup outside the VPS.

```bash
./scripts/backup_all.sh
```

For a restore, first test on an isolated PostgreSQL instance before replacing production data.

## 8. Roles

- `admin`: platform-wide management, users/staff, settings, audit and operations.
- `instructor`: lessons, resources, assignments, quizzes, analytics and submission review.
- `student`: learning, submissions, quizzes, notifications and certificates.

## 9. Production checklist

- DNS resolves to the server
- HTTPS certificate is valid
- `ACADEMY_COOKIE_SECURE=1`
- `ACADEMY_BASE_URL` uses HTTPS
- Trusted host list contains only the real hostnames
- Strong database/admin/session secrets are set
- SMTP test completed
- Backup completed and restore tested
- First student invitation completed successfully
