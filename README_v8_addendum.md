# v8 additions

## Password recovery
- `/forgot-password` generates a 30-minute, single-use reset link.
- In this self-hosted build, the link is intentionally shown rather than emailed; connect an SMTP/provider later if desired.
- Admins can generate a per-student reset link from Control Center.

## Certificate verification
- Public verification endpoint: `/verify?certificate_id=...`
- Certificates include a verification link and can be upgraded to a QR code at the branding/deployment stage.

## Reminder hook
- Admin Control Center includes a reminder action for students inactive for 3+ days.
- `scripts/reminder_hook.sh` can be triggered by cron/systemd/your scheduler after supplying an authenticated service session.
