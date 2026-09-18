from __future__ import annotations

import csv
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import smtplib
import logging
import uuid
from email.message import EmailMessage
from io import BytesIO
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

from fastapi import BackgroundTasks, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:
    psycopg = None
    dict_row = None

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "data" / "academy.db"
UPLOADS = BASE / "uploads"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))
DB.parent.mkdir(parents=True, exist_ok=True)
UPLOADS.mkdir(parents=True, exist_ok=True)

SECRET = os.getenv("ACADEMY_SECRET", "dev-only-change-me")
ENV = os.getenv("ACADEMY_ENV", "development").lower()
COOKIE_SECURE = os.getenv("ACADEMY_COOKIE_SECURE", "0") == "1"
TRUSTED_HOSTS = [h.strip() for h in os.getenv("ACADEMY_TRUSTED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
if ENV == "production" and SECRET == "dev-only-change-me":
    raise RuntimeError("ACADEMY_SECRET must be set in production")
MAX_UPLOAD = 20 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".ppt", ".pptx", ".doc", ".docx", ".txt", ".zip", ".ino", ".c", ".h", ".cpp", ".py", ".jpg", ".jpeg", ".png"}
ALLOWED_SUBMISSION_EXTENSIONS = ALLOWED_EXTENSIONS
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_ATTEMPTS = 6
SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587") or 587)
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "").strip()
SMTP_TLS = os.getenv("SMTP_TLS", "1") == "1"
PUBLIC_BASE_URL = os.getenv("ACADEMY_BASE_URL", "http://localhost:8000").rstrip("/")

def client_key(request: Request, username: str = ""):
    host = request.client.host if request.client else "unknown"
    return f"{host}:{username.strip().lower()}"

SCHEMA_VERSION = 6
logger = logging.getLogger("embedded_academy")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
app = FastAPI(title="KCI Academy", version="20.0")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=TRUSTED_HOSTS)
app.add_middleware(SessionMiddleware, secret_key=SECRET, max_age=60 * 60 * 24 * 7, same_site="lax", https_only=COOKIE_SECURE)
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.filters["fromjson"] = lambda value: json.loads(value)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response


app.add_middleware(SecurityHeaders)

@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("Unhandled request error", extra={"path": request.url.path, "request_id": request_id})
        raise
    response.headers["X-Request-ID"] = request_id
    if ENV == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

@app.exception_handler(404)
async def not_found(request: Request, exc):
    return templates.TemplateResponse("error.html", {"request": request, "user": current(request), "academy": settings(), "status_code": 404, "message": "The page you requested could not be found."}, status_code=404)

@app.exception_handler(500)
async def server_error(request: Request, exc):
    logger.exception("Internal server error")
    return templates.TemplateResponse("error.html", {"request": request, "user": current(request), "academy": settings(), "status_code": 500, "message": "Something went wrong on the academy server."}, status_code=500)


class CompatRow(dict):
    """PostgreSQL dict row that also supports SQLite-style integer indexing."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)

def compat_dict_row(cursor):
    factory = dict_row(cursor)
    def make_row(values):
        return CompatRow(factory(values))
    return make_row

class Database:
    def __init__(self, conn, postgres=False):
        self.conn = conn
        self.postgres = postgres
    def _sql(self, sql):
        return sql.replace("?", "%s") if self.postgres else sql
    def execute(self, sql, params=()):
        return self.conn.execute(self._sql(sql), params)
    def executemany(self, sql, params):
        return self.conn.executemany(self._sql(sql), params)
    def executescript(self, script):
        if not self.postgres:
            return self.conn.executescript(script)
        for statement in [x.strip() for x in script.split(';') if x.strip()]:
            statement = statement.replace("id INTEGER PRIMARY KEY", "id BIGSERIAL PRIMARY KEY")
            self.conn.execute(self._sql(statement))
    def commit(self):
        self.conn.commit()
    def close(self):
        self.conn.close()


def db():
    if USE_POSTGRES:
        if psycopg is None:
            raise RuntimeError("PostgreSQL mode requires psycopg[binary]")
        return Database(psycopg.connect(DATABASE_URL, row_factory=compat_dict_row), True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return Database(c)


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 240_000)
    return f"pbkdf2_sha256$240000${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        method, rounds, salt_hex, digest_hex = stored.split("$")
        if method != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds)).hex()
        return hmac.compare_digest(candidate, digest_hex)
    except Exception:
        return False


def init():
    c = db()
    schema = """
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        name TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'student',
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_login TEXT,
        must_change_password INTEGER NOT NULL DEFAULT 0,
        avatar_color TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS lessons(
        id INTEGER PRIMARY KEY,
        day INTEGER UNIQUE NOT NULL CHECK(day BETWEEN 1 AND 70),
        title TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'Embedded Systems',
        description TEXT NOT NULL DEFAULT '',
        video_url TEXT NOT NULL DEFAULT '',
        published INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS progress(
        user_id INTEGER NOT NULL,
        lesson_id INTEGER NOT NULL,
        completed INTEGER NOT NULL DEFAULT 0,
        completed_at TEXT,
        PRIMARY KEY(user_id, lesson_id),
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY(lesson_id) REFERENCES lessons(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS files(
        id INTEGER PRIMARY KEY,
        lesson_id INTEGER NOT NULL,
        filename TEXT NOT NULL,
        stored TEXT NOT NULL,
        size INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(lesson_id) REFERENCES lessons(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS announcements(
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        published INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS assignments(
        id INTEGER PRIMARY KEY,
        lesson_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        instructions TEXT NOT NULL,
        due_text TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(lesson_id) REFERENCES lessons(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS submissions(
        id INTEGER PRIMARY KEY,
        assignment_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        filename TEXT,
        stored TEXT,
        note TEXT NOT NULL DEFAULT '',
        submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(assignment_id, user_id),
        FOREIGN KEY(assignment_id) REFERENCES assignments(id) ON DELETE CASCADE,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS quizzes(
        id INTEGER PRIMARY KEY,
        lesson_id INTEGER UNIQUE NOT NULL,
        title TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(lesson_id) REFERENCES lessons(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS quiz_questions(
        id INTEGER PRIMARY KEY,
        quiz_id INTEGER NOT NULL,
        question TEXT NOT NULL,
        options_json TEXT NOT NULL,
        correct_index INTEGER NOT NULL CHECK(correct_index BETWEEN 0 AND 3),
        FOREIGN KEY(quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS quiz_attempts(
        id INTEGER PRIMARY KEY,
        quiz_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        score INTEGER NOT NULL,
        total INTEGER NOT NULL,
        submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(quiz_id, user_id),
        FOREIGN KEY(quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS password_resets(
        id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, token_hash TEXT UNIQUE NOT NULL,
        expires_at TEXT NOT NULL, used_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS certificates(
        id INTEGER PRIMARY KEY, user_id INTEGER UNIQUE NOT NULL, certificate_id TEXT UNIQUE NOT NULL, issued_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS audit_log(
        id INTEGER PRIMARY KEY, user_id INTEGER, action TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
    );
    CREATE TABLE IF NOT EXISTS notifications(
        id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, link TEXT NOT NULL DEFAULT '', read_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    """
    c.executescript(schema)
    try:
        if c.postgres:
            cols = c.execute("SELECT column_name FROM information_schema.columns WHERE table_name='users'").fetchall()
            if not any((row.get("column_name") if isinstance(row, dict) else row[0]) == "email" for row in cols):
                c.execute("ALTER TABLE users ADD COLUMN email TEXT")
        else:
            cols = c.execute("PRAGMA table_info(users)").fetchall()
            if not any(col[1] == "email" for col in cols):
                c.execute("ALTER TABLE users ADD COLUMN email TEXT")
    except Exception:
        pass
    if not c.execute("SELECT 1 FROM settings WHERE key='academy_name'").fetchone():
        c.execute("INSERT INTO settings(key,value) VALUES('academy_name',?)", ("KCI Academy",))
    else:
        # One-time cleanup for databases created before the KCI rebrand.
        current_academy = c.execute("SELECT value FROM settings WHERE key='academy_name'").fetchone()
        current_name = current_academy.get("value") if isinstance(current_academy, dict) else current_academy[0]
        if current_name == "Embedded Academy":
            c.execute("UPDATE settings SET value=? WHERE key='academy_name'", ("KCI Academy",))
        current_prefix = c.execute("SELECT value FROM settings WHERE key='certificate_prefix'").fetchone()
        prefix_value = current_prefix.get("value") if isinstance(current_prefix, dict) else current_prefix[0]
        if prefix_value == "EA-2026":
            c.execute("UPDATE settings SET value=? WHERE key='certificate_prefix'", ("KCI-2026",))
    if not c.execute("SELECT 1 FROM settings WHERE key='course_name'").fetchone():
        c.execute("INSERT INTO settings(key,value) VALUES('course_name',?)", ("70 Days Embedded Systems Course · 2026",))
    defaults = {"academy_tagline":"Learn embedded systems. Build real things.","certificate_enabled":"1","certificate_prefix":"KCI-2026","certificate_requirements":"All published lessons completed","contact_email":"","logo_stored":"","brand_accent":"#70f0c6"}
    for key, value in defaults.items():
        if not c.execute("SELECT 1 FROM settings WHERE key=?", (key,)).fetchone(): c.execute("INSERT INTO settings(key,value) VALUES(?,?)", (key,value))
    if not c.execute("SELECT 1 FROM users WHERE username='admin'").fetchone():
        c.execute("INSERT INTO users(username,password,name,role) VALUES(?,?,?,?)", ("admin", hash_password(os.getenv("ACADEMY_ADMIN_PASSWORD", "admin123")), "Academy Admin", "admin"))
    if not c.execute("SELECT 1 FROM users WHERE username='student01'").fetchone():
        c.execute("INSERT INTO users(username,password,name) VALUES(?,?,?)", ("student01", hash_password(os.getenv("ACADEMY_DEMO_PASSWORD", "student123")), "Demo Student"))
    if c.execute("SELECT COUNT(*) FROM lessons").fetchone()[0] == 0:
        categories = [(1, 20, "Embedded C"), (21, 40, "Microcontrollers"), (41, 55, "RTOS & Peripherals"), (56, 70, "IoT & Projects")]
        for d in range(1, 71):
            cat = next(name for start, end, name in categories if start <= d <= end)
            c.execute("INSERT INTO lessons(day,title,category,description,published) VALUES(?,?,?,?,0)", (d, f"Day {d:02d} — Embedded Systems Lesson", cat, "Lecture content, practical examples, notes and source code."))
    if not c.execute("SELECT 1 FROM announcements").fetchone():
        c.execute("INSERT INTO announcements(title,body) VALUES(?,?)", ("Welcome to KCI Academy", "Your 70-day learning journey starts here. Check the dashboard for the next published lesson."))
    apply_migrations(c)
    c.commit(); c.close()


def apply_migrations(c):
    """Small, idempotent migration runner for upgrades from earlier academy builds."""
    c.execute("CREATE TABLE IF NOT EXISTS migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    row = c.execute("SELECT MAX(version) AS version FROM migrations").fetchone()
    current = int(row["version"] or 0) if row else 0
    if current < 1:
        c.execute("CREATE INDEX IF NOT EXISTS idx_progress_user_completed ON progress(user_id, completed)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_notifications_user_read ON notifications(user_id, read_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at)")
        c.execute("INSERT INTO migrations(version,applied_at) VALUES(?,?)", (1, now()))
        current = 1
    if current < 2:
        c.execute("CREATE INDEX IF NOT EXISTS idx_password_resets_user_used ON password_resets(user_id, used_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_certificates_code ON certificates(certificate_id)")
        c.execute("INSERT INTO migrations(version,applied_at) VALUES(?,?)", (2, now()))
        current = 2
    if current < 3:
        try:
            if c.postgres:
                cols = c.execute("SELECT column_name FROM information_schema.columns WHERE table_name='submissions'").fetchall()
                names = {row['column_name'] if isinstance(row, dict) else row[0] for row in cols}
            else:
                names = {col[1] for col in c.execute("PRAGMA table_info(submissions)").fetchall()}
            if 'review_status' not in names: c.execute("ALTER TABLE submissions ADD COLUMN review_status TEXT NOT NULL DEFAULT 'Pending'")
            if 'review_note' not in names: c.execute("ALTER TABLE submissions ADD COLUMN review_note TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        c.execute("CREATE INDEX IF NOT EXISTS idx_submissions_review ON submissions(review_status, submitted_at)")
        c.execute("INSERT INTO migrations(version,applied_at) VALUES(?,?)", (3, now()))
        current = 3
    if current < 4:
        c.execute("CREATE INDEX IF NOT EXISTS idx_lessons_published_day ON lessons(published, day)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_assignments_lesson_active ON assignments(lesson_id, active)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_quiz_attempts_user ON quiz_attempts(user_id, submitted_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_notifications_created ON notifications(user_id, created_at)")
        c.execute("INSERT INTO migrations(version,applied_at) VALUES(?,?)", (4, now()))
    if current < 5:
        try:
            if c.postgres:
                cols = c.execute("SELECT column_name FROM information_schema.columns WHERE table_name='users'").fetchall()
                names = {row['column_name'] if isinstance(row, dict) else row[0] for row in cols}
            else:
                names = {col[1] for col in c.execute("PRAGMA table_info(users)").fetchall()}
            if 'must_change_password' not in names:
                c.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        c.execute("CREATE INDEX IF NOT EXISTS idx_users_password_change ON users(role, must_change_password, active)")
        c.execute("INSERT INTO migrations(version,applied_at) VALUES(?,?)", (5, now()))
        current = 5
    if current < 6:
        try:
            if c.postgres:
                cols = c.execute("SELECT column_name FROM information_schema.columns WHERE table_name='users'").fetchall()
                names = {row['column_name'] if isinstance(row, dict) else row[0] for row in cols}
            else:
                names = {col[1] for col in c.execute("PRAGMA table_info(users)").fetchall()}
            if 'avatar_color' not in names:
                c.execute("ALTER TABLE users ADD COLUMN avatar_color TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        c.execute("CREATE INDEX IF NOT EXISTS idx_users_role_active ON users(role, active)")
        c.execute("INSERT INTO migrations(version,applied_at) VALUES(?,?)", (6, now()))
        current = 6


init()


def current(request: Request):
    uid = request.session.get("uid")
    if not uid: return None
    c = db(); u = c.execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone(); c.close()
    return u


def auth(request: Request, role: str | None = None):
    u = current(request)
    return u if u and (role is None or u["role"] == role) else None

def staff(request: Request):
    u = current(request)
    return u if u and u["role"] in {"admin", "instructor"} else None

def admin_only(request: Request):
    return auth(request, "admin")


def csrf(request: Request):
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32); request.session["csrf"] = token
    return token


def check_csrf(request: Request, token: str):
    expected = request.session.get("csrf", "")
    return bool(token and expected and secrets.compare_digest(token, expected))


def settings():
    c = db(); rows = c.execute("SELECT key,value FROM settings").fetchall(); c.close(); return {r["key"]: r["value"] for r in rows}


def page(request: Request, name: str, **context):
    context.update(request=request, user=current(request), csrf_token=csrf(request), academy=settings(), flash=pull_flash(request), PUBLIC_BASE_URL=PUBLIC_BASE_URL)
    return templates.TemplateResponse(name, context)


def redirect(path: str): return RedirectResponse(path, 303)

def flash(request: Request, message: str, kind: str = "info"):
    request.session["flash"] = {"message": message, "kind": kind}

def pull_flash(request: Request):
    return request.session.pop("flash", None)

def audit(user_id, action, target="", detail=""):
    c=db(); c.execute("INSERT INTO audit_log(user_id,action,target,detail,created_at) VALUES(?,?,?,?,?)",(user_id,action,target,detail[:1000],now())); c.commit(); c.close()


def certificate_code(user_id: int) -> str:
    return f"{settings().get('certificate_prefix','EA-2026')}-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{user_id:04d}"


def valid_video_url(value: str):
    if not value: return True
    p = urlparse(value.strip())
    return p.scheme in {"http", "https"} and bool(p.netloc)


def safe_username(value: str): return bool(re.fullmatch(r"[A-Za-z0-9_.-]{3,40}", value.strip()))


def safe_filename(value: str, allowed=ALLOWED_EXTENSIONS):
    name = Path(value or "resource").name.replace(" ", "_")
    ext = Path(name).suffix.lower()
    return name if name and ext in allowed else None


def bounded_read(upload: UploadFile):
    data = upload.file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD: raise ValueError("File exceeds 20 MB limit")
    return data

def absolute_url(path: str):
    return PUBLIC_BASE_URL + (path if path.startswith("/") else "/" + path)

def email_ready():
    return bool(SMTP_HOST and SMTP_FROM)

def send_email(to: str, subject: str, body: str):
    if not email_ready():
        logger.error("SMTP email not configured: SMTP_HOST or SMTP_FROM is missing")
        return False, "Email delivery is not configured."
    if not to:
        logger.error("SMTP email not sent: recipient address is missing")
        return False, "Recipient email is missing."
    logger.info("SMTP email attempt: recipient=%s subject=%s host=%s port=%s tls=%s", to, subject, SMTP_HOST, SMTP_PORT, SMTP_TLS)
    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            logger.info("SMTP connection established: host=%s port=%s", SMTP_HOST, SMTP_PORT)
            if SMTP_TLS:
                server.starttls()
                logger.info("SMTP STARTTLS completed")
            if SMTP_USER:
                server.login(SMTP_USER, SMTP_PASSWORD)
                logger.info("SMTP authentication successful: user=%s", SMTP_USER)
            server.send_message(msg)
        logger.info("SMTP email sent successfully: recipient=%s subject=%s", to, subject)
        return True, "Email sent."
    except Exception as exc:
        logger.exception("SMTP email failed: recipient=%s subject=%s error=%s", to, subject, exc)
        return False, f"Email delivery failed: {exc}"

def queue_email(background_tasks: BackgroundTasks, to: str, subject: str, body: str):
    if email_ready() and to:
        background_tasks.add_task(send_email, to, subject, body)
        logger.info("SMTP email queued: recipient=%s subject=%s", to, subject)
        return True
    logger.warning("SMTP email not queued: email_ready=%s recipient_present=%s", email_ready(), bool(to))
    return False


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    c = db()
    counts = {"published": c.execute("SELECT COUNT(*) FROM lessons WHERE published=1").fetchone()[0], "students": c.execute("SELECT COUNT(*) FROM users WHERE role='student'").fetchone()[0]}
    stats = {"assignments": c.execute("SELECT COUNT(*) FROM assignments WHERE active=1").fetchone()[0], "submissions": c.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]}
    c.close(); return page(request, "home.html", counts=counts, stats=stats)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request): return page(request, "login.html", error=None)


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, username: str = Form(...), password: str = Form(...), csrf_token: str = Form(...)):
    if not check_csrf(request, csrf_token): return page(request, "login.html", error="Your session expired. Please try again.")
    key = client_key(request, username)
    attempts = request.session.get("login_attempts", {})
    entry = attempts.get(key, {"count": 0, "first": datetime.now(timezone.utc).timestamp()})
    now_ts = datetime.now(timezone.utc).timestamp()
    if now_ts - entry["first"] > LOGIN_WINDOW_SECONDS:
        entry = {"count": 0, "first": now_ts}
    if entry["count"] >= LOGIN_MAX_ATTEMPTS:
        return page(request, "login.html", error="Too many failed attempts. Please wait 15 minutes and try again.")
    c = db(); u = c.execute("SELECT * FROM users WHERE username=? AND active=1", (username.strip(),)).fetchone()
    if not u or not verify_password(password, u["password"]):
        c.close(); entry["count"] += 1; attempts[key] = entry; request.session["login_attempts"] = attempts
        return page(request, "login.html", error="Invalid username or password.")
    c.execute("UPDATE users SET last_login=? WHERE id=?", (now(), u["id"])); c.commit(); c.close()
    attempts.pop(key, None); request.session["login_attempts"] = attempts
    audit(u["id"], "login", "auth")
    request.session.clear(); request.session["uid"] = u["id"]; request.session["csrf"] = secrets.token_urlsafe(32)
    if u["role"] in {"admin", "instructor"}: return redirect("/admin")
    return redirect("/profile?first=1" if u["must_change_password"] else "/dashboard")


@app.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request):
    return page(request, "forgot_password.html", message=None, reset_link=None)

@app.post("/forgot-password", response_class=HTMLResponse)
def forgot_password(request: Request, background_tasks: BackgroundTasks, username: str = Form(...), csrf_token: str = Form(...)):
    if not check_csrf(request, csrf_token): return page(request, "forgot_password.html", message="Your session expired. Please try again.", reset_link=None)
    c=db(); u=c.execute("SELECT * FROM users WHERE username=? AND active=1",(username.strip(),)).fetchone()
    if not u:
        c.close(); return page(request,"forgot_password.html",message="If that account exists, a reset link has been prepared.",reset_link=None)
    raw=secrets.token_urlsafe(32); token_hash=hashlib.sha256(raw.encode()).hexdigest(); expires=(datetime.now(timezone.utc)+timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S UTC")
    c.execute("UPDATE password_resets SET used_at=? WHERE user_id=? AND used_at IS NULL",(now(),u["id"]))
    c.execute("INSERT INTO password_resets(user_id,token_hash,expires_at) VALUES(?,?,?)",(u["id"],token_hash,expires)); c.commit(); c.close();
    link=absolute_url(f"/reset-password?token={raw}")
    sent = queue_email(background_tasks, u["email"] if "email" in u.keys() else "", "Reset your KCI Academy password", f"Hello {u['name']},\n\nUse this link to reset your password (valid for 30 minutes):\n{link}\n\nIf you did not request this, you can ignore this email.")
    message = "A password reset link has been emailed to your registered address." if sent else "Reset link generated. Email delivery is not configured, so the link is shown below for secure sharing."
    return page(request,"forgot_password.html",message=message,reset_link=None if sent else link)

@app.get("/reset-password", response_class=HTMLResponse)
def reset_password_page(request: Request, token: str = ""):
    return page(request,"reset_password.html",token=token,error=None,saved=False)

@app.post("/reset-password", response_class=HTMLResponse)
def reset_password(request: Request, token: str = Form(...), password: str = Form(...), password2: str = Form(...), csrf_token: str = Form(...)):
    if not check_csrf(request, csrf_token): return page(request,"reset_password.html",token=token,error="Your session expired.",saved=False)
    if len(password)<8 or password!=password2: return page(request,"reset_password.html",token=token,error="Passwords must match and be at least 8 characters.",saved=False)
    token_hash=hashlib.sha256(token.encode()).hexdigest(); c=db(); r=c.execute("SELECT * FROM password_resets WHERE token_hash=? AND used_at IS NULL",(token_hash,)).fetchone()
    if not r: c.close(); return page(request,"reset_password.html",token=token,error="This reset link is invalid or already used.",saved=False)
    try:
        exp=datetime.strptime(r["expires_at"], "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
        if exp < datetime.now(timezone.utc): raise ValueError
    except ValueError:
        c.close(); return page(request,"reset_password.html",token=token,error="This reset link has expired.",saved=False)
    c.execute("UPDATE users SET password=? WHERE id=?",(hash_password(password),r["user_id"])); c.execute("UPDATE password_resets SET used_at=? WHERE id=?",(now(),r["id"])); c.commit(); c.close(); audit(r["user_id"],"password_reset","account")
    return page(request,"reset_password.html",token="",error=None,saved=True)

@app.get("/logout")
def logout(request: Request): request.session.clear(); return RedirectResponse("/", 303)


@app.get("/course", response_class=HTMLResponse)
def course_overview(request: Request):
    c=db(); lessons=c.execute("SELECT day,title,category,description,published FROM lessons ORDER BY day").fetchall(); published=sum(1 for l in lessons if l["published"]); categories=[]
    seen=set()
    for l in lessons:
        if l["category"] not in seen:
            seen.add(l["category"]); categories.append(l["category"])
    c.close(); return page(request,"course.html",lessons=lessons,published=published,categories=categories)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    u = auth(request, "student")
    if not u: return RedirectResponse("/login", 303)
    c = db(); lessons = c.execute("SELECT l.*, COALESCE(p.completed,0) completed, p.completed_at FROM lessons l LEFT JOIN progress p ON p.lesson_id=l.id AND p.user_id=? WHERE l.published=1 ORDER BY l.day", (u["id"],)).fetchall()
    done = sum(x["completed"] for x in lessons); total = len(lessons); current_lesson = next((x for x in lessons if not x["completed"]), None)
    announcements = c.execute("SELECT * FROM announcements WHERE published=1 ORDER BY id DESC LIMIT 4").fetchall()
    assignment_count = c.execute("SELECT COUNT(*) FROM assignments a JOIN lessons l ON l.id=a.lesson_id LEFT JOIN submissions s ON s.assignment_id=a.id AND s.user_id=? WHERE a.active=1 AND l.published=1 AND s.id IS NULL", (u["id"],)).fetchone()[0]
    quiz_count = c.execute("SELECT COUNT(*) FROM quizzes q JOIN lessons l ON l.id=q.lesson_id LEFT JOIN quiz_attempts qa ON qa.quiz_id=q.id AND qa.user_id=? WHERE q.active=1 AND l.published=1 AND qa.id IS NULL", (u["id"],)).fetchone()[0]
    quiz_stats = c.execute("SELECT COUNT(*) total, COALESCE(SUM(CASE WHEN qa.score * 100.0 / NULLIF(qa.total,0) >= 70 THEN 1 ELSE 0 END),0) passed FROM quizzes q JOIN lessons l ON l.id=q.lesson_id LEFT JOIN quiz_attempts qa ON qa.quiz_id=q.id AND qa.user_id=? WHERE q.active=1 AND l.published=1", (u["id"],)).fetchone()
    cert = c.execute("SELECT * FROM certificates WHERE user_id=?", (u["id"],)).fetchone(); c.close(); pct = round(done / total * 100) if total else 0
    cert_eligible = bool(total and done >= total and quiz_stats["passed"] >= quiz_stats["total"])
    return page(request, "dashboard.html", lessons=lessons, done=done, total=total, pct=pct, current_lesson=current_lesson, announcements=announcements, assignment_count=assignment_count, quiz_count=quiz_count, cert=cert, cert_eligible=cert_eligible)


@app.get("/lesson/{day}", response_class=HTMLResponse)
def lesson(request: Request, day: int):
    u = auth(request, "student")
    if not u: return RedirectResponse("/login", 303)
    c = db(); lesson = c.execute("SELECT l.*, COALESCE(p.completed,0) completed FROM lessons l LEFT JOIN progress p ON p.lesson_id=l.id AND p.user_id=? WHERE l.day=? AND l.published=1", (u["id"], day)).fetchone()
    if not lesson: c.close(); return RedirectResponse("/dashboard", 303)
    files = c.execute("SELECT * FROM files WHERE lesson_id=? ORDER BY id DESC", (lesson["id"],)).fetchall()
    assignments = c.execute("""SELECT a.*, s.id submission_id, s.filename submission_filename, s.note submission_note, s.submitted_at
      FROM assignments a LEFT JOIN submissions s ON s.assignment_id=a.id AND s.user_id=? WHERE a.lesson_id=? AND a.active=1 ORDER BY a.id DESC""", (u["id"], lesson["id"])).fetchall()
    quiz = c.execute("SELECT * FROM quizzes WHERE lesson_id=? AND active=1", (lesson["id"],)).fetchone()
    attempt = c.execute("SELECT * FROM quiz_attempts WHERE quiz_id=? AND user_id=?", (quiz["id"], u["id"])) .fetchone() if quiz else None
    prev = c.execute("SELECT day FROM lessons WHERE day<? AND published=1 ORDER BY day DESC LIMIT 1", (day,)).fetchone()
    nxt = c.execute("SELECT day FROM lessons WHERE day>? AND published=1 ORDER BY day LIMIT 1", (day,)).fetchone()
    c.close(); return page(request, "lesson.html", lesson=lesson, files=files, assignments=assignments, quiz=quiz, attempt=attempt, prev_day=prev["day"] if prev else None, next_day=nxt["day"] if nxt else None)


@app.post("/lesson/{day}/complete")
def complete(request: Request, day: int, csrf_token: str = Form(...)):
    u = auth(request, "student")
    if not u or not check_csrf(request, csrf_token): return RedirectResponse("/login", 303)
    c = db(); l = c.execute("SELECT id FROM lessons WHERE day=? AND published=1", (day,)).fetchone()
    if l:
        c.execute("INSERT INTO progress(user_id,lesson_id,completed,completed_at) VALUES(?,?,1,?) ON CONFLICT(user_id,lesson_id) DO UPDATE SET completed=1, completed_at=excluded.completed_at", (u["id"], l["id"], now())); c.commit()
    c.close(); audit(u["id"], "lesson_completed", f"day:{day}"); return redirect(f"/lesson/{day}")


@app.get("/files/{file_id}")
def download_resource(request: Request, file_id: int):
    u = auth(request, "student") or auth(request, "admin")
    if not u: return RedirectResponse("/login", 303)
    c = db(); row = c.execute("SELECT f.*, l.published FROM files f JOIN lessons l ON l.id=f.lesson_id WHERE f.id=?", (file_id,)).fetchone(); c.close()
    if not row or (u["role"] == "student" and not row["published"]): return RedirectResponse("/dashboard" if u["role"] == "student" else "/admin", 303)
    path = UPLOADS / row["stored"]
    return FileResponse(path, filename=row["filename"]) if path.exists() else RedirectResponse("/dashboard", 303)


@app.get("/assignments", response_class=HTMLResponse)
def assignments(request: Request):
    u = auth(request, "student")
    if not u: return RedirectResponse("/login", 303)
    c = db(); rows = c.execute("""SELECT a.*, l.day, s.submitted_at FROM assignments a JOIN lessons l ON l.id=a.lesson_id
        LEFT JOIN submissions s ON s.assignment_id=a.id AND s.user_id=? WHERE a.active=1 AND l.published=1 ORDER BY l.day, a.id""", (u["id"],)).fetchall(); c.close()
    return page(request, "assignments.html", assignments=rows)


@app.post("/assignment/{assignment_id}/submit")
def submit_assignment(request: Request, assignment_id: int, csrf_token: str = Form(...), file: UploadFile | None = File(None), note: str = Form("")):
    u = auth(request, "student")
    if not u or not check_csrf(request, csrf_token): return RedirectResponse("/login", 303)
    c = db(); a = c.execute("SELECT a.id,l.day,l.published FROM assignments a JOIN lessons l ON l.id=a.lesson_id WHERE a.id=? AND a.active=1", (assignment_id,)).fetchone()
    if not a or not a["published"]: c.close(); return RedirectResponse("/dashboard", 303)
    filename = stored = None
    try:
        if file and file.filename:
            filename = safe_filename(file.filename, ALLOWED_SUBMISSION_EXTENSIONS)
            if not filename: raise ValueError("Unsupported file type")
            data = bounded_read(file); stored = f"submission_{u['id']}_{assignment_id}_{secrets.token_hex(8)}{Path(filename).suffix.lower()}"; (UPLOADS / stored).write_bytes(data)
        c.execute("INSERT INTO submissions(assignment_id,user_id,filename,stored,note,submitted_at) VALUES(?,?,?,?,?,?) ON CONFLICT(assignment_id,user_id) DO UPDATE SET filename=excluded.filename, stored=excluded.stored, note=excluded.note, submitted_at=excluded.submitted_at", (assignment_id, u["id"], filename, stored, note[:2000], now())); c.commit()
    except ValueError:
        pass
    finally: c.close()
    return redirect(f"/lesson/{a['day']}")


@app.get("/quiz/{quiz_id}", response_class=HTMLResponse)
def quiz_page(request: Request, quiz_id: int):
    u = auth(request, "student")
    if not u: return RedirectResponse("/login", 303)
    c = db(); q = c.execute("SELECT q.*, l.day, l.title lesson_title FROM quizzes q JOIN lessons l ON l.id=q.lesson_id WHERE q.id=? AND q.active=1 AND l.published=1", (quiz_id,)).fetchone()
    if not q: c.close(); return RedirectResponse("/dashboard", 303)
    questions = c.execute("SELECT * FROM quiz_questions WHERE quiz_id=? ORDER BY id", (quiz_id,)).fetchall(); attempt = c.execute("SELECT * FROM quiz_attempts WHERE quiz_id=? AND user_id=?", (quiz_id, u["id"])).fetchone(); c.close()
    return page(request, "quiz.html", quiz=q, questions=questions, attempt=attempt)


@app.post("/quiz/{quiz_id}/submit")
async def quiz_submit(request: Request, quiz_id: int):
    u = auth(request, "student"); form = await request.form(); token = str(form.get("csrf_token", ""))
    if not u or not check_csrf(request, token): return RedirectResponse("/login", 303)
    c = db(); quiz = c.execute("SELECT * FROM quizzes WHERE id=? AND active=1", (quiz_id,)).fetchone(); qs = c.execute("SELECT * FROM quiz_questions WHERE quiz_id=? ORDER BY id", (quiz_id,)).fetchall()
    if not quiz or not qs: c.close(); return RedirectResponse("/dashboard", 303)
    score = 0
    for q in qs:
        try: selected = int(form.get(f"q_{q['id']}", -1))
        except Exception: selected = -1
        if selected == q["correct_index"]: score += 1
    c.execute("INSERT INTO quiz_attempts(quiz_id,user_id,score,total,submitted_at) VALUES(?,?,?,?,?) ON CONFLICT(quiz_id,user_id) DO UPDATE SET score=excluded.score,total=excluded.total,submitted_at=excluded.submitted_at", (quiz_id, u["id"], score, len(qs), now())); c.commit(); c.close(); audit(u["id"], "quiz_submitted", f"quiz:{quiz_id}", f"score={score}/{len(qs)}")
    return redirect(f"/quiz/{quiz_id}")


@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request):
    u = auth(request)
    if not u: return RedirectResponse("/login", 303)
    return page(request, "profile.html", saved=False, error=None, first=request.query_params.get("first") == "1")


@app.post("/profile", response_class=HTMLResponse)
def profile_save(request: Request, name: str = Form(...), email: str = Form(""), password: str = Form(""), password2: str = Form(""), csrf_token: str = Form(...)):
    u = auth(request)
    if not u or not check_csrf(request, csrf_token): return RedirectResponse("/login", 303)
    name = name.strip()
    if not name: return page(request, "profile.html", saved=False, error="Name is required.", first=bool(u["must_change_password"]))
    if password and (len(password) < 8 or password != password2): return page(request, "profile.html", saved=False, error="Password must be 8+ characters and match confirmation.", first=bool(u["must_change_password"]))
    c = db()
    if password:
        c.execute("UPDATE users SET name=?, email=?, password=?, must_change_password=0 WHERE id=?", (name, email.strip()[:200], hash_password(password), u["id"]))
    else:
        c.execute("UPDATE users SET name=?, email=? WHERE id=?", (name, email.strip()[:200], u["id"]))
    c.commit(); c.close(); audit(u["id"], "profile_updated", "profile", "password_changed" if password else "name_changed")
    return page(request, "profile.html", saved=True, error=None, first=False)


@app.get("/certificate", response_class=HTMLResponse)
def certificate(request: Request):
    u=auth(request,"student")
    if not u: return RedirectResponse("/login",303)
    cfg=settings(); c=db(); total=c.execute("SELECT COUNT(*) FROM lessons WHERE published=1").fetchone()[0]; done=c.execute("SELECT COUNT(*) FROM progress p JOIN lessons l ON l.id=p.lesson_id WHERE p.user_id=? AND p.completed=1 AND l.published=1",(u["id"],)).fetchone()[0]; qtotal=c.execute("SELECT COUNT(*) FROM quizzes q JOIN lessons l ON l.id=q.lesson_id WHERE q.active=1 AND l.published=1").fetchone()[0]; qpassed=c.execute("SELECT COUNT(*) FROM quizzes q JOIN lessons l ON l.id=q.lesson_id JOIN quiz_attempts qa ON qa.quiz_id=q.id AND qa.user_id=? WHERE q.active=1 AND l.published=1 AND qa.total>0 AND qa.score*100.0/qa.total>=70",(u["id"],)).fetchone()[0]; cert=c.execute("SELECT * FROM certificates WHERE user_id=?",(u["id"],)).fetchone(); eligible=cfg.get("certificate_enabled","1")=="1" and bool(total and done>=total and qpassed>=qtotal)
    if eligible and not cert:
        cid=certificate_code(u["id"]); issued=now(); c.execute("INSERT INTO certificates(user_id,certificate_id,issued_at) VALUES(?,?,?)",(u["id"],cid,issued)); c.commit(); cert=c.execute("SELECT * FROM certificates WHERE user_id=?",(u["id"],)).fetchone(); audit(u["id"],"certificate_issued",cid)
    c.close(); return page(request,"certificate.html",eligible=eligible,total=total,done=done,quiz_total=qtotal,quiz_passed=qpassed,cert=cert,requirements=cfg.get("certificate_requirements","All published lessons completed"))


@app.get("/verify", response_class=HTMLResponse)
def verify_page(request: Request, certificate_id: str = ""):
    result=None
    if certificate_id:
        c=db(); result=c.execute("SELECT c.certificate_id,c.issued_at,u.name,u.username,s.value course_name FROM certificates c JOIN users u ON u.id=c.user_id JOIN settings s ON s.key='course_name' WHERE c.certificate_id=?",(certificate_id.strip(),)).fetchone(); c.close()
    return page(request,"verify.html",certificate_id=certificate_id,result=result)

@app.get("/certificate/pdf")
def certificate_pdf(request: Request):
    u=auth(request,"student")
    if not u: return RedirectResponse("/login",303)
    cfg=settings(); c=db(); total=c.execute("SELECT COUNT(*) FROM lessons WHERE published=1").fetchone()[0]; done=c.execute("SELECT COUNT(*) FROM progress p JOIN lessons l ON l.id=p.lesson_id WHERE p.user_id=? AND p.completed=1 AND l.published=1",(u["id"],)).fetchone()[0]; qtotal=c.execute("SELECT COUNT(*) FROM quizzes q JOIN lessons l ON l.id=q.lesson_id WHERE q.active=1 AND l.published=1").fetchone()[0]; qpassed=c.execute("SELECT COUNT(*) FROM quizzes q JOIN lessons l ON l.id=q.lesson_id JOIN quiz_attempts qa ON qa.quiz_id=q.id AND qa.user_id=? WHERE q.active=1 AND l.published=1 AND qa.total>0 AND qa.score*100.0/qa.total>=70",(u["id"],)).fetchone()[0]; cert=c.execute("SELECT * FROM certificates WHERE user_id=?",(u["id"],)).fetchone(); eligible=cfg.get("certificate_enabled","1")=="1" and bool(total and done>=total and qpassed>=qtotal)
    if eligible and not cert:
        cid=certificate_code(u["id"]); c.execute("INSERT INTO certificates(user_id,certificate_id,issued_at) VALUES(?,?,?)",(u["id"],cid,now())); c.commit(); cert=c.execute("SELECT * FROM certificates WHERE user_id=?",(u["id"],)).fetchone()
    c.close()
    if not eligible: return RedirectResponse("/certificate",303)
    from reportlab.lib.pagesizes import landscape,A4
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import mm
    from reportlab.lib.colors import HexColor
    buf=BytesIO(); pdf=canvas.Canvas(buf,pagesize=landscape(A4)); w,h=landscape(A4)
    pdf.setFillColor(HexColor("#08131c")); pdf.rect(0,0,w,h,fill=1,stroke=0); pdf.setStrokeColor(HexColor("#70f0c6")); pdf.setLineWidth(2); pdf.rect(18*mm,18*mm,w-36*mm,h-36*mm,fill=0,stroke=1)
    pdf.setFillColor(HexColor("#70f0c6")); pdf.setFont("Helvetica-Bold",12); pdf.drawCentredString(w/2,h-38*mm,cfg.get("academy_name","KCI Academy").upper())
    pdf.setFillColor(HexColor("#ffffff")); pdf.setFont("Helvetica-Bold",30); pdf.drawCentredString(w/2,h-61*mm,"Certificate of Completion")
    pdf.setFont("Helvetica",12); pdf.setFillColor(HexColor("#b9c9d1")); pdf.drawCentredString(w/2,h-78*mm,"This certifies that")
    pdf.setFillColor(HexColor("#ffffff")); pdf.setFont("Helvetica-Bold",25); pdf.drawCentredString(w/2,h-96*mm,u["name"])
    pdf.setFont("Helvetica",12); pdf.setFillColor(HexColor("#b9c9d1")); pdf.drawCentredString(w/2,h-114*mm,"has successfully completed")
    pdf.setFillColor(HexColor("#70f0c6")); pdf.setFont("Helvetica-Bold",15); pdf.drawCentredString(w/2,h-126*mm,cfg.get("course_name","70 Days Embedded Systems Course · 2026"))
    pdf.setFillColor(HexColor("#b9c9d1")); pdf.setFont("Helvetica",9); pdf.drawCentredString(w/2,36*mm,f"Certificate ID: {cert['certificate_id']}  •  Issued: {cert['issued_at']}")
    try:
        import qrcode
        qr = qrcode.make(absolute_url(f"/verify?certificate_id={cert['certificate_id']}"))
        qr_path = UPLOADS / f"qr_{u['id']}.png"; qr.save(qr_path)
        pdf.drawImage(str(qr_path), w-62*mm, 25*mm, width=28*mm, height=28*mm, preserveAspectRatio=True, mask='auto')
        qr_path.unlink(missing_ok=True)
    except Exception:
        pass
    pdf.save(); buf.seek(0); path=UPLOADS/f"certificate_{u['id']}.pdf"; path.write_bytes(buf.getvalue()); return FileResponse(path,filename=f"{cert['certificate_id']}.pdf",media_type="application/pdf")


# Admin
@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    actor=staff(request)
    if not actor: return RedirectResponse("/login",303)
    c=db(); students=c.execute("SELECT * FROM users WHERE role='student' ORDER BY name").fetchall(); lessons=c.execute("SELECT l.*, (SELECT COUNT(*) FROM files f WHERE f.lesson_id=l.id) resources, (SELECT COUNT(*) FROM assignments a WHERE a.lesson_id=l.id AND a.active=1) assignments, (SELECT COUNT(*) FROM progress p WHERE p.lesson_id=l.id AND p.completed=1) completions, (SELECT COUNT(*) FROM quizzes q WHERE q.lesson_id=l.id AND q.active=1) quizzes FROM lessons l ORDER BY l.day").fetchall(); announcements=c.execute("SELECT * FROM announcements ORDER BY id DESC LIMIT 8").fetchall(); total_students=len(students); published=c.execute("SELECT COUNT(*) FROM lessons WHERE published=1").fetchone()[0]; completed=c.execute("SELECT COUNT(*) FROM progress WHERE completed=1").fetchone()[0]; submissions=c.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]; pending_submissions=c.execute("SELECT COUNT(*) FROM submissions WHERE submitted_at >= ?", ((datetime.now(timezone.utc).replace(microsecond=0) - __import__("datetime").timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S UTC"),)).fetchone()[0]; quiz_count=c.execute("SELECT COUNT(*) FROM quizzes WHERE active=1").fetchone()[0]; active_students=c.execute("SELECT COUNT(*) FROM users WHERE role='student' AND active=1").fetchone()[0]; certs=c.execute("SELECT COUNT(*) FROM certificates").fetchone()[0]; top_students=c.execute("SELECT u.id,u.name,COUNT(p.lesson_id) done FROM users u LEFT JOIN progress p ON p.user_id=u.id AND p.completed=1 WHERE u.role='student' GROUP BY u.id ORDER BY done DESC,u.name LIMIT 8").fetchall(); recent_audit=c.execute("SELECT a.*,u.name FROM audit_log a LEFT JOIN users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT 10").fetchall(); email_students=c.execute("SELECT COUNT(*) FROM users WHERE role='student' AND active=1 AND email IS NOT NULL AND email<>''").fetchone()[0]; password_setup=c.execute("SELECT COUNT(*) FROM users WHERE role='student' AND active=1 AND must_change_password=1").fetchone()[0]; cfg=settings(); readiness={"students":total_students>0,"lessons":published>0,"email_coverage":(email_students==active_students if active_students else True),"smtp":email_ready(),"secret":SECRET!="dev-only-change-me" or ENV!="production","base_url":PUBLIC_BASE_URL.startswith("https://") if ENV=="production" else True}; staff_users=c.execute("SELECT * FROM users WHERE role IN ('admin','instructor') ORDER BY role,name").fetchall(); c.close(); return page(request,"admin.html",students=students,lessons=lessons,announcements=announcements,total_students=total_students,active_students=active_students,published=published,completed=completed,submissions=submissions,pending_submissions=pending_submissions,quiz_count=quiz_count,certs=certs,top_students=top_students,recent_audit=recent_audit,course_settings=cfg,email_ready=email_ready(),smtp_host=SMTP_HOST,smtp_from=SMTP_FROM,readiness=readiness,email_students=email_students,password_setup=password_setup,actor=actor,can_manage_users=actor["role"]=="admin",staff_users=staff_users)


@app.post("/admin/logo")
def logo_upload(request: Request, csrf_token: str = Form(...), file: UploadFile = File(...)):
    admin=admin_only(request)
    if not admin or not check_csrf(request,csrf_token): return RedirectResponse("/login",303)
    filename=safe_filename(file.filename,{".png",".jpg",".jpeg",".svg"})
    if not filename: return redirect("/admin")
    try: data=bounded_read(file)
    except ValueError: return redirect("/admin")
    stored=f"academy_logo_{secrets.token_hex(8)}{Path(filename).suffix.lower()}"; (UPLOADS/stored).write_bytes(data)
    c=db(); old=c.execute("SELECT value FROM settings WHERE key='logo_stored'").fetchone(); c.execute("INSERT INTO settings(key,value) VALUES('logo_stored',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(stored,)); c.commit(); c.close()
    if old and old["value"]: (UPLOADS/old["value"]).unlink(missing_ok=True)
    audit(admin["id"],"logo_updated","academy",filename); flash(request,"Academy logo updated.","success"); return redirect("/admin")

@app.get("/brand/logo")
def brand_logo():
    cfg=settings(); stored=cfg.get("logo_stored","")
    if not stored: return RedirectResponse("/static/favicon.svg",307)
    path=UPLOADS/stored
    return FileResponse(path) if path.exists() else RedirectResponse("/static/favicon.svg",307)

@app.post("/admin/settings")
def admin_settings(request: Request, academy_name: str = Form(...), course_name: str = Form(...), academy_tagline: str = Form(""), certificate_prefix: str = Form("EA-2026"), certificate_enabled: str | None = Form(None), certificate_requirements: str = Form("All published lessons completed"), contact_email: str = Form(""), csrf_token: str = Form(...)):
    u=auth(request,"admin")
    if not u or not check_csrf(request,csrf_token): return RedirectResponse("/login",303)
    values={"academy_name":academy_name.strip()[:120],"course_name":course_name.strip()[:160],"academy_tagline":academy_tagline.strip()[:200],"certificate_prefix":re.sub(r"[^A-Za-z0-9-]","",certificate_prefix.strip())[:30] or "EA-2026","certificate_enabled":"1" if certificate_enabled else "0","certificate_requirements":certificate_requirements.strip()[:300],"contact_email":contact_email.strip()[:160]}
    c=db(); c.executemany("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",values.items()); c.commit(); c.close(); audit(u["id"],"settings_updated","academy"); return redirect("/admin")


@app.post("/admin/student/create")
def student_create(request: Request, name: str = Form(...), username: str = Form(...), password: str = Form(...), email: str = Form(""), csrf_token: str = Form(...)):
    if not auth(request, "admin") or not check_csrf(request, csrf_token): return RedirectResponse("/login", 303)
    if not safe_username(username) or len(password) < 8: return redirect("/admin")
    c = db()
    try: c.execute("INSERT INTO users(name,username,password,email,must_change_password) VALUES(?,?,?,?,1)", (name.strip(), username.strip(), hash_password(password), email.strip()[:200])) ; c.commit()
    except (sqlite3.IntegrityError if not USE_POSTGRES else psycopg.IntegrityError): pass
    c.close(); audit(auth(request,"admin")["id"],"student_created",username.strip()); return redirect("/admin")


@app.post("/admin/student/{student_id}/toggle")
def student_toggle(request: Request, student_id: int, csrf_token: str = Form(...)):
    if not auth(request, "admin") or not check_csrf(request, csrf_token): return RedirectResponse("/login", 303)
    c = db(); c.execute("UPDATE users SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=? AND role='student'", (student_id,)); c.commit(); c.close(); audit(auth(request,"admin")["id"],"student_toggled",f"student:{student_id}"); return redirect("/admin")


@app.post("/admin/student/{student_id}/reset")
def student_reset(request: Request, student_id: int, password: str = Form(...), csrf_token: str = Form(...)):
    if not auth(request, "admin") or not check_csrf(request, csrf_token): return RedirectResponse("/login", 303)
    if len(password) >= 8:
        c = db(); c.execute("UPDATE users SET password=?, must_change_password=1 WHERE id=? AND role='student'", (hash_password(password), student_id)); c.commit(); c.close(); audit(auth(request,"admin")["id"],"student_password_reset",f"student:{student_id}")
    return redirect("/admin")


@app.post("/admin/student/{student_id}/invite")
def admin_invite(request: Request, background_tasks: BackgroundTasks, student_id: int, csrf_token: str = Form(...)):
    admin = auth(request, "admin")
    if not admin or not check_csrf(request, csrf_token): return RedirectResponse("/login",303)
    c=db(); u=c.execute("SELECT * FROM users WHERE id=? AND role='student'",(student_id,)).fetchone()
    if not u: c.close(); return redirect("/admin")
    c.execute("UPDATE users SET must_change_password=1 WHERE id=?", (student_id,))
    raw=secrets.token_urlsafe(32); token_hash=hashlib.sha256(raw.encode()).hexdigest(); expires=(datetime.now(timezone.utc)+timedelta(minutes=60)).strftime("%Y-%m-%d %H:%M:%S UTC")
    c.execute("UPDATE password_resets SET used_at=? WHERE user_id=? AND used_at IS NULL",(now(),student_id)); c.execute("INSERT INTO password_resets(user_id,token_hash,expires_at) VALUES(?,?,?)",(student_id,token_hash,expires)); c.commit(); c.close()
    link=absolute_url(f"/reset-password?token={raw}")
    sent=queue_email(background_tasks, u["email"] if "email" in u.keys() else "", f"Welcome to {settings().get('academy_name','KCI Academy')}", f"Hello {u['name']},\n\nYour academy account is ready. Set your password using this link (valid for 60 minutes):\n{link}\n\nCourse: {settings().get('course_name','')}")
    audit(admin["id"],"student_invite_sent" if sent else "student_invite_link_created",f"student:{student_id}")
    return page(request,"reset_link.html",student=u,reset_link=None if sent else link,expires=expires,emailed=sent)

@app.post("/admin/student/{student_id}/reset-link")
def admin_reset_link(request: Request, student_id: int, csrf_token: str = Form(...)):
    admin = auth(request, "admin")
    if not admin or not check_csrf(request, csrf_token): return RedirectResponse("/login", 303)
    c = db(); u = c.execute("SELECT * FROM users WHERE id=? AND role='student'", (student_id,)).fetchone()
    if not u:
        c.close(); return redirect("/admin")
    raw = secrets.token_urlsafe(32); token_hash = hashlib.sha256(raw.encode()).hexdigest(); expires = (datetime.now(timezone.utc)+timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S UTC")
    c.execute("UPDATE password_resets SET used_at=? WHERE user_id=? AND used_at IS NULL", (now(), student_id))
    c.execute("INSERT INTO password_resets(user_id,token_hash,expires_at) VALUES(?,?,?)", (student_id, token_hash, expires)); c.commit(); c.close()
    audit(admin["id"], "password_reset_link_created", f"student:{student_id}")
    return page(request, "reset_link.html", student=u, reset_link=f"/reset-password?token={raw}", expires=expires)

@app.get("/admin/student/{student_id}/progress", response_class=HTMLResponse)
def admin_progress(request: Request, student_id: int):
    if not auth(request, "admin"): return RedirectResponse("/login", 303)
    c = db(); student = c.execute("SELECT * FROM users WHERE id=? AND role='student'", (student_id,)).fetchone(); rows = c.execute("SELECT l.*, COALESCE(p.completed,0) completed,p.completed_at FROM lessons l LEFT JOIN progress p ON p.lesson_id=l.id AND p.user_id=? ORDER BY l.day", (student_id,)).fetchall();
    submissions = c.execute("SELECT s.*,a.title assignment_title,l.day FROM submissions s JOIN assignments a ON a.id=s.assignment_id JOIN lessons l ON l.id=a.lesson_id WHERE s.user_id=? ORDER BY s.submitted_at DESC", (student_id,)).fetchall(); c.close()
    done = sum(r["completed"] for r in rows if r["completed"]); total = len([r for r in rows if r["published"]]); pct = round(done/total*100) if total else 0
    return page(request, "student_progress.html", student=student, rows=rows, submissions=submissions, done=done, total=total, pct=pct)


@app.post("/admin/announcement/create")
def announcement_create(request: Request, title: str = Form(...), body: str = Form(...), csrf_token: str = Form(...)):
    if not staff(request) or not check_csrf(request, csrf_token): return RedirectResponse("/login", 303)
    c = db(); clean_title, clean_body = title.strip()[:160], body.strip()[:2000]
    c.execute("INSERT INTO announcements(title,body) VALUES(?,?)", (clean_title, clean_body))
    students = c.execute("SELECT id FROM users WHERE role='student' AND active=1").fetchall()
    c.executemany("INSERT INTO notifications(user_id,title,body,link) VALUES(?,?,?,?)", [(s["id"], clean_title, clean_body, "/dashboard") for s in students])
    c.commit(); c.close(); audit(staff(request)["id"],"announcement_created",clean_title); return redirect("/admin")


@app.post("/admin/announcement/{announcement_id}/toggle")
def announcement_toggle(request: Request, announcement_id: int, csrf_token: str = Form(...)):
    if not staff(request) or not check_csrf(request, csrf_token): return RedirectResponse("/login", 303)
    c = db(); c.execute("UPDATE announcements SET published=CASE published WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (announcement_id,)); c.commit(); c.close(); return redirect("/admin")


@app.post("/admin/lesson/{lesson_id}")
def lesson_update(request: Request, lesson_id: int, title: str = Form(...), category: str = Form(...), description: str = Form(""), video_url: str = Form(""), published: str | None = Form(None), csrf_token: str = Form(...)):
    if not staff(request) or not check_csrf(request, csrf_token): return RedirectResponse("/login", 303)
    if not valid_video_url(video_url): return redirect("/admin")
    c = db(); c.execute("UPDATE lessons SET title=?,category=?,description=?,video_url=?,published=?,updated_at=? WHERE id=?", (title.strip(), category.strip(), description.strip(), video_url.strip(), 1 if published else 0, now(), lesson_id)); c.commit(); c.close(); audit(staff(request)["id"],"lesson_updated",f"lesson:{lesson_id}","published" if published else "draft"); return redirect("/admin")


@app.post("/admin/lesson/{lesson_id}/upload")
def lesson_upload(request: Request, lesson_id: int, csrf_token: str = Form(...), file: UploadFile = File(...)):
    if not staff(request) or not check_csrf(request, csrf_token): return RedirectResponse("/login", 303)
    filename = safe_filename(file.filename)
    if not filename: return redirect("/admin")
    try: data = bounded_read(file)
    except ValueError: return redirect("/admin")
    stored = f"resource_{lesson_id}_{secrets.token_hex(8)}{Path(filename).suffix.lower()}"; (UPLOADS / stored).write_bytes(data)
    c = db(); c.execute("INSERT INTO files(lesson_id,filename,stored,size) VALUES(?,?,?,?)", (lesson_id, filename, stored, len(data))); c.commit(); c.close(); audit(staff(request)["id"],"resource_uploaded",f"lesson:{lesson_id}",filename); return redirect("/admin")


@app.post("/admin/assignment/create")
def assignment_create(request: Request, lesson_id: int = Form(...), title: str = Form(...), instructions: str = Form(...), due_text: str = Form(""), csrf_token: str = Form(...)):
    if not staff(request) or not check_csrf(request, csrf_token): return RedirectResponse("/login", 303)
    c = db(); c.execute("INSERT INTO assignments(lesson_id,title,instructions,due_text) VALUES(?,?,?,?)", (lesson_id,title.strip(),instructions.strip(),due_text.strip())); c.commit(); c.close(); audit(staff(request)["id"],"assignment_created",f"lesson:{lesson_id}",title.strip()); return redirect("/admin")


@app.post("/admin/quiz/create")
def quiz_create(request: Request, lesson_id: int = Form(...), title: str = Form(...), question: str = Form(...), option_a: str = Form(...), option_b: str = Form(...), option_c: str = Form(...), option_d: str = Form(...), correct: int = Form(...), csrf_token: str = Form(...)):
    if not staff(request) or not check_csrf(request, csrf_token): return RedirectResponse("/login", 303)
    if correct not in range(4): return redirect("/admin")
    c = db(); existing = c.execute("SELECT id FROM quizzes WHERE lesson_id=?", (lesson_id,)).fetchone()
    if existing:
        quiz_id = existing["id"]; c.execute("UPDATE quizzes SET title=?,active=1 WHERE id=?", (title.strip(), quiz_id))
    else:
        quiz_id = c.execute("INSERT INTO quizzes(lesson_id,title) VALUES(?,?) RETURNING id", (lesson_id,title.strip())).fetchone()[0]
    c.execute("INSERT INTO quiz_questions(quiz_id,question,options_json,correct_index) VALUES(?,?,?,?)", (quiz_id, question.strip(), json.dumps([option_a.strip(),option_b.strip(),option_c.strip(),option_d.strip()]), correct)); c.commit(); c.close(); audit(staff(request)["id"],"quiz_question_added",f"quiz:{quiz_id}",question.strip()); return redirect("/admin")


@app.post("/admin/assignment/{assignment_id}/toggle")
def assignment_toggle(request: Request, assignment_id: int, csrf_token: str = Form(...)):
    admin=staff(request)
    if not admin or not check_csrf(request,csrf_token): return RedirectResponse("/login",303)
    c=db(); c.execute("UPDATE assignments SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?",(assignment_id,)); c.commit(); c.close(); audit(admin["id"],"assignment_toggled",f"assignment:{assignment_id}"); flash(request,"Assignment status updated.","success"); return redirect("/admin")

@app.post("/admin/quiz/{quiz_id}/toggle")
def quiz_toggle(request: Request, quiz_id: int, csrf_token: str = Form(...)):
    admin=staff(request)
    if not admin or not check_csrf(request,csrf_token): return RedirectResponse("/login",303)
    c=db(); c.execute("UPDATE quizzes SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?",(quiz_id,)); c.commit(); c.close(); audit(admin["id"],"quiz_toggled",f"quiz:{quiz_id}"); flash(request,"Quiz status updated.","success"); return redirect("/admin")

@app.post("/admin/file/{file_id}/delete")
def file_delete(request: Request, file_id: int, csrf_token: str = Form(...)):
    admin=staff(request)
    if not admin or not check_csrf(request,csrf_token): return RedirectResponse("/login",303)
    c=db(); row=c.execute("SELECT * FROM files WHERE id=?",(file_id,)).fetchone()
    if row:
        path=UPLOADS / row["stored"]
        c.execute("DELETE FROM files WHERE id=?",(file_id,)); c.commit();
        if path.exists(): path.unlink(missing_ok=True)
        audit(admin["id"],"resource_deleted",f"file:{file_id}",row["filename"])
        flash(request,"Resource deleted.","success")
    c.close(); return redirect("/admin")

@app.post("/admin/staff/create")
def staff_create(request: Request, name: str = Form(...), username: str = Form(...), email: str = Form(""), password: str = Form(...), role: str = Form(...), csrf_token: str = Form(...)):
    admin=admin_only(request)
    if not admin or not check_csrf(request,csrf_token): return RedirectResponse("/login",303)
    if role not in {"admin","instructor"} or not safe_username(username) or len(password)<8 or not name.strip(): return redirect("/admin")
    c=db()
    try:
        c.execute("INSERT INTO users(name,username,password,email,role,active) VALUES(?,?,?,?,?,1)",(name.strip()[:120],username.strip(),hash_password(password),email.strip()[:200],role))
        c.commit(); audit(admin["id"],"staff_created",username.strip(),role); flash(request,"Staff account created.","success")
    except Exception:
        c.rollback(); flash(request,"Could not create staff account. Username may already exist.","error")
    finally: c.close()
    return redirect("/admin")

@app.post("/admin/staff/{staff_id}/toggle")
def staff_toggle(request: Request, staff_id: int, csrf_token: str = Form(...)):
    admin=admin_only(request)
    if not admin or not check_csrf(request,csrf_token): return RedirectResponse("/login",303)
    c=db(); row=c.execute("SELECT * FROM users WHERE id=? AND role IN ('admin','instructor')",(staff_id,)).fetchone()
    if row and row["id"] != admin["id"]:
        c.execute("UPDATE users SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?",(staff_id,)); c.commit(); audit(admin["id"],"staff_toggled",f"staff:{staff_id}")
    c.close(); return redirect("/admin")

@app.get("/admin/audit", response_class=HTMLResponse)
def admin_audit(request: Request):
    if not auth(request, "admin"): return RedirectResponse("/login", 303)
    c = db()
    rows = c.execute("SELECT a.*, COALESCE(u.name,'System') actor FROM audit_log a LEFT JOIN users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT 250").fetchall()
    c.close()
    return page(request, "audit.html", audit_rows=rows)


@app.get("/admin/submissions", response_class=HTMLResponse)
def admin_submissions(request: Request):
    if not staff(request): return RedirectResponse("/login", 303)
    c = db(); submissions = c.execute("SELECT s.*,u.name student_name,u.username,a.title assignment_title,l.day FROM submissions s JOIN users u ON u.id=s.user_id JOIN assignments a ON a.id=s.assignment_id JOIN lessons l ON l.id=a.lesson_id ORDER BY s.id DESC LIMIT 100").fetchall(); c.close(); return page(request, "submissions.html", submissions=submissions)


@app.get("/submissions/{submission_id}")
def submission_file(request: Request, submission_id: int):
    if not auth(request, "admin"): return RedirectResponse("/login", 303)
    c = db(); s = c.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone(); c.close();
    if not s or not s["stored"]: return RedirectResponse("/admin/submissions", 303)
    path = UPLOADS / s["stored"]
    return FileResponse(path, filename=s["filename"]) if path.exists() else RedirectResponse("/admin/submissions", 303)


@app.get("/notifications/read-all")
def notifications_read_all(request: Request):
    u=auth(request,"student")
    if not u: return RedirectResponse("/login",303)
    c=db(); c.execute("UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL", (now(),u["id"])); c.commit(); c.close(); return redirect("/dashboard")

@app.get("/admin/analytics", response_class=HTMLResponse)
def admin_analytics(request: Request):
    u=staff(request)
    if not u: return RedirectResponse("/login",303)
    c=db()
    lessons=c.execute("SELECT l.day,l.title,l.published,COUNT(p.user_id) completions FROM lessons l LEFT JOIN progress p ON p.lesson_id=l.id AND p.completed=1 GROUP BY l.id ORDER BY l.day").fetchall()
    students=c.execute("SELECT u.id,u.name,u.username,u.active,COUNT(CASE WHEN p.completed=1 THEN 1 END) done,MAX(p.completed_at) last_completion FROM users u LEFT JOIN progress p ON p.user_id=u.id WHERE u.role='student' GROUP BY u.id ORDER BY done DESC,u.name").fetchall()
    quiz_stats=c.execute("SELECT AVG(CASE WHEN total>0 THEN 100.0*score/total END) avg_score,COUNT(*) attempts FROM quiz_attempts").fetchone()
    weekly=c.execute("SELECT substr(COALESCE(completed_at,created_at),1,10) day,COUNT(*) completions FROM progress p JOIN users u ON u.id=p.user_id WHERE p.completed=1 AND substr(COALESCE(completed_at,created_at),1,10) >= substr(date('now','-6 day'),1,10) GROUP BY day ORDER BY day").fetchall() if not USE_POSTGRES else []
    c.close()
    return page(request,"analytics.html",lessons=lessons,students=students,quiz_stats=quiz_stats,weekly=weekly)

@app.get("/admin/students/template")
def student_csv_template(request: Request):
    if not auth(request,"admin"): return RedirectResponse("/login",303)
    path=BASE/"data"/"students_import_template.csv"
    path.write_text("name,username,password,email\nExample Student,student02,changeMe123,john@example.com\n",encoding="utf-8")
    return FileResponse(path,filename="students_import_template.csv",media_type="text/csv")

@app.post("/admin/students/import")
async def student_import(request: Request, csrf_token: str = Form(...), file: UploadFile = File(...)):
    u=auth(request,"admin")
    if not u or not check_csrf(request,csrf_token): return RedirectResponse("/login",303)
    raw=await file.read(200_000)
    try: rows=list(csv.DictReader(raw.decode("utf-8-sig").splitlines()))
    except Exception: return redirect("/admin")
    c=db(); created=0
    for row in rows[:200]:
        name=(row.get("name") or "").strip()[:120]; username=(row.get("username") or "").strip(); password=row.get("password") or ""
        if not name or not safe_username(username) or len(password)<8: continue
        try:
            c.execute("INSERT INTO users(name,username,password,email) VALUES(?,?,?,?)",(name,username,hash_password(password),(row.get("email") or "").strip()[:200])); created+=1
        except Exception:
            continue
    c.commit(); c.close(); audit(u["id"],"students_imported","batch",f"created={created}"); return redirect("/admin")

@app.post("/admin/reminders/run")
def admin_reminders_run(request: Request, csrf_token: str = Form(...)):
    admin=admin_only(request)
    if not admin or not check_csrf(request,csrf_token): return RedirectResponse("/login",303)
    c=db(); students=c.execute("SELECT id FROM users WHERE role='student' AND active=1 AND (last_login IS NULL OR last_login < ?)",(datetime.now(timezone.utc)-timedelta(days=3),)).fetchall()
    open_lesson=c.execute("SELECT day,title FROM lessons WHERE published=1 ORDER BY day LIMIT 1").fetchone()
    created=0
    if open_lesson:
        for st in students:
            c.execute("INSERT INTO notifications(user_id,title,body,link) VALUES(?,?,?,?)",(st["id"],"Keep your learning streak going",f"You have a published lesson waiting in the academy: Day {open_lesson['day']:02d} — {open_lesson['title']}.",f"/lesson/{open_lesson['day']}")); created+=1
    c.commit(); c.close(); audit(admin["id"],"reminders_run","batch",f"created={created}"); return redirect("/admin")

@app.post("/admin/submission/{submission_id}/review")
def review_submission(request: Request, submission_id: int, review_status: str = Form(...), review_note: str = Form(""), csrf_token: str = Form(...)):
    admin = staff(request)
    if not admin or not check_csrf(request, csrf_token): return RedirectResponse("/login",303)
    status = review_status if review_status in {"Pending","Reviewed","Needs revision"} else "Pending"
    c = db(); srow = c.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
    if not srow: c.close(); return RedirectResponse("/admin/submissions",303)
    c.execute("UPDATE submissions SET review_status=?, review_note=? WHERE id=?", (status, review_note.strip()[:2000], submission_id)); c.commit(); c.close()
    audit(admin["id"], "submission_reviewed", f"submission:{submission_id}", status)
    return RedirectResponse("/admin/submissions",303)

@app.get("/admin/export/students")
def export_students(request: Request):
    if not auth(request,"admin"): return RedirectResponse("/login",303)
    import io
    from fastapi.responses import StreamingResponse
    c=db(); rows=c.execute("SELECT name,username,email,active,created_at,last_login FROM users WHERE role='student' ORDER BY name").fetchall(); c.close()
    out=io.StringIO(); w=csv.writer(out); w.writerow(["name","username","email","status","created_at","last_login"])
    for r in rows: w.writerow([r["name"],r["username"],r["email"] or "", "Active" if r["active"] else "Disabled", r["created_at"], r["last_login"] or ""])
    return StreamingResponse(iter([out.getvalue()]), media_type="text/csv", headers={"Content-Disposition":"attachment; filename=academy_students.csv"})

@app.get("/admin/export/progress")
def export_progress(request: Request):
    if not auth(request,"admin"): return RedirectResponse("/login",303)
    import io
    from fastapi.responses import StreamingResponse
    c=db(); rows=c.execute("""SELECT u.name,u.username,l.day,l.title,p.completed,p.completed_at
      FROM users u CROSS JOIN lessons l LEFT JOIN progress p ON p.user_id=u.id AND p.lesson_id=l.id
      WHERE u.role='student' ORDER BY u.name,l.day""").fetchall(); c.close()
    out=io.StringIO(); w=csv.writer(out); w.writerow(["student","username","day","lesson","status","completed_at"])
    for r in rows: w.writerow([r["name"],r["username"],r["day"],r["title"],"Completed" if r["completed"] else "Pending",r["completed_at"] or ""])
    return StreamingResponse(iter([out.getvalue()]), media_type="text/csv", headers={"Content-Disposition":"attachment; filename=academy_progress.csv"})

@app.get("/api/summary")
def api_summary(request: Request):
    if not auth(request,"admin"): return JSONResponse({"detail":"Unauthorized"}, status_code=401)
    c=db(); students=c.execute("SELECT COUNT(*) n FROM users WHERE role='student'").fetchone()[0]; active=c.execute("SELECT COUNT(*) n FROM users WHERE role='student' AND active=1").fetchone()[0]
    published=c.execute("SELECT COUNT(*) n FROM lessons WHERE published=1").fetchone()[0]; completions=c.execute("SELECT COUNT(*) n FROM progress WHERE completed=1").fetchone()[0]
    reviewed=c.execute("SELECT COUNT(*) n FROM submissions WHERE review_status='Reviewed'").fetchone()[0]; pending=c.execute("SELECT COUNT(*) n FROM submissions WHERE review_status='Pending'").fetchone()[0]
    c.close(); return {"students":students,"active_students":active,"published_lessons":published,"completions":completions,"reviewed_submissions":reviewed,"pending_submissions":pending}

@app.get("/health")
def health(): return {"status":"ok","service":"embedded-academy","version":"15.0","schema_version":SCHEMA_VERSION,"database":"postgresql" if USE_POSTGRES else "sqlite"}
