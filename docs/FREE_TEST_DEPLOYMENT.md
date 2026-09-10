# Free Public Test Deployment

KCI Academy can be trialled publicly on Render without buying a domain. Render provides a free Python web service and a free Postgres database, but the free database expires after 30 days and the free web service spins down after 15 minutes of inactivity. Free web services also cannot send outbound SMTP on ports 25/465/587. Do not use this configuration for production student data.

## Prerequisites

1. Create a Render account.
2. Put this project in a Git repository.
3. In Render, create a Blueprint from the repository root. Render will read `render.yaml`.

## Required values during setup

- `ACADEMY_BASE_URL`: after Render creates the web service, set this to the generated `https://<service>.onrender.com` URL.
- `ACADEMY_ADMIN_PASSWORD`: choose a strong admin password.
- SMTP values can be left blank during the free test. Invitation/reset links can still be surfaced inside the app; email delivery should be enabled later with a production mail provider or an HTTPS email API.

## First login

The default admin username is `admin`. The password is the value supplied in `ACADEMY_ADMIN_PASSWORD` during the first database initialization.

## Important free-tier limitations

- Free Postgres: 1 GB and expires after 30 days.
- Free web service: sleeps after 15 minutes idle and wakes on the next request.
- Local filesystem is ephemeral, so uploaded files are not durable on the free web service.
- Do not store irreplaceable course uploads in the free service filesystem.

## Recommended test data

Start with 2-5 students, a few lessons, one quiz, one assignment, and a test certificate. Once the workflows are verified, move to persistent production infrastructure before onboarding a real batch.
