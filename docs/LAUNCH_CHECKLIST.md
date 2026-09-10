# Embedded Academy — Launch Checklist

## Before the first student
- [ ] Copy `.env.example` to `.env`.
- [ ] Set a long random `ACADEMY_SECRET`.
- [ ] Set a strong `ACADEMY_ADMIN_PASSWORD`.
- [ ] Set `ACADEMY_BASE_URL` to the real HTTPS domain.
- [ ] Add the domain to `ACADEMY_TRUSTED_HOSTS`.
- [ ] Configure PostgreSQL credentials.
- [ ] Configure SMTP if invitation/reset emails are desired.
- [ ] Run `python3 scripts/self_check.py`.
- [ ] Run `docker compose up -d`.
- [ ] Verify `/health`.
- [ ] Log in as admin and change the default academy settings.
- [ ] Publish Day 01 only for the first batch.
- [ ] Import students from CSV and send invitations.
- [ ] Confirm a student can complete first-login password setup.
- [ ] Test one lesson, one quiz, one assignment and one certificate verification.

## Ongoing operations
- [ ] Schedule `scripts/backup_all.sh`.
- [ ] Review `/admin/audit`.
- [ ] Export student/progress CSV periodically.
- [ ] Keep PostgreSQL backups and upload backups on separate storage.
