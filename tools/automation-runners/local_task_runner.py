#!/usr/bin/env python3
"""
local_task_runner.py

Purpose:
    Local dashboard for scheduling recurring script runs and tracking run history.

Created by:
    Avraham Makovsky

Focus:
    IT infrastructure, endpoint operations, validation/lab support, and practical
    automation for day-to-day support workflows.

License:
    MIT

Rationale:
    Some corporate or lab environments do not use Jira, Azure DevOps, ServiceNow,
    or another modern ticketing platform for every recurring operational task.
    In those cases, teams may lack a simple way to schedule internal scripts,
    track when they ran, keep stdout/stderr history, and extract created
    ticket/task references from script output.

    This tool is a small local alternative: it runs approved local scripts on a
    schedule, stores run history in SQLite, and exposes a local FastAPI dashboard
    for review.

Safety notes:
    - Designed as a local/internal automation tool, not an internet-facing service.
    - Keep the host as 127.0.0.1 unless proper authentication, TLS, and network
      controls are added.
    - Set LOCAL_TASK_RUNNER_TOKEN for local browser login protection.
    - Use only for scripts and systems you are authorized to operate.

Install:
    pip install fastapi uvicorn apscheduler python-multipart

Optional, only if your scheduled scripts use internal HTTP/Kerberos APIs:
    pip install requests urllib3 requests-kerberos

Optional local protection:
    set LOCAL_TASK_RUNNER_TOKEN=choose-a-long-random-token
    $env:LOCAL_TASK_RUNNER_TOKEN="choose-a-long-random-token"

Run:
    python local_task_runner.py

Open:
    http://127.0.0.1:8080
"""

from __future__ import annotations

import html
import ipaddress
import os
import re
import secrets
import shlex
import sqlite3
import subprocess
import sys
import threading
import traceback
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode
from uuid import uuid4

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from zoneinfo import ZoneInfo

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

APP_TITLE = "Local Task Runner"
APP_VERSION = "3.4.1"
APP_TIMEZONE = ZoneInfo(os.getenv("LOCAL_TASK_RUNNER_TIMEZONE", "UTC"))
APP_HOST = os.getenv("LOCAL_TASK_RUNNER_HOST", "127.0.0.1")
APP_PORT = int(os.getenv("LOCAL_TASK_RUNNER_PORT", "8080"))

# Optional intranet controls.
# Empty = no IP filtering. Example:
#   LOCAL_TASK_RUNNER_ALLOWED_CLIENTS=127.0.0.1,::1,10.10.0.0/16,192.168.56.25
# This is a network safety layer, not a replacement for authentication.
ALLOWED_CLIENTS_RAW = os.getenv("LOCAL_TASK_RUNNER_ALLOWED_CLIENTS", "").strip()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "app_data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "local_task_runner.db"
INLINE_DIR = DATA_DIR / "inline_scripts"
INLINE_DIR.mkdir(parents=True, exist_ok=True)

AUTH_TOKEN = os.getenv("LOCAL_TASK_RUNNER_TOKEN", "").strip()
AUTH_COOKIE_NAME = "local_task_runner_auth"
CSRF_TOKEN = secrets.token_urlsafe(32)

SQLITE_TIMEOUT_SECONDS = int(os.getenv("LOCAL_TASK_RUNNER_SQLITE_TIMEOUT", "30"))
LOG_MAX_CHARS = int(os.getenv("LOCAL_TASK_RUNNER_LOG_MAX_CHARS", "100000"))
RUN_RETENTION_MAX_ROWS = int(os.getenv("LOCAL_TASK_RUNNER_RUN_RETENTION_MAX_ROWS", "500"))
RUN_RETENTION_DAYS = int(os.getenv("LOCAL_TASK_RUNNER_RUN_RETENTION_DAYS", "90"))

DEFAULT_LAUNCHERS = {
    "python": sys.executable or "python",
    "powershell": "powershell.exe" if os.name == "nt" else "pwsh",
    "cmd": "cmd.exe" if os.name == "nt" else "sh",
}

DAY_LABELS = [
    ("sun", "Sun"),
    ("mon", "Mon"),
    ("tue", "Tue"),
    ("wed", "Wed"),
    ("thu", "Thu"),
    ("fri", "Fri"),
    ("sat", "Sat"),
]
DAY_NAMES = [value for value, _ in DAY_LABELS]
WEEKDAY_NAMES = ["mon", "tue", "wed", "thu", "fri"]
LEGACY_NUMERIC_DOW = {
    "0": "sun",
    "7": "sun",
    "1": "mon",
    "2": "tue",
    "3": "wed",
    "4": "thu",
    "5": "fri",
    "6": "sat",
}

scheduler = BackgroundScheduler(timezone=APP_TIMEZONE)
state_lock = threading.Lock()
running_jobs: set[str] = set()


def parse_allowed_clients(raw_value: str) -> list[ipaddress._BaseNetwork]:
    networks: list[ipaddress._BaseNetwork] = []
    for item in raw_value.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            # strict=False lets a plain IP such as 10.0.0.15 become a /32 or /128 network.
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            print(f"Ignoring invalid LOCAL_TASK_RUNNER_ALLOWED_CLIENTS item: {item}")
    return networks


ALLOWED_CLIENT_NETWORKS = parse_allowed_clients(ALLOWED_CLIENTS_RAW)


def client_ip_allowed(host: str | None) -> bool:
    if not ALLOWED_CLIENT_NETWORKS:
        return True
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in network for network in ALLOWED_CLIENT_NETWORKS)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    mark_interrupted_runs()
    cleanup_old_runs()
    if not scheduler.running:
        scheduler.start()
    load_scheduler_jobs()
    try:
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)


app = FastAPI(title=APP_TITLE, version=APP_VERSION, lifespan=lifespan)


@app.middleware("http")
async def ip_allowlist_middleware(request: Request, call_next):
    client_host = request.client.host if request.client else None
    if not client_ip_allowed(client_host):
        return PlainTextResponse("Forbidden: client IP is not allowed", status_code=403)
    return await call_next(request)


# -----------------------------------------------------------------------------
# Data models
# -----------------------------------------------------------------------------

@dataclass
class JobRecord:
    id: str
    name: str
    description: str
    source_type: str
    script_path: str
    script_inline: str
    cron_expr: str
    enabled: int
    timeout_seconds: int
    launcher_kind: str
    launcher_command: str
    working_directory: str
    created_at: str
    updated_at: str


@dataclass
class ScheduleBuilderState:
    mode: str
    time_value: str
    days: list[str]


@dataclass
class CommandSpec:
    command: list[str]
    workdir: str
    temp_file: Optional[Path]


# -----------------------------------------------------------------------------
# Generic helpers
# -----------------------------------------------------------------------------

def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def local_display(dt_str: Optional[str]) -> str:
    if not dt_str:
        return "-"
    try:
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(APP_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(dt_str)


def truncate_text(text: Any, max_chars: int = LOG_MAX_CHARS) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    text = str(text)
    if len(text) <= max_chars:
        return text
    marker = f"\n\n--- Output truncated to {max_chars} characters. Original length: {len(text)} characters. ---\n"
    keep = max(0, max_chars - len(marker))
    return text[:keep] + marker


def redirect_home(**params: str) -> RedirectResponse:
    clean = {k: v for k, v in params.items() if v}
    query = "?" + urlencode(clean) if clean else ""
    return RedirectResponse(url=f"/{query}", status_code=303)


def csrf_input() -> str:
    return f'<input type="hidden" name="csrf_token" value="{esc(CSRF_TOKEN)}" />'


# -----------------------------------------------------------------------------
# Local token auth and basic CSRF protection
# -----------------------------------------------------------------------------

def is_authenticated(request: Request) -> bool:
    if not AUTH_TOKEN:
        return True
    if request.cookies.get(AUTH_COOKIE_NAME) == AUTH_TOKEN:
        return True
    if request.headers.get("X-Local-Token") == AUTH_TOKEN:
        return True
    if request.query_params.get("token") == AUTH_TOKEN:
        return True
    return False


def auth_redirect_if_token_in_query(request: Request) -> Optional[RedirectResponse]:
    if not AUTH_TOKEN:
        return None
    if request.query_params.get("token") == AUTH_TOKEN:
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            AUTH_COOKIE_NAME,
            AUTH_TOKEN,
            httponly=True,
            samesite="strict",
            secure=False,
            max_age=60 * 60 * 12,
        )
        return response
    return None


def require_get_auth(request: Request) -> None:
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Not authenticated")


def require_post_auth(request: Request, csrf_token: str) -> None:
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    if csrf_token != CSRF_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def render_login_page(error: str = "") -> str:
    error_html = f"<div class='message-banner error'>{esc(error)}</div>" if error else ""
    return f"""
    <!doctype html>
    <html lang="en">
    <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{esc(APP_TITLE)} - Login</title>
        {get_app_styles()}
    </head>
    <body>
        <main class="login-wrap">
            <section class="login-card">
                <h1>{esc(APP_TITLE)}</h1>
                <p class="muted">Local token protection is enabled.</p>
                {error_html}
                <form method="post" action="/login" class="stack">
                    <label class="form-label">Access token</label>
                    <input name="token" type="password" class="form-input" autofocus autocomplete="current-password" />
                    <button class="btn btn-primary" type="submit">Open dashboard</button>
                </form>
            </section>
        </main>
    </body>
    </html>
    """


# -----------------------------------------------------------------------------
# Cron and schedule helpers
# -----------------------------------------------------------------------------

def split_cron(expr: str) -> Optional[tuple[str, str, str, str, str]]:
    parts = expr.strip().split()
    if len(parts) != 5:
        return None
    return parts[0], parts[1], parts[2], parts[3], parts[4]


def expand_legacy_numeric_dow(dow: str) -> Optional[list[str]]:
    """
    Converts the old UI convention 0=Sunday, 1=Monday, ... 6=Saturday
    into weekday names. This avoids APScheduler's numeric ambiguity, where
    weekday number 0 means Monday.
    """
    if not re.fullmatch(r"[0-7](?:[,-][0-7])*(?:-[0-7])?", dow):
        # The simple regex above is intentionally conservative. Fall back below.
        if not re.fullmatch(r"[0-7,\-]+", dow):
            return None

    output: list[str] = []
    for piece in dow.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if "-" in piece:
            start_s, end_s = piece.split("-", 1)
            if start_s not in LEGACY_NUMERIC_DOW or end_s not in LEGACY_NUMERIC_DOW:
                return None
            start = int("0" if start_s == "7" else start_s)
            end = int("0" if end_s == "7" else end_s)
            if start <= end:
                nums = list(range(start, end + 1))
            else:
                nums = list(range(start, 7)) + [0]
            for num in nums:
                name = LEGACY_NUMERIC_DOW[str(num)]
                if name not in output:
                    output.append(name)
        else:
            if piece not in LEGACY_NUMERIC_DOW:
                return None
            name = LEGACY_NUMERIC_DOW[piece]
            if name not in output:
                output.append(name)
    return output or None


def convert_legacy_cron_expr(expr: str) -> str:
    parts = split_cron(expr)
    if not parts:
        return expr
    minute, hour, dom, month, dow = parts
    if dow == "*":
        return expr
    if re.fullmatch(r"[0-7,\-]+", dow):
        names = expand_legacy_numeric_dow(dow)
        if names:
            return f"{minute} {hour} {dom} {month} {','.join(names)}"
    return expr


def expand_named_dow(dow: str) -> Optional[list[str]]:
    if dow == "*":
        return DAY_NAMES.copy()

    output: list[str] = []
    lowered = dow.lower()
    for piece in lowered.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if "-" in piece:
            start, end = piece.split("-", 1)
            if start not in DAY_NAMES or end not in DAY_NAMES:
                return None
            start_i = DAY_NAMES.index(start)
            end_i = DAY_NAMES.index(end)
            if start_i <= end_i:
                selected = DAY_NAMES[start_i:end_i + 1]
            else:
                selected = DAY_NAMES[start_i:] + DAY_NAMES[:end_i + 1]
            for name in selected:
                if name not in output:
                    output.append(name)
        else:
            if piece in DAY_NAMES:
                if piece not in output:
                    output.append(piece)
            elif re.fullmatch(r"[0-7]", piece):
                legacy = expand_legacy_numeric_dow(piece)
                if not legacy:
                    return None
                for name in legacy:
                    if name not in output:
                        output.append(name)
            else:
                return None
    return output or None


def parse_schedule_builder_state(cron_expr: str) -> ScheduleBuilderState:
    default = ScheduleBuilderState(mode="weekdays", time_value="09:00", days=WEEKDAY_NAMES.copy())
    converted = convert_legacy_cron_expr(cron_expr)
    parts = split_cron(converted)
    if not parts:
        return ScheduleBuilderState(mode="cron", time_value="09:00", days=default.days)

    minute, hour, day_of_month, month, day_of_week = parts

    # Check for monthly patterns
    if day_of_month == "1" and month == "*" and day_of_week == "*":
        if minute.isdigit() and hour.isdigit():
            hour_i = int(hour)
            minute_i = int(minute)
            if 0 <= hour_i <= 23 and 0 <= minute_i <= 59:
                time_value = f"{hour_i:02d}:{minute_i:02d}"
                return ScheduleBuilderState(mode="monthly", time_value=time_value, days=default.days)

    # Check for quarterly patterns
    if day_of_month == "1" and month in ["1,4,7,10", "1,4,7,10"] and day_of_week == "*":
        if minute.isdigit() and hour.isdigit():
            hour_i = int(hour)
            minute_i = int(minute)
            if 0 <= hour_i <= 23 and 0 <= minute_i <= 59:
                time_value = f"{hour_i:02d}:{minute_i:02d}"
                return ScheduleBuilderState(mode="quarterly", time_value=time_value, days=default.days)

    # Check for last day of month pattern
    if day_of_month == "28" and month == "*" and day_of_week == "*":
        if minute.isdigit() and hour.isdigit():
            hour_i = int(hour)
            minute_i = int(minute)
            if 0 <= hour_i <= 23 and 0 <= minute_i <= 59:
                time_value = f"{hour_i:02d}:{minute_i:02d}"
                return ScheduleBuilderState(mode="monthly_last", time_value=time_value, days=default.days)

    # Original daily/weekly pattern logic
    if not (minute.isdigit() and hour.isdigit() and day_of_month == "*" and month == "*"):
        return ScheduleBuilderState(mode="cron", time_value="09:00", days=default.days)

    hour_i = int(hour)
    minute_i = int(minute)
    if not (0 <= hour_i <= 23 and 0 <= minute_i <= 59):
        return ScheduleBuilderState(mode="cron", time_value="09:00", days=default.days)

    time_value = f"{hour_i:02d}:{minute_i:02d}"
    days = expand_named_dow(day_of_week)
    if days is None:
        return ScheduleBuilderState(mode="cron", time_value=time_value, days=default.days)

    if set(days) == set(DAY_NAMES) and len(days) == 7:
        return ScheduleBuilderState(mode="everyday", time_value=time_value, days=DAY_NAMES.copy())
    if days == WEEKDAY_NAMES or set(days) == set(WEEKDAY_NAMES):
        return ScheduleBuilderState(mode="weekdays", time_value=time_value, days=WEEKDAY_NAMES.copy())
    return ScheduleBuilderState(mode="custom", time_value=time_value, days=days)


def build_cron_from_friendly(schedule_mode: str, schedule_time: str, schedule_days: list[str]) -> str:
    time_value = (schedule_time or "09:00").strip()
    if not re.fullmatch(r"\d{2}:\d{2}", time_value):
        raise ValueError("Schedule time must be in HH:MM format")

    hour_s, minute_s = time_value.split(":")
    hour = int(hour_s)
    minute = int(minute_s)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("Schedule time is out of range")

    if schedule_mode == "everyday":
        dow = "*"
    elif schedule_mode == "weekdays":
        dow = "mon-fri"
    elif schedule_mode == "monthly":
        # First day of every month
        return f"{minute} {hour} 1 * *"
    elif schedule_mode == "monthly_last":
        # Last day of every month (28th to be safe across all months)
        return f"{minute} {hour} 28 * *"
    elif schedule_mode == "quarterly":
        # First day of Jan, Apr, Jul, Oct
        return f"{minute} {hour} 1 1,4,7,10 *"
    elif schedule_mode == "custom":
        clean_days = [d for d in DAY_NAMES if d in set(schedule_days)]
        if not clean_days:
            raise ValueError("Select at least one day for a custom schedule")
        dow = ",".join(clean_days)
    else:
        raise ValueError("Unsupported friendly schedule mode")

    return f"{minute} {hour} * * {dow}"


def validate_cron(cron_expr: str) -> CronTrigger:
    cron_expr = convert_legacy_cron_expr(cron_expr.strip())
    return CronTrigger.from_crontab(cron_expr, timezone=APP_TIMEZONE)


# -----------------------------------------------------------------------------
# Database
# -----------------------------------------------------------------------------

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=SQLITE_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_TIMEOUT_SECONDS * 1000}")
    return conn


def init_db() -> None:
    with sqlite3.connect(DB_PATH, timeout=SQLITE_TIMEOUT_SECONDS) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {SQLITE_TIMEOUT_SECONDS * 1000}")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL DEFAULT 'file',
                script_path TEXT NOT NULL DEFAULT '',
                script_inline TEXT NOT NULL DEFAULT '',
                cron_expr TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                timeout_seconds INTEGER NOT NULL DEFAULT 300,
                launcher_kind TEXT NOT NULL DEFAULT 'python',
                launcher_command TEXT NOT NULL DEFAULT '',
                working_directory TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                duration_seconds REAL,
                exit_code INTEGER,
                ticket_ref TEXT,
                message TEXT,
                stdout TEXT,
                stderr TEXT,
                FOREIGN KEY (job_id) REFERENCES jobs (id) ON DELETE CASCADE
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_job_id_started_at ON runs(job_id, started_at DESC)")

        existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        migrations = [
            ("source_type", "ALTER TABLE jobs ADD COLUMN source_type TEXT NOT NULL DEFAULT 'file'"),
            ("script_inline", "ALTER TABLE jobs ADD COLUMN script_inline TEXT NOT NULL DEFAULT ''"),
            ("launcher_kind", "ALTER TABLE jobs ADD COLUMN launcher_kind TEXT NOT NULL DEFAULT 'python'"),
            ("launcher_command", "ALTER TABLE jobs ADD COLUMN launcher_command TEXT NOT NULL DEFAULT ''"),
            ("working_directory", "ALTER TABLE jobs ADD COLUMN working_directory TEXT NOT NULL DEFAULT ''"),
        ]
        for col, ddl in migrations:
            if col not in existing_columns:
                conn.execute(ddl)

        # Fix legacy day-of-week expressions from earlier versions that used 0=Sunday.
        rows = conn.execute("SELECT id, cron_expr FROM jobs").fetchall()
        for row in rows:
            fixed = convert_legacy_cron_expr(row[1])
            if fixed != row[1]:
                conn.execute("UPDATE jobs SET cron_expr = ?, updated_at = ? WHERE id = ?",
                             (fixed, now_utc_iso(), row[0]))

        conn.commit()


def db_row_to_job(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        id=row["id"],
        name=row["name"],
        description=row["description"] or "",
        source_type=row["source_type"] or "file",
        script_path=row["script_path"] or "",
        script_inline=row["script_inline"] or "",
        cron_expr=convert_legacy_cron_expr(row["cron_expr"]),
        enabled=int(row["enabled"]),
        timeout_seconds=int(row["timeout_seconds"]),
        launcher_kind=row["launcher_kind"] or "python",
        launcher_command=row["launcher_command"] or "",
        working_directory=row["working_directory"] or "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_all_jobs() -> list[JobRecord]:
    with closing(get_connection()) as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY name COLLATE NOCASE").fetchall()
    return [db_row_to_job(row) for row in rows]


def get_job(job_id: str) -> Optional[JobRecord]:
    if not job_id:
        return None
    with closing(get_connection()) as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return db_row_to_job(row) if row else None


def get_recent_runs(limit: int = 50) -> list[sqlite3.Row]:
    with closing(get_connection()) as conn:
        rows = conn.execute(
            """
            SELECT r.*, j.name AS job_name
            FROM runs r
            JOIN jobs j ON j.id = r.job_id
            ORDER BY r.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return rows


def get_runs_for_job(job_id: str, limit: int = 30) -> list[sqlite3.Row]:
    with closing(get_connection()) as conn:
        rows = conn.execute(
            "SELECT * FROM runs WHERE job_id = ? ORDER BY id DESC LIMIT ?",
            (job_id, limit),
        ).fetchall()
    return rows


def get_run(run_id: int) -> Optional[sqlite3.Row]:
    with closing(get_connection()) as conn:
        row = conn.execute(
            """
            SELECT r.*, j.name AS job_name
            FROM runs r
            JOIN jobs j ON j.id = r.job_id
            WHERE r.id = ?
            """,
            (run_id,),
        ).fetchone()
    return row


def mark_interrupted_runs() -> None:
    finished = now_utc_iso()
    with closing(get_connection()) as conn:
        conn.execute(
            """
            UPDATE runs
            SET status = 'INTERRUPTED', finished_at = ?, message = 'Application restarted before this run finished'
            WHERE status IN ('RUNNING', 'QUEUED') AND finished_at IS NULL
            """,
            (finished,),
        )
        conn.commit()


def count_runs() -> int:
    with closing(get_connection()) as conn:
        row = conn.execute("SELECT COUNT(*) FROM runs").fetchone()
    return int(row[0] if row else 0)


def cleanup_run_history(days: int = RUN_RETENTION_DAYS, keep_latest: int = RUN_RETENTION_MAX_ROWS) -> int:
    """
    Delete run history using two practical rules:
    - delete runs older than `days` days when days > 0
    - keep only the newest `keep_latest` rows when keep_latest >= 0

    Returns the number of deleted rows.
    """
    days = max(0, int(days))
    keep_latest = int(keep_latest)
    before = count_runs()

    with closing(get_connection()) as conn:
        if days > 0:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            conn.execute("DELETE FROM runs WHERE started_at < ?", (cutoff,))

        if keep_latest == 0:
            conn.execute("DELETE FROM runs")
        elif keep_latest > 0:
            keep_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM runs ORDER BY id DESC LIMIT ?",
                    (keep_latest,),
                ).fetchall()
            ]
            if keep_ids:
                placeholders = ",".join("?" for _ in keep_ids)
                conn.execute(f"DELETE FROM runs WHERE id NOT IN ({placeholders})", keep_ids)
        conn.commit()

    after = count_runs()
    deleted = max(0, before - after)
    if deleted:
        # VACUUM makes the SQLite file reflect the cleanup on disk instead of only
        # marking pages as reusable internally. This is useful for a local desktop tool.
        try:
            with closing(sqlite3.connect(DB_PATH, timeout=SQLITE_TIMEOUT_SECONDS)) as vacuum_conn:
                vacuum_conn.execute("VACUUM")
        except Exception:
            pass
    return deleted


def cleanup_old_runs() -> int:
    return cleanup_run_history(days=RUN_RETENTION_DAYS, keep_latest=RUN_RETENTION_MAX_ROWS)


def dashboard_stats() -> dict[str, int]:
    jobs = get_all_jobs()
    with closing(get_connection()) as conn:
        total_runs = int(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
        failed_runs = int(conn.execute("SELECT COUNT(*) FROM runs WHERE status = 'FAILED'").fetchone()[0])
        running_runs = int(conn.execute("SELECT COUNT(*) FROM runs WHERE status = 'RUNNING'").fetchone()[0])
        success_runs = int(conn.execute("SELECT COUNT(*) FROM runs WHERE status = 'SUCCESS'").fetchone()[0])
    return {
        "jobs": len(jobs),
        "enabled_jobs": sum(1 for job in jobs if job.enabled),
        "disabled_jobs": sum(1 for job in jobs if not job.enabled),
        "runs": total_runs,
        "success_runs": success_runs,
        "failed_runs": failed_runs,
        "running_runs": running_runs,
    }


def job_to_api_dict(job: JobRecord, include_inline_script: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": job.id,
        "name": job.name,
        "description": job.description,
        "source_type": job.source_type,
        "script_path": job.script_path,
        "cron_expr": job.cron_expr,
        "enabled": bool(job.enabled),
        "timeout_seconds": job.timeout_seconds,
        "launcher_kind": job.launcher_kind,
        "launcher_command": launcher_display(job),
        "working_directory": job.working_directory,
        "next_run": next_run_for(job.id),
        "last_run": job_last_run_summary(job.id),
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }
    if include_inline_script:
        data["script_inline"] = job.script_inline
    elif job.source_type == "inline":
        data["script_inline"] = "<hidden; request include_inline_script=true>"
    return data


def run_row_to_api_dict(row: sqlite3.Row, include_output: bool = False) -> dict[str, Any]:
    data = {
        "id": row["id"],
        "job_id": row["job_id"],
        "job_name": row["job_name"] if "job_name" in row.keys() else None,
        "trigger_type": row["trigger_type"],
        "started_at": row["started_at"],
        "started_at_local": local_display(row["started_at"]),
        "finished_at": row["finished_at"],
        "finished_at_local": local_display(row["finished_at"]),
        "status": row["status"],
        "duration_seconds": row["duration_seconds"],
        "exit_code": row["exit_code"],
        "ticket_ref": row["ticket_ref"],
        "message": row["message"],
    }
    if include_output:
        data["stdout"] = row["stdout"] or ""
        data["stderr"] = row["stderr"] or ""
    return data


def query_runs(
        job_id: str = "",
        status: str = "",
        trigger_type: str = "",
        ticket_ref: str = "",
        q: str = "",
        limit: int = 50,
        offset: int = 0,
) -> list[sqlite3.Row]:
    where: list[str] = []
    params: list[Any] = []

    if job_id:
        where.append("r.job_id = ?")
        params.append(job_id)
    if status:
        where.append("UPPER(r.status) = UPPER(?)")
        params.append(status)
    if trigger_type:
        where.append("r.trigger_type = ?")
        params.append(trigger_type)
    if ticket_ref:
        where.append("r.ticket_ref LIKE ?")
        params.append(f"%{ticket_ref}%")
    if q:
        like = f"%{q}%"
        where.append("(j.name LIKE ? OR r.message LIKE ? OR r.stdout LIKE ? OR r.stderr LIKE ? OR r.ticket_ref LIKE ?)")
        params.extend([like, like, like, like, like])

    where_sql = "WHERE " + " AND ".join(where) if where else ""
    limit = min(max(int(limit), 1), 500)
    offset = max(int(offset), 0)

    sql = f"""
        SELECT r.*, j.name AS job_name
        FROM runs r
        JOIN jobs j ON j.id = r.job_id
        {where_sql}
        ORDER BY r.id DESC
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])

    with closing(get_connection()) as conn:
        return conn.execute(sql, params).fetchall()


def query_runs_count(
        job_id: str = "",
        status: str = "",
        trigger_type: str = "",
        ticket_ref: str = "",
        q: str = "",
) -> int:
    # Keep count logic intentionally parallel to query_runs for useful API pagination metadata.
    where: list[str] = []
    params: list[Any] = []

    if job_id:
        where.append("r.job_id = ?")
        params.append(job_id)
    if status:
        where.append("UPPER(r.status) = UPPER(?)")
        params.append(status)
    if trigger_type:
        where.append("r.trigger_type = ?")
        params.append(trigger_type)
    if ticket_ref:
        where.append("r.ticket_ref LIKE ?")
        params.append(f"%{ticket_ref}%")
    if q:
        like = f"%{q}%"
        where.append("(j.name LIKE ? OR r.message LIKE ? OR r.stdout LIKE ? OR r.stderr LIKE ? OR r.ticket_ref LIKE ?)")
        params.extend([like, like, like, like, like])

    where_sql = "WHERE " + " AND ".join(where) if where else ""
    sql = f"""
        SELECT COUNT(*)
        FROM runs r
        JOIN jobs j ON j.id = r.job_id
        {where_sql}
    """
    with closing(get_connection()) as conn:
        return int(conn.execute(sql, params).fetchone()[0])


# -----------------------------------------------------------------------------
# Job validation and persistence
# -----------------------------------------------------------------------------

def parse_ticket_ref(text: str) -> Optional[str]:
    patterns = [
        r"^TICKET_REF\s*=\s*(.+)$",
        r"^TICKET_ID\s*=\s*(.+)$",
        r"^TASK_REF\s*=\s*(.+)$",
        r"^TASK_ID\s*=\s*(.+)$",
        r"^ISSUE_KEY\s*=\s*(.+)$",
        r"^JIRA_KEY\s*=\s*(.+)$",
        # Enhanced patterns for JSON responses
        r"'new_id':\s*(\d+)",
        r'"new_id":\s*(\d+)',
        r"new_id.*?(\d{8,})",  # Look for long numeric IDs
        r"Article ID:\s*(\d+)",
        r"ID:\s*(\d{8,})",
    ]
    for line in text.splitlines():
        stripped = line.strip()
        for pattern in patterns:
            match = re.search(pattern, stripped, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip()[:250]
    return None


def validate_job_source(source_type: str, script_path: str, script_inline: str) -> None:
    if source_type not in {"file", "inline"}:
        raise ValueError("Script source must be file or inline")
    if source_type == "file" and not script_path.strip():
        raise ValueError("Script path is required for file-based jobs")
    if source_type == "inline" and not script_inline.strip():
        raise ValueError("Inline script content is required for inline jobs")


def validate_launcher(launcher_kind: str, launcher_command: str) -> None:
    if launcher_kind not in {"python", "powershell", "cmd", "custom"}:
        raise ValueError("Launcher must be python, powershell, cmd, or custom")
    if launcher_kind == "custom":
        if not launcher_command.strip():
            raise ValueError("Custom launcher template is required when launcher is custom")
        if "{script}" not in launcher_command:
            raise ValueError("Custom launcher template must contain {script}")


def upsert_job(
        job_id: Optional[str],
        name: str,
        description: str,
        source_type: str,
        script_path: str,
        script_inline: str,
        cron_expr: str,
        enabled: int,
        timeout_seconds: int,
        launcher_kind: str,
        launcher_command: str,
        working_directory: str,
) -> str:
    name = name.strip()
    description = description.strip()
    source_type = source_type.strip() or "file"
    script_path = script_path.strip()
    launcher_kind = launcher_kind.strip() or "python"
    launcher_command = launcher_command.strip()
    working_directory = working_directory.strip()
    cron_expr = convert_legacy_cron_expr(cron_expr.strip())

    if not name:
        raise ValueError("Job name is required")
    validate_cron(cron_expr)
    validate_job_source(source_type, script_path, script_inline)
    validate_launcher(launcher_kind, launcher_command)
    if timeout_seconds < 5:
        raise ValueError("Timeout must be at least 5 seconds")

    updated = now_utc_iso()
    job_id = job_id or str(uuid4())

    with closing(get_connection()) as conn:
        existing = conn.execute("SELECT id, created_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE jobs
                SET name = ?, description = ?, source_type = ?, script_path = ?, script_inline = ?,
                    cron_expr = ?, enabled = ?, timeout_seconds = ?, launcher_kind = ?,
                    launcher_command = ?, working_directory = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    name,
                    description,
                    source_type,
                    script_path,
                    script_inline,
                    cron_expr,
                    int(enabled),
                    int(timeout_seconds),
                    launcher_kind,
                    launcher_command,
                    working_directory,
                    updated,
                    job_id,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO jobs (
                    id, name, description, source_type, script_path, script_inline, cron_expr, enabled,
                    timeout_seconds, launcher_kind, launcher_command, working_directory, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    name,
                    description,
                    source_type,
                    script_path,
                    script_inline,
                    cron_expr,
                    int(enabled),
                    int(timeout_seconds),
                    launcher_kind,
                    launcher_command,
                    working_directory,
                    updated,
                    updated,
                ),
            )
        conn.commit()

    sync_scheduler_job(job_id)
    return job_id


def delete_job(job_id: str) -> None:
    existing = scheduler.get_job(job_id)
    if existing:
        scheduler.remove_job(job_id)
    with closing(get_connection()) as conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        conn.commit()


def insert_run(job_id: str, trigger_type: str, status: str, message: str = "") -> int:
    with closing(get_connection()) as conn:
        cur = conn.execute(
            """
            INSERT INTO runs (job_id, trigger_type, started_at, status, message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (job_id, trigger_type, now_utc_iso(), status, message),
        )
        conn.commit()
        return int(cur.lastrowid)


def finish_run(
        run_id: int,
        status: str,
        started_at_iso: str,
        exit_code: Optional[int],
        message: str,
        stdout: str,
        stderr: str,
) -> None:
    finished = now_utc_iso()
    try:
        started = datetime.fromisoformat(started_at_iso)
        finished_dt = datetime.fromisoformat(finished)
        duration = round((finished_dt - started).total_seconds(), 3)
    except Exception:
        duration = None

    stdout = truncate_text(stdout)
    stderr = truncate_text(stderr)
    ticket_ref = parse_ticket_ref(stdout + "\n" + stderr)

    with closing(get_connection()) as conn:
        conn.execute(
            """
            UPDATE runs
            SET finished_at = ?, status = ?, duration_seconds = ?, exit_code = ?,
                ticket_ref = ?, message = ?, stdout = ?, stderr = ?
            WHERE id = ?
            """,
            (finished, status, duration, exit_code, ticket_ref, truncate_text(message, 4000), stdout, stderr, run_id),
        )
        conn.commit()


def sync_scheduler_job(job_id: str) -> None:
    job = get_job(job_id)
    existing = scheduler.get_job(job_id)
    if existing:
        scheduler.remove_job(job_id)

    if not job or not job.enabled:
        return

    trigger = validate_cron(job.cron_expr)
    scheduler.add_job(
        func=execute_job,
        trigger=trigger,
        id=job.id,
        replace_existing=True,
        kwargs={"job_id": job.id, "trigger_type": "scheduled"},
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )


def load_scheduler_jobs() -> None:
    for job in get_all_jobs():
        if job.enabled:
            sync_scheduler_job(job.id)


# -----------------------------------------------------------------------------
# Command execution
# -----------------------------------------------------------------------------

def resolve_inline_extension(launcher_kind: str) -> str:
    if launcher_kind == "python":
        return ".py"
    if launcher_kind == "powershell":
        return ".ps1"
    if launcher_kind == "cmd":
        return ".cmd" if os.name == "nt" else ".sh"
    return ".txt"


def split_custom_command(template: str, script_path: Path, workdir: str, job_name: str) -> list[str]:
    formatted = template.format(
        script=shlex.quote(str(script_path)),
        workdir=shlex.quote(workdir),
        job_name=shlex.quote(job_name),
    )
    try:
        parts = shlex.split(formatted, posix=True)
    except ValueError as exc:
        raise ValueError(f"Could not parse custom command template: {exc}") from exc
    if not parts:
        raise ValueError("Custom command template produced an empty command")
    return parts


def build_command_for_job(job: JobRecord) -> CommandSpec:
    temp_file: Optional[Path] = None

    if job.source_type == "file":
        script_path = Path(job.script_path).expanduser()
        if not script_path.exists():
            raise FileNotFoundError(f"Script not found: {script_path}")
        if not script_path.is_file():
            raise FileNotFoundError(f"Script path is not a file: {script_path}")
    else:
        # Enhanced inline script handling with job-specific directory
        suffix = resolve_inline_extension(job.launcher_kind)
        job_dir = INLINE_DIR / f"job_{job.id}"
        job_dir.mkdir(exist_ok=True)
        temp_file = job_dir / f"script{suffix}"
        temp_file.write_text(job.script_inline, encoding="utf-8")
        script_path = temp_file

    # Enhanced working directory logic
    if job.working_directory:
        workdir_path = Path(job.working_directory).expanduser()
    elif job.source_type == "file":
        workdir_path = script_path.parent
    else:
        # For inline scripts, use the job-specific directory to help with dependencies
        workdir_path = script_path.parent

    if not workdir_path.exists():
        raise FileNotFoundError(f"Working directory not found: {workdir_path}")
    if not workdir_path.is_dir():
        raise FileNotFoundError(f"Working directory is not a directory: {workdir_path}")
    workdir = str(workdir_path)

    if job.launcher_kind == "python":
        launcher = job.launcher_command.strip() or DEFAULT_LAUNCHERS["python"]
        return CommandSpec([launcher, str(script_path)], workdir, temp_file)

    if job.launcher_kind == "powershell":
        launcher = job.launcher_command.strip() or DEFAULT_LAUNCHERS["powershell"]
        return CommandSpec([launcher, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)], workdir,
                           temp_file)

    if job.launcher_kind == "cmd":
        launcher = job.launcher_command.strip() or DEFAULT_LAUNCHERS["cmd"]
        if os.name == "nt":
            return CommandSpec([launcher, "/c", str(script_path)], workdir, temp_file)
        return CommandSpec([launcher, str(script_path)], workdir, temp_file)

    if job.launcher_kind == "custom":
        command = split_custom_command(job.launcher_command.strip(), script_path, workdir, job.name)
        return CommandSpec(command, workdir, temp_file)

    raise ValueError(f"Unsupported launcher kind: {job.launcher_kind}")


def execute_job(job_id: str, trigger_type: str = "manual") -> None:
    job = get_job(job_id)
    if not job:
        return

    with state_lock:
        if job_id in running_jobs:
            run_id = insert_run(job_id, trigger_type, "SKIPPED", "Job is already running")
            run_row = get_run(run_id)
            started_at = run_row["started_at"] if run_row else now_utc_iso()
            finish_run(run_id, "SKIPPED", started_at, None, "Skipped because the job is already running", "", "")
            return
        running_jobs.add(job_id)

    run_id = insert_run(job_id, trigger_type, "RUNNING", "Job started")
    run_row = get_run(run_id)
    started_at = run_row["started_at"] if run_row else now_utc_iso()

    stdout = ""
    stderr = ""
    message = ""
    exit_code: Optional[int] = None
    final_status = "FAILED"
    temp_file: Optional[Path] = None

    try:
        spec = build_command_for_job(job)
        temp_file = spec.temp_file

        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        env['PYTHONUNBUFFERED'] = '1'

        completed = subprocess.run(
            spec.command,
            cwd=spec.workdir,
            capture_output=True,
            text=True,
            timeout=job.timeout_seconds,
            shell=False,
            env=env,
            encoding='utf-8',
            errors='replace',
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        exit_code = completed.returncode
        if completed.returncode == 0:
            final_status = "SUCCESS"
            message = "Completed successfully"
        else:
            final_status = "FAILED"
            message = f"Script exited with code {completed.returncode}"
    except subprocess.TimeoutExpired as exc:
        stdout = truncate_text(exc.stdout.decode('utf-8', errors='replace') if exc.stdout else "")
        stderr = truncate_text((exc.stderr.decode('utf-8',
                                                  errors='replace') if exc.stderr else "") + f"\nTimeout after {job.timeout_seconds} seconds")
        final_status = "FAILED"
        message = f"Timed out after {job.timeout_seconds} seconds"
    except Exception as exc:
        stderr = f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}"
        final_status = "FAILED"
        message = str(exc)
    finally:
        if temp_file:
            try:
                temp_file.unlink(missing_ok=True)
            except Exception:
                pass
        finish_run(run_id, final_status, started_at, exit_code, message, stdout, stderr)
        with state_lock:
            running_jobs.discard(job_id)
        cleanup_old_runs()


def start_job_thread(job_id: str, trigger_type: str = "manual") -> None:
    thread = threading.Thread(target=execute_job, kwargs={"job_id": job_id, "trigger_type": trigger_type}, daemon=True)
    thread.start()


def next_run_for(job_id: str) -> str:
    scheduled_job = scheduler.get_job(job_id)
    if not scheduled_job or not scheduled_job.next_run_time:
        return "-"
    return scheduled_job.next_run_time.astimezone(APP_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def job_last_run_summary(job_id: str) -> dict[str, str]:
    runs = get_runs_for_job(job_id, limit=1)
    if not runs:
        return {"status": "-", "time": "-", "ticket_ref": "-"}
    row = runs[0]
    return {
        "status": row["status"] or "-",
        "time": local_display(row["finished_at"] or row["started_at"]),
        "ticket_ref": row["ticket_ref"] or "-",
    }


def launcher_display(job: JobRecord) -> str:
    if job.launcher_kind == "custom":
        return job.launcher_command or "custom"
    return job.launcher_command.strip() or DEFAULT_LAUNCHERS.get(job.launcher_kind, job.launcher_kind)


# -----------------------------------------------------------------------------
# HTML rendering
# -----------------------------------------------------------------------------

def render_message_banner(error: str = "", info: str = "", warning: str = "") -> str:
    if error:
        tone = "error"
        text = f"Error: {error}"
    elif warning:
        tone = "warning"
        text = warning
    elif info:
        tone = "info"
        text = info
    else:
        return ""
    return f"""
    <div class="message-banner {tone}" id="message-banner">
        <span>{esc(text)}</span>
        <button type="button" onclick="closeMessageBanner()" aria-label="Close">x</button>
    </div>
    """


def render_dashboard_summary() -> str:
    stats = dashboard_stats()
    return f"""
    <section class="quick-strip" aria-label="Dashboard summary">
        <span><strong>{stats['jobs']}</strong> jobs</span>
        <span><strong>{stats['enabled_jobs']}</strong> enabled</span>
        <span><strong>{stats['runs']}</strong> run records</span>
        <span><strong>{stats['failed_runs']}</strong> failed</span>
    </section>
    """


def render_schedule_builder(schedule_state: ScheduleBuilderState, cron_expr: str) -> str:
    mode = schedule_state.mode if schedule_state.mode in {"everyday", "weekdays", "monthly", "monthly_last",
                                                          "quarterly", "custom", "cron"} else "weekdays"
    checked = {key: "checked" if mode == key else "" for key in
               ["everyday", "weekdays", "monthly", "monthly_last", "quarterly", "custom", "cron"]}
    selected_days = set(schedule_state.days)
    day_pills = []
    for value, label in DAY_LABELS:
        day_checked = "checked" if value in selected_days else ""
        day_pills.append(
            f"<label class='day-pill'><input type='checkbox' name='schedule_days' value='{esc(value)}' {day_checked} /> <span>{esc(label)}</span></label>"
        )

    return f"""
    <section class="form-section full-width">
        <div class="section-title-row">
            <div>
                <label class="section-label">Schedule</label>
                <p class="section-help">Use the friendly options for normal cases. The cron field stays visible so you can verify exactly what will be saved.</p>
            </div>
        </div>

        <div class="segmented" role="radiogroup" aria-label="Schedule mode">
            <label><input type="radio" name="schedule_mode" value="everyday" {checked['everyday']} onchange="syncScheduleUI()" /> <span>Every day</span></label>
            <label><input type="radio" name="schedule_mode" value="weekdays" {checked['weekdays']} onchange="syncScheduleUI()" /> <span>Weekdays</span></label>
            <label><input type="radio" name="schedule_mode" value="monthly" {checked['monthly']} onchange="syncScheduleUI()" /> <span>Monthly (1st)</span></label>
            <label><input type="radio" name="schedule_mode" value="monthly_last" {checked['monthly_last']} onchange="syncScheduleUI()" /> <span>Monthly (28th)</span></label>
            <label><input type="radio" name="schedule_mode" value="quarterly" {checked['quarterly']} onchange="syncScheduleUI()" /> <span>Quarterly</span></label>
            <label><input type="radio" name="schedule_mode" value="custom" {checked['custom']} onchange="syncScheduleUI()" /> <span>Custom days</span></label>
            <label><input type="radio" name="schedule_mode" value="cron" {checked['cron']} onchange="syncScheduleUI()" /> <span>Advanced cron</span></label>
        </div>

        <div id="friendly-schedule-section" class="schedule-section">
            <div class="two-col compact-gap">
                <div class="form-group">
                    <label class="form-label">Run time</label>
                    <input id="schedule_time" name="schedule_time" type="time" value="{esc(schedule_state.time_value)}" class="form-input" />
                </div>
                <div id="custom-days-section" class="form-group">
                    <label class="form-label">Days</label>
                    <div class="days-selector">{''.join(day_pills)}</div>
                </div>
            </div>
        </div>

        <div id="cron-section" class="schedule-section">
            <label class="form-label">Cron expression</label>
            <input id="cron_expr" name="cron_expr" value="{esc(cron_expr)}" required class="form-input mono" />
            <p class="help-text">Format: minute hour day month day_of_week. This version uses named days, for example <code>0 9 * * mon-fri</code>, to avoid APScheduler numeric weekday confusion.</p>
        </div>
    </section>
    """


def render_job_form(selected: Optional[JobRecord]) -> str:
    job_id = selected.id if selected else ""
    name = selected.name if selected else ""
    description = selected.description if selected else ""
    source_type = selected.source_type if selected else "file"
    script_path = selected.script_path if selected else ""
    script_inline = selected.script_inline if selected else ""
    cron_expr = selected.cron_expr if selected else "0 9 * * mon-fri"
    timeout_seconds = selected.timeout_seconds if selected else 300
    launcher_kind = selected.launcher_kind if selected else "python"
    launcher_command = selected.launcher_command if selected else ""
    working_directory = selected.working_directory if selected else ""
    enabled_checked = "checked" if (selected.enabled if selected else 1) else ""
    schedule_state = parse_schedule_builder_state(cron_expr)
    title = "Edit job" if selected else "Add job"

    return f"""
    <form method="post" action="/jobs" class="modal-card job-form">
        {csrf_input()}
        <input type="hidden" name="job_id" value="{esc(job_id)}" />

        <header class="modal-header">
            <div>
                <h2>{esc(title)}</h2>
                <p class="muted small">Create a recurring local script run. The Save button stays visible while you scroll.</p>
            </div>
            <a class="modal-close" href="/" aria-label="Close">x</a>
        </header>

        <main class="modal-body">
            <div class="form-grid">
                <div class="form-group full-width">
                    <label class="form-label">Job name *</label>
                    <input name="name" value="{esc(name)}" required class="form-input" placeholder="Example: Create monthly printer maintenance ticket" />
                </div>

                <div class="form-group full-width">
                    <label class="form-label">Description</label>
                    <textarea name="description" class="form-textarea" placeholder="What this job creates, checks, or reminds you to handle">{esc(description)}</textarea>
                    <p class="help-text">This text appears directly on the dashboard, so use it as your quick operational note.</p>
                </div>

                <section class="form-section full-width">
                    <label class="section-label">Script source</label>
                    <p class="section-help">Choose an existing file, or store a small inline script inside the scheduler database.</p>
                    <div class="segmented">
                        <label><input type="radio" name="source_type" value="file" {'checked' if source_type == 'file' else ''} onchange="syncSourceTypeUI()" /> <span>External file</span></label>
                        <label><input type="radio" name="source_type" value="inline" {'checked' if source_type == 'inline' else ''} onchange="syncSourceTypeUI()" /> <span>Inline script</span></label>
                    </div>

                    <div id="file-source-section" class="source-section">
                        <label class="form-label">Script path</label>
                        <input name="script_path" value="{esc(script_path)}" class="form-input mono" placeholder="C:\\Scripts\\create_ticket.py" />
                        <p class="help-text">Use a full path. If working directory is blank, the app runs from this file's folder.</p>
                    </div>

                    <div id="inline-source-section" class="source-section">
                        <label class="form-label">Inline script</label>
                        <textarea name="script_inline" class="form-textarea code-textarea" placeholder="print('TICKET_REF=ABC-123')">{esc(script_inline)}</textarea>
                        <p class="help-text">For ticket references, print a line like <code>TICKET_REF=ABC-123</code>, <code>JIRA_KEY=ABC-123</code>, or <code>TASK_ID=123</code>.</p>
                    </div>
                </section>

                {render_schedule_builder(schedule_state, cron_expr)}

                <section class="form-section full-width">
                    <label class="section-label">Execution</label>
                    <p class="section-help">Keep the launcher blank unless you need a specific Python, PowerShell, or custom executable.</p>
                    <div class="two-col">
                        <div class="form-group">
                            <label class="form-label">Launcher</label>
                            <select id="launcher_kind" name="launcher_kind" onchange="syncLauncherUI()" class="form-select">
                                <option value="python" {'selected' if launcher_kind == 'python' else ''}>Python</option>
                                <option value="powershell" {'selected' if launcher_kind == 'powershell' else ''}>PowerShell</option>
                                <option value="cmd" {'selected' if launcher_kind == 'cmd' else ''}>CMD / Batch</option>
                                <option value="custom" {'selected' if launcher_kind == 'custom' else ''}>Custom template</option>
                            </select>
                        </div>

                        <div class="form-group">
                            <label class="form-label">Executable / template</label>
                            <input id="launcher_command" name="launcher_command" value="{esc(launcher_command)}" class="form-input mono" />
                            <p id="launcher_help" class="help-text"></p>
                        </div>

                        <div class="form-group">
                            <label class="form-label">Timeout seconds</label>
                            <input name="timeout_seconds" type="number" min="5" value="{esc(timeout_seconds)}" required class="form-input" />
                        </div>

                        <div class="form-group">
                            <label class="form-label">Working directory</label>
                            <input name="working_directory" value="{esc(working_directory)}" class="form-input mono" placeholder="Blank = script folder" />
                        </div>
                    </div>
                </section>

                <div class="form-group full-width inline-check-row">
                    <label class="checkbox-label"><input type="checkbox" name="enabled" value="1" {enabled_checked} /> Enable this job</label>
                </div>
            </div>
        </main>

        <footer class="modal-footer">
            <a href="/" class="btn btn-secondary">Cancel</a>
            <button type="submit" class="btn btn-primary">Save job</button>
        </footer>
    </form>
    """


def status_dot(status: str) -> str:
    status = status or "-"
    return f"<span class='status-badge {esc(status).lower()}'>{esc(status)}</span>"


def short_path(value: str, max_len: int = 95) -> str:
    value = value or ""
    if len(value) <= max_len:
        return value
    return "..." + value[-max_len:]


def render_jobs_table(selected_job_id: Optional[str]) -> str:
    jobs = get_all_jobs()
    enabled_count = sum(1 for job in jobs if job.enabled)
    rows: list[str] = []

    for job in jobs:
        summary = job_last_run_summary(job.id)
        selected_class = " selected" if selected_job_id == job.id else ""
        toggle_label = "Pause" if job.enabled else "Resume"
        toggle_icon = "⏸" if job.enabled else "▶"
        toggle_tone = "warning" if job.enabled else "success"
        enabled_badge = '<span class="status-badge success">Enabled</span>' if job.enabled else '<span class="status-badge paused">Paused</span>'
        source_summary = job.script_path if job.source_type == "file" else "Inline script stored in database"
        launcher_summary = launcher_display(job)

        rows.append(
            f"""
            <tr class="job-row-table{selected_class}">
                <td class="job-cell">
                    <div class="job-name-line">
                        <strong>{esc(job.name)}</strong>
                        {enabled_badge}
                    </div>
                    <div class="job-description-line">{esc(job.description or 'No description yet')}</div>
                </td>
                <td class="schedule-cell">
                    <code>{esc(job.cron_expr)}</code>
                    <div class="muted small">Next: {esc(next_run_for(job.id))}</div>
                </td>
                <td class="last-cell">
                    {status_dot(summary['status'])}
                    <div class="muted small">{esc(summary['time'])}</div>
                </td>
                <td class="ticket-cell mono">{esc(summary['ticket_ref'])}</td>
                <td class="source-cell">
                    <span class="pill">{esc(job.source_type)}</span>
                    <div class="muted mono path-line">{esc(short_path(source_summary, 70))}</div>
                </td>
                <td class="launcher-cell">
                    <span class="pill">{esc(job.launcher_kind)}</span>
                    <div class="muted mono path-line">{esc(short_path(launcher_summary, 60))}</div>
                </td>
                <td class="actions-col">
                    <div class="table-actions" aria-label="Actions for {esc(job.name)}">
                        <form method="post" action="/jobs/{esc(job.id)}/run">
                            {csrf_input()}
                            <button class="icon-btn primary" title="Run now" aria-label="Run now">▶</button>
                        </form>
                        <form method="post" action="/jobs/{esc(job.id)}/toggle">
                            {csrf_input()}
                            <button class="icon-btn {toggle_tone}" title="{esc(toggle_label)}" aria-label="{esc(toggle_label)}">{toggle_icon}</button>
                        </form>
                        <a class="icon-btn" href="/?view_job_id={esc(job.id)}" title="Details" aria-label="Details">ⓘ</a>
                        <a class="icon-btn" href="/?job_id={esc(job.id)}" onclick="localStorage.setItem('local-task-runner-runs-open','true')" title="Runs" aria-label="Runs">▤</a>
                        <a class="icon-btn" href="/?edit_job_id={esc(job.id)}&show_form=1" title="Edit" aria-label="Edit">✎</a>
                        <form method="post" action="/jobs/{esc(job.id)}/delete" onsubmit="return confirm('Delete this job and its run history?')">
                            {csrf_input()}
                            <button class="icon-btn danger" title="Delete" aria-label="Delete">×</button>
                        </form>
                    </div>
                </td>
            </tr>
            """
        )

    if not rows:
        rows.append("<tr><td colspan='7' class='empty-state'>No jobs yet. Add your first recurring task.</td></tr>")

    return f"""
    <section class="card jobs-card table-dashboard-card">
        <div class="card-header compact-card-header">
            <div>
                <h2>Scheduled jobs</h2>
                <p class="muted">{len(jobs)} job(s), {enabled_count} enabled - timezone {esc(str(APP_TIMEZONE))}</p>
            </div>
            <div class="header-actions">
                <a class="icon-btn primary add-job-icon" href="/?show_form=1" title="Add job" aria-label="Add job">＋</a>
            </div>
        </div>
        <div class="table-wrap jobs-table-wrap">
            <table class="jobs-table-modern">
                <thead>
                    <tr>
                        <th>Job</th>
                        <th>Schedule</th>
                        <th>Last run</th>
                        <th>Task/ticket ref</th>
                        <th>Source</th>
                        <th>Launcher</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>{''.join(rows)}</tbody>
            </table>
        </div>
    </section>
    """


def render_runs_panel(job_id: Optional[str]) -> str:
    if job_id:
        job = get_job(job_id)
        subtitle = f"Job: {job.name}" if job else "All jobs"
        rows = get_runs_for_job(job_id, 50) if job else get_recent_runs(50)
    else:
        subtitle = "All jobs"
        rows = get_recent_runs(50)

    body_rows: list[str] = []
    for row in rows:
        run_job_name = row["job_name"] if "job_name" in row.keys() else row["job_id"]
        duration = f"{row['duration_seconds']}s" if row["duration_seconds"] is not None else "-"
        body_rows.append(
            f"""
            <tr>
                <td>#{esc(row['id'])}</td>
                <td>{esc(run_job_name)}</td>
                <td><span class="pill">{esc(row['trigger_type'])}</span></td>
                <td>{status_dot(row['status'])}</td>
                <td>{esc(local_display(row['started_at']))}</td>
                <td>{esc(local_display(row['finished_at']))}</td>
                <td>{esc(duration)}</td>
                <td>{esc(row['ticket_ref'] or '-')}</td>
                <td class="wrap-text">{esc(row['message'] or '-')}</td>
                <td><a href="/?view_run_id={esc(row['id'])}" class="link-btn">View</a></td>
            </tr>
            """
        )

    if not body_rows:
        body_rows.append("<tr><td colspan='10' class='empty-state'>No runs recorded yet.</td></tr>")

    return f"""
    <details id="runs-panel" class="card collapsible runs-card">
        <summary class="card-header details-summary">
            <div>
                <h2>Recent runs</h2>
                <p class="muted">{esc(subtitle)} - run history and cleanup tools.</p>
            </div>
            <span class="summary-hint">Click to expand/collapse</span>
        </summary>
        <div class="run-tools">
            <form method="post" action="/cleanup" class="cleanup-form">
                {csrf_input()}
                <label>Delete runs older than <input name="cleanup_days" type="number" min="1" value="7" /> days</label>
                <label>and keep latest <input name="keep_latest" type="number" min="1" value="100" /> rows</label>
                <button class="btn btn-secondary" type="submit"><span class="btn-ico">🧹</span><span>Clean</span></button>
            </form>
            <form method="post" action="/cleanup" onsubmit="return confirm('Clear all run history? Jobs will stay, only run records will be deleted.')">
                {csrf_input()}
                <input type="hidden" name="clear_all" value="1" />
                <button class="btn btn-danger" type="submit"><span class="btn-ico">🗑</span><span>Clear all</span></button>
            </form>
            {f'<a class="btn" href="/">View all runs</a>' if job_id else ''}
        </div>
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Job</th>
                        <th>Trigger</th>
                        <th>Status</th>
                        <th>Started</th>
                        <th>Finished</th>
                        <th>Duration</th>
                        <th>Task/ticket ref</th>
                        <th>Message</th>
                        <th>Details</th>
                    </tr>
                </thead>
                <tbody>{''.join(body_rows)}</tbody>
            </table>
        </div>
    </details>
    """


def render_job_details_modal(job: JobRecord) -> str:
    recent = get_runs_for_job(job.id, 8)
    rows = []
    for run in recent:
        rows.append(
            f"<tr><td>#{esc(run['id'])}</td><td>{status_dot(run['status'])}</td><td>{esc(local_display(run['started_at']))}</td><td>{esc(run['ticket_ref'] or '-')}</td><td><a href='/?view_run_id={esc(run['id'])}'>View</a></td></tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='5' class='empty-state'>No runs yet.</td></tr>")
    source = job.script_path if job.source_type == "file" else "Inline script"
    return f"""
    <section class="modal-card wide">
        <header class="modal-header"><div><h2>Job details</h2><p class="muted small">{esc(job.name)}</p></div><a href="/" class="modal-close">x</a></header>
        <main class="modal-body">
            <div class="details-grid">
                <div><strong>Name</strong><span>{esc(job.name)}</span></div>
                <div><strong>Status</strong><span>{'Enabled' if job.enabled else 'Paused'}</span></div>
                <div><strong>Source</strong><code>{esc(source)}</code></div>
                <div><strong>Launcher</strong><code>{esc(launcher_display(job))}</code></div>
                <div><strong>Cron</strong><code>{esc(job.cron_expr)}</code></div>
                <div><strong>Next run</strong><span>{esc(next_run_for(job.id))}</span></div>
                <div><strong>Timeout</strong><span>{esc(job.timeout_seconds)}s</span></div>
                <div><strong>Working dir</strong><span>{esc(job.working_directory or 'Script directory')}</span></div>
                <div class="full-width"><strong>Description</strong><span>{esc(job.description or '-')}</span></div>
            </div>
            <h3>Recent runs for this job</h3>
            <div class="table-wrap"><table><thead><tr><th>ID</th><th>Status</th><th>Started</th><th>Ticket</th><th>View</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
        </main>
        <footer class="modal-footer">
            <form method="post" action="/jobs/{esc(job.id)}/run">{csrf_input()}<button class="btn btn-primary">Run now</button></form>
            <a href="/?edit_job_id={esc(job.id)}&show_form=1" class="btn btn-secondary">Edit</a>
            <a href="/" class="btn">Close</a>
        </footer>
    </section>
    """


def render_run_details_modal(row: sqlite3.Row) -> str:
    return f"""
    <section class="modal-card wide run-modal">
        <header class="modal-header"><div><h2>Run details #{esc(row['id'])}</h2><p class="muted small">{esc(row['job_name'])}</p></div><a href="/" class="modal-close">x</a></header>
        <main class="modal-body">
            <div class="details-grid">
                <div><strong>Job</strong><span>{esc(row['job_name'])}</span></div>
                <div><strong>Status</strong><span>{status_dot(row['status'])}</span></div>
                <div><strong>Trigger</strong><span>{esc(row['trigger_type'])}</span></div>
                <div><strong>Started</strong><span>{esc(local_display(row['started_at']))}</span></div>
                <div><strong>Finished</strong><span>{esc(local_display(row['finished_at']))}</span></div>
                <div><strong>Duration</strong><span>{esc(str(row['duration_seconds']) + 's' if row['duration_seconds'] is not None else '-')}</span></div>
                <div><strong>Exit code</strong><span>{esc(row['exit_code'] if row['exit_code'] is not None else '-')}</span></div>
                <div><strong>Task/ticket ref</strong><span>{esc(row['ticket_ref'] or '-')}</span></div>
                <div class="full-width"><strong>Message</strong><span>{esc(row['message'] or '-')}</span></div>
            </div>
            <h3>Stdout</h3>
            <pre>{esc(row['stdout'] or 'No output')}</pre>
            <h3>Stderr</h3>
            <pre class="pre-error">{esc(row['stderr'] or 'No errors')}</pre>
        </main>
        <footer class="modal-footer"><a href="/" class="btn">Close</a></footer>
    </section>
    """


def get_app_styles() -> str:
    return """
    <style>
        :root {
            --bg: #eef3f8;
            --surface: #ffffff;
            --surface-2: #f7f9fc;
            --surface-3: #edf2f7;
            --border: #d8e1ec;
            --text: #172033;
            --muted: #64748b;
            --primary: #2563eb;
            --primary-hover: #1d4ed8;
            --secondary: #475569;
            --success: #0f8a5f;
            --warning: #b45309;
            --danger: #dc2626;
            --shadow: 0 12px 35px rgba(15, 23, 42, 0.09);
            --radius: 16px;
            --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
        }
        :root[data-theme="dark"] {
            --bg: #0b1220;
            --surface: #101a2e;
            --surface-2: #142035;
            --surface-3: #1b2a43;
            --border: #2b3b55;
            --text: #e5e7eb;
            --muted: #9aa8bd;
            --primary: #3b82f6;
            --primary-hover: #2563eb;
            --secondary: #64748b;
            --shadow: 0 12px 35px rgba(0, 0, 0, 0.35);
        }
        * { box-sizing: border-box; }
        html, body { min-height: 100%; }
        body {
            margin: 0;
            background: radial-gradient(circle at top left, rgba(37, 99, 235, .12), transparent 30%), var(--bg);
            color: var(--text);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
            line-height: 1.5;
        }
        a { color: var(--primary); }
        .wrap { max-width: 1480px; margin: 0 auto; padding: 24px; }
        .topbar {
            display: flex; align-items: center; justify-content: space-between; gap: 16px;
            margin-bottom: 18px; padding: 18px 20px; border: 1px solid var(--border);
            border-radius: var(--radius); background: rgba(255,255,255,.72); backdrop-filter: blur(8px); box-shadow: var(--shadow);
        }
        :root[data-theme="dark"] .topbar { background: rgba(16, 26, 46, .8); }
        .topbar h1 { margin: 0; font-size: 28px; letter-spacing: -0.02em; }
        .topbar .meta { color: var(--muted); font-size: 13px; margin-top: 4px; }
        .top-actions, .header-actions, .job-actions, .run-tools, .cleanup-form { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
        .summary-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 18px; }
        .summary-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 16px; box-shadow: var(--shadow); }
        .summary-value { display: block; font-size: 28px; font-weight: 800; }
        .summary-label { color: var(--muted); font-size: 13px; }
        .card, .login-card {
            background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
            box-shadow: var(--shadow); margin-bottom: 18px; overflow: hidden;
        }
        .card-header {
            display: flex; align-items: center; justify-content: space-between; gap: 16px;
            padding: 18px 20px; border-bottom: 1px solid var(--border); background: var(--surface-2);
        }
        .card-header h2 { margin: 0; font-size: 20px; }
        .muted { color: var(--muted); }
        .small { font-size: 12px; }
        .mono, code { font-family: var(--mono); }
        code { background: var(--surface-3); border: 1px solid var(--border); border-radius: 7px; padding: 2px 6px; white-space: nowrap; }
        .table-wrap { overflow-x: auto; }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 11px 12px; border-bottom: 1px solid var(--border); vertical-align: top; text-align: left; }
        th { background: var(--surface-2); color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
        tr:hover td { background: rgba(148, 163, 184, 0.08); }
        .wrap-text { max-width: 420px; overflow-wrap: anywhere; }
        .pill, .status-badge {
            display: inline-flex; align-items: center; border-radius: 999px; padding: 3px 9px;
            font-size: 12px; font-weight: 750; background: var(--surface-3); border: 1px solid var(--border);
        }
        .status-badge.success { color: var(--success); background: rgba(16, 185, 129, .12); border-color: rgba(16, 185, 129, .28); }
        .status-badge.failed { color: var(--danger); background: rgba(239, 68, 68, .12); border-color: rgba(239, 68, 68, .28); }
        .status-badge.running { color: var(--warning); background: rgba(245, 158, 11, .12); border-color: rgba(245, 158, 11, .28); }
        .status-badge.paused, .status-badge.skipped, .status-badge.interrupted { color: var(--muted); }
        .btn, .link-btn {
            display: inline-flex; align-items: center; justify-content: center; min-height: 36px;
            border: 1px solid var(--border); border-radius: 10px; padding: 8px 12px;
            background: var(--surface); color: var(--text); text-decoration: none; cursor: pointer;
            font-weight: 700; font-size: 13px; white-space: nowrap;
        }
        .btn:hover, .link-btn:hover { transform: translateY(-1px); border-color: var(--primary); }
        .btn-primary { background: var(--primary); border-color: var(--primary); color: white; }
        .btn-primary:hover { background: var(--primary-hover); }
        .btn-secondary { background: var(--secondary); border-color: var(--secondary); color: white; }
        .btn-danger { color: white; background: var(--danger); border-color: var(--danger); }
        .job-list { display: grid; gap: 12px; padding: 14px; }
        .job-card { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 18px; background: var(--surface-2); border: 1px solid var(--border); border-radius: 14px; padding: 16px; }
        .job-card.selected { outline: 2px solid var(--primary); }
        .job-title-row { display: flex; align-items: start; justify-content: space-between; gap: 12px; margin-bottom: 8px; }
        .job-title-row h3 { margin: 0; font-size: 18px; }
        .job-description { margin: 0 0 12px 0; color: var(--text); }
        .job-meta { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px 14px; margin: 0; }
        .job-meta div { min-width: 0; }
        .job-meta dt { color: var(--muted); font-size: 11px; font-weight: 800; text-transform: uppercase; letter-spacing: .04em; }
        .job-meta dd { margin: 2px 0 0 0; font-size: 13px; overflow-wrap: anywhere; }
        .wide-meta { grid-column: 1 / -1; }
        .job-actions { align-content: start; justify-content: flex-end; max-width: 310px; }
        .job-actions form, .run-tools form { margin: 0; display: inline-flex; }
        .message-banner { margin: 0 0 16px 0; padding: 12px 14px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); display: flex; justify-content: space-between; gap: 12px; }
        .message-banner.error { color: var(--danger); background: rgba(239, 68, 68, .10); }
        .message-banner.info { color: var(--primary); background: rgba(59, 130, 246, .10); }
        .message-banner.warning { color: var(--warning); background: rgba(245, 158, 11, .10); }
        .message-banner button, .modal-close { background: transparent; border: 0; color: inherit; text-decoration: none; cursor: pointer; font-weight: 900; }
        .collapsible > summary { cursor: pointer; list-style: none; }
        .collapsible > summary::-webkit-details-marker { display: none; }
        .summary-hint { color: var(--muted); font-size: 13px; }
        .run-tools { padding: 12px 20px; border-bottom: 1px solid var(--border); background: var(--surface); }
        .run-tools input { width: 72px; padding: 6px 8px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface-2); color: var(--text); }
        .modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,.58); z-index: 20; overflow: auto; padding: 3vh 18px; display: flex; align-items: flex-start; justify-content: center; }
        .modal-card { width: min(980px, 100%); max-height: 94vh; display: flex; flex-direction: column; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow); overflow: hidden; }
        .modal-card.wide { width: min(1120px, 100%); }
        .modal-header, .modal-footer { flex: 0 0 auto; padding: 16px 20px; background: var(--surface-2); border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; gap: 12px; }
        .modal-header h2 { margin: 0; }
        .modal-footer { border-bottom: 0; border-top: 1px solid var(--border); justify-content: flex-end; }
        .modal-body { flex: 1 1 auto; min-height: 0; overflow-y: auto; padding: 20px; }
        .form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
        .full-width { grid-column: 1 / -1; }
        .two-col { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
        .compact-gap { gap: 10px; }
        .form-section { border: 1px solid var(--border); background: rgba(148,163,184,.06); border-radius: var(--radius); padding: 15px; }
        .section-label { display: block; font-weight: 850; margin-bottom: 4px; }
        .section-help, .help-text { color: var(--muted); font-size: 12px; margin: 5px 0 10px 0; }
        .form-label { display: block; font-weight: 750; margin-bottom: 6px; }
        .form-input, .form-select, .form-textarea { width: 100%; border: 1px solid var(--border); border-radius: 10px; background: var(--surface); color: var(--text); padding: 10px 11px; font: inherit; }
        .form-textarea { min-height: 92px; resize: vertical; }
        .code-textarea { min-height: 240px; font-family: var(--mono); font-size: 13px; }
        .segmented { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0 12px 0; }
        .segmented label, .day-pill, .checkbox-label { cursor: pointer; }
        .segmented input { position: absolute; opacity: 0; pointer-events: none; }
        .segmented span, .day-pill { display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--border); border-radius: 999px; padding: 7px 11px; background: var(--surface); font-size: 13px; font-weight: 700; }
        .segmented input:checked + span { background: rgba(37,99,235,.12); border-color: var(--primary); color: var(--primary); }
        .days-selector { display: flex; flex-wrap: wrap; gap: 8px; }
        .source-section, .schedule-section { margin-top: 10px; }
        .inline-check-row { padding: 4px 0; }
        .details-grid { display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 12px; margin-bottom: 18px; }
        .details-grid > div { background: var(--surface-2); border: 1px solid var(--border); border-radius: 12px; padding: 11px; min-width: 0; overflow-wrap: anywhere; }
        .details-grid strong { display: block; color: var(--muted); font-size: 11px; margin-bottom: 5px; text-transform: uppercase; letter-spacing: .04em; }
        pre { background: #0b1020; color: #e5e7eb; padding: 14px; border-radius: 12px; overflow: auto; max-height: 340px; font-family: var(--mono); font-size: 13px; }
        .pre-error { border: 1px solid rgba(239,68,68,.55); }
        .empty-state { text-align: center; color: var(--muted); padding: 22px !important; }
        .login-wrap { min-height: 100vh; display: grid; place-items: center; padding: 24px; }
        .login-card { width: min(420px, 100%); padding: 24px; }
        .stack { display: grid; gap: 12px; }
        @media (max-width: 980px) {
            .summary-grid { grid-template-columns: repeat(2, minmax(0,1fr)); }
            .job-card { grid-template-columns: 1fr; }
            .job-actions { justify-content: flex-start; max-width: none; }
            .job-meta { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        }
        @media (max-width: 720px) {
            .wrap { padding: 14px; }
            .topbar, .card-header { align-items: flex-start; flex-direction: column; }
            .form-grid, .two-col, .details-grid, .job-meta { grid-template-columns: 1fr; }
            .summary-grid { grid-template-columns: 1fr 1fr; }
            .modal-overlay { padding: 0; }
            .modal-card { max-height: 100vh; height: 100vh; border-radius: 0; }
        }
        .quick-strip {
            display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
            margin: -4px 0 12px 0; color: var(--muted); font-size: 12px;
        }
        .quick-strip span {
            display: inline-flex; align-items: baseline; gap: 5px;
            background: rgba(255,255,255,.58); border: 1px solid var(--border);
            border-radius: 999px; padding: 5px 9px;
        }
        :root[data-theme="dark"] .quick-strip span { background: rgba(16, 26, 46, .72); }
        .quick-strip strong { color: var(--text); font-size: 13px; }
        .topbar { padding: 14px 18px; margin-bottom: 12px; }
        .topbar h1 { font-size: 24px; }
        .icon-btn {
            width: 34px; height: 34px; min-width: 34px;
            display: inline-flex; align-items: center; justify-content: center;
            border: 1px solid var(--border); border-radius: 9px; background: var(--surface);
            color: var(--text); text-decoration: none; cursor: pointer;
            font-weight: 850; font-size: 15px; line-height: 1; padding: 0;
        }
        .icon-btn:hover { transform: translateY(-1px); border-color: var(--primary); }
        .icon-btn.primary { color: white; background: var(--primary); border-color: var(--primary); }
        .icon-btn.primary:hover { background: var(--primary-hover); }
        .icon-btn.warning { color: var(--warning); background: rgba(245,158,11,.12); border-color: rgba(245,158,11,.32); }
        .icon-btn.success { color: var(--success); background: rgba(16,185,129,.12); border-color: rgba(16,185,129,.32); }
        .icon-btn.danger { color: var(--danger); background: rgba(239,68,68,.10); border-color: rgba(239,68,68,.30); }
        .icon-btn.top-icon { width: 38px; height: 34px; min-width: 38px; }
        .compact-card-header { padding: 13px 16px; }
        .compact-card-header h2 { font-size: 18px; margin-bottom: 2px; }
        .compact-card-header p { margin: 0; font-size: 12px; }
        .add-job-icon { width: 38px; height: 36px; min-width: 38px; font-size: 18px; }
        .jobs-table-modern { table-layout: auto; font-size: 13px; }
        .jobs-table-modern th { padding: 9px 10px; white-space: nowrap; }
        .jobs-table-modern td { padding: 10px; vertical-align: middle; }
        .job-row-table.selected td { background: rgba(37,99,235,.075); }
        .job-cell { min-width: 260px; max-width: 440px; }
        .job-name-line { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 4px; }
        .job-name-line strong { font-size: 14px; }
        .job-description-line { color: var(--text); font-size: 12.5px; line-height: 1.35; overflow-wrap: anywhere; }
        .schedule-cell { min-width: 150px; }
        .last-cell { min-width: 135px; }
        .ticket-cell { min-width: 95px; }
        .source-cell { min-width: 180px; max-width: 260px; }
        .launcher-cell { min-width: 140px; max-width: 220px; }
        .path-line { margin-top: 4px; font-size: 11.5px; overflow-wrap: anywhere; }
        .actions-col { width: 224px; min-width: 224px; }
        .table-actions { display: flex; align-items: center; justify-content: flex-end; gap: 5px; flex-wrap: nowrap; }
        .table-actions form { margin: 0; display: inline-flex; }
        .table-actions .icon-btn { width: 31px; height: 31px; min-width: 31px; border-radius: 8px; font-size: 14px; }
        .table-actions .icon-btn.danger { font-size: 19px; }
        .run-tools .btn { min-height: 32px; padding: 6px 10px; }
        .modal-footer .btn, .modal-footer button { min-width: 92px; }
        @media (max-width: 1050px) { .jobs-table-modern { min-width: 1080px; } }
        @media (max-width: 720px) {
            .topbar { flex-direction: row; align-items: center; }
            .topbar h1 { font-size: 19px; }
            .topbar .meta { display: none; }
            .compact-card-header { flex-direction: row; align-items: center; }
            .compact-card-header p { display: none; }
        }
    </style>
    """


def get_app_scripts() -> str:
    return """
    <script>
        function currentScheduleMode() {
            const checked = document.querySelector('input[name="schedule_mode"]:checked');
            return checked ? checked.value : "weekdays";
        }

        function syncScheduleUI() {
            const mode = currentScheduleMode();
            const friendly = document.getElementById("friendly-schedule-section");
            const cron = document.getElementById("cron-section");
            const customDays = document.getElementById("custom-days-section");
            const cronInput = document.getElementById("cron_expr");
            const timeInput = document.getElementById("schedule_time");
            if (!friendly || !cron || !customDays || !cronInput || !timeInput) return;

            if (mode === "cron") {
                friendly.style.display = "none";
                cron.style.display = "block";
                return;
            }

            friendly.style.display = "block";
            cron.style.display = "block";
            customDays.style.display = mode === "custom" ? "block" : "none";

            const timeValue = timeInput.value || "09:00";
            const parts = timeValue.split(":");
            const hour = String(parseInt(parts[0] || "9", 10));
            const minute = String(parseInt(parts[1] || "0", 10));

            let cronExpr = "";
            if (mode === "everyday") {
                cronExpr = `${minute} ${hour} * * *`;
            } else if (mode === "weekdays") {
                cronExpr = `${minute} ${hour} * * mon-fri`;
            } else if (mode === "monthly") {
                cronExpr = `${minute} ${hour} 1 * *`;
            } else if (mode === "monthly_last") {
                cronExpr = `${minute} ${hour} 28 * *`;
            } else if (mode === "quarterly") {
                cronExpr = `${minute} ${hour} 1 1,4,7,10 *`;
            } else if (mode === "custom") {
                const selected = Array.from(document.querySelectorAll('input[name="schedule_days"]:checked')).map(el => el.value);
                const dow = selected.length ? selected.join(",") : "mon";
                cronExpr = `${minute} ${hour} * * ${dow}`;
            }

            cronInput.value = cronExpr;
        }

        function syncSourceTypeUI() {
            const source = document.querySelector('input[name="source_type"]:checked');
            const fileSection = document.getElementById("file-source-section");
            const inlineSection = document.getElementById("inline-source-section");
            if (!source || !fileSection || !inlineSection) return;
            fileSection.style.display = source.value === "file" ? "block" : "none";
            inlineSection.style.display = source.value === "inline" ? "block" : "none";
        }

        function syncLauncherUI() {
            const kind = document.getElementById("launcher_kind");
            const input = document.getElementById("launcher_command");
            const help = document.getElementById("launcher_help");
            if (!kind || !input || !help) return;
            if (kind.value === "python") {
                input.placeholder = "Blank = current Python interpreter";
                help.textContent = "Use blank for the same Python that runs this app, or set python.exe path.";
            } else if (kind.value === "powershell") {
                input.placeholder = "Blank = powershell.exe";
                help.textContent = "Runs with -NoProfile -ExecutionPolicy Bypass -File.";
            } else if (kind.value === "cmd") {
                input.placeholder = "Blank = cmd.exe";
                help.textContent = "Good for .bat and .cmd files.";
            } else {
                input.placeholder = "Example: python {script}";
                help.textContent = "Custom template must contain {script}. Optional: {workdir}, {job_name}. It is parsed without shell=True.";
            }
        }

        function closeMessageBanner() {
            const el = document.getElementById("message-banner");
            if (el) el.remove();
        }

        function setTheme(theme) {
            document.documentElement.setAttribute("data-theme", theme);
            localStorage.setItem("local-task-runner-theme", theme);
        }

        function toggleTheme() {
            const current = document.documentElement.getAttribute("data-theme") || "light";
            setTheme(current === "dark" ? "light" : "dark");
        }

        function initRunsPanelState() {
            const panel = document.getElementById("runs-panel");
            if (!panel) return;
            const stored = localStorage.getItem("local-task-runner-runs-open");
            panel.open = stored === "true";
            panel.addEventListener("toggle", function() {
                localStorage.setItem("local-task-runner-runs-open", panel.open ? "true" : "false");
            });
        }

        document.addEventListener("DOMContentLoaded", function() {
            const savedTheme = localStorage.getItem("local-task-runner-theme");
            if (savedTheme) document.documentElement.setAttribute("data-theme", savedTheme);
            else if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) document.documentElement.setAttribute("data-theme", "dark");
            initRunsPanelState();
            syncSourceTypeUI();
            syncLauncherUI();
            syncScheduleUI();
            document.querySelectorAll('input[name="schedule_days"]').forEach(el => el.addEventListener("change", syncScheduleUI));
            const timeInput = document.getElementById("schedule_time");
            if (timeInput) timeInput.addEventListener("change", syncScheduleUI);
            setTimeout(closeMessageBanner, 7000);
            document.addEventListener("keydown", function(e) {
                if (e.ctrlKey && e.key.toLowerCase() === "n") { e.preventDefault(); window.location.href = "/?show_form=1"; }
                if (e.ctrlKey && e.key.toLowerCase() === "d") { e.preventDefault(); toggleTheme(); }
                if (e.key === "Escape") { const modal = document.querySelector(".modal-overlay"); if (modal) window.location.href = "/"; }
            });
        });
    </script>
    """


def layout(title: str, body: str) -> str:
    auth_note = "Token auth: enabled" if AUTH_TOKEN else "Token auth: disabled - local use only"
    return f"""
    <!doctype html>
    <html lang="en">
    <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{esc(title)}</title>
        {get_app_styles()}
    </head>
    <body>
        <div class="wrap">
            <header class="topbar">
                <div>
                    <h1>{esc(APP_TITLE)} <span class="muted small">v{esc(APP_VERSION)}</span></h1>
                    <div class="meta">Local scheduler - timezone {esc(str(APP_TIMEZONE))} - {esc(auth_note)}</div>
                </div>
                <div class="top-actions">
                    <button type="button" class="icon-btn top-icon" onclick="toggleTheme()" title="Theme" aria-label="Theme">◐</button>
                    {f'<a class="icon-btn top-icon" href="/logout" title="Logout" aria-label="Logout">⇥</a>' if AUTH_TOKEN else ''}
                </div>
            </header>
            {body}
        </div>
        {get_app_scripts()}
    </body>
    </html>
    """


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse, response_model=None)
def login_page(request: Request):
    if not AUTH_TOKEN or is_authenticated(request):
        return RedirectResponse(url="/", status_code=303)
    return render_login_page()


@app.post("/login", response_model=None)
async def login(request: Request, token: str = Form("")):
    if not AUTH_TOKEN:
        return RedirectResponse(url="/", status_code=303)
    if token.strip() != AUTH_TOKEN:
        return HTMLResponse(render_login_page("Invalid token"), status_code=401)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        AUTH_COOKIE_NAME,
        AUTH_TOKEN,
        httponly=True,
        samesite="strict",
        secure=False,
        max_age=60 * 60 * 12,
    )
    return response


@app.get("/logout", response_model=None)
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(AUTH_COOKIE_NAME)
    return response


@app.get("/health", response_class=PlainTextResponse, response_model=None)
def health():
    return "ok"


@app.get("/api", response_model=None)
def api_index(request: Request):
    require_get_auth(request)
    return {
        "app": APP_TITLE,
        "version": APP_VERSION,
        "timezone": str(APP_TIMEZONE),
        "auth_required": bool(AUTH_TOKEN),
        "ip_allowlist_enabled": bool(ALLOWED_CLIENT_NETWORKS),
        "endpoints": {
            "stats": "/api/stats",
            "jobs": "/api/jobs",
            "job_details": "/api/jobs/{job_id}",
            "job_runs": "/api/jobs/{job_id}/runs",
            "runs": "/api/runs",
            "run_details": "/api/runs/{run_id}",
            "ticket_runs": "/api/tickets/{ticket_ref}/runs",
            "scheduler": "/api/scheduler",
        },
    }


@app.get("/api/stats", response_model=None)
def api_stats(request: Request):
    require_get_auth(request)
    return dashboard_stats()


@app.get("/api/jobs", response_model=None)
def api_jobs(
        request: Request,
        enabled: Optional[bool] = None,
        q: str = "",
        include_inline_script: bool = False,
):
    require_get_auth(request)
    q_norm = q.strip().lower()
    jobs = get_all_jobs()
    if enabled is not None:
        jobs = [job for job in jobs if bool(job.enabled) is enabled]
    if q_norm:
        jobs = [
            job for job in jobs
            if q_norm in job.name.lower()
               or q_norm in job.description.lower()
               or q_norm in job.script_path.lower()
               or q_norm in job.cron_expr.lower()
        ]
    return {
        "count": len(jobs),
        "jobs": [job_to_api_dict(job, include_inline_script=include_inline_script) for job in jobs],
    }


@app.get("/api/jobs/{job_id}", response_model=None)
def api_job_details(request: Request, job_id: str, include_inline_script: bool = False):
    require_get_auth(request)
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job_to_api_dict(job, include_inline_script=include_inline_script)


@app.get("/api/jobs/{job_id}/runs", response_model=None)
def api_job_runs(
        request: Request,
        job_id: str,
        status: str = "",
        ticket_ref: str = "",
        q: str = "",
        limit: int = 50,
        offset: int = 0,
        include_output: bool = False,
):
    require_get_auth(request)
    if not get_job(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    rows = query_runs(job_id=job_id, status=status, ticket_ref=ticket_ref, q=q, limit=limit, offset=offset)
    total = query_runs_count(job_id=job_id, status=status, ticket_ref=ticket_ref, q=q)
    return {
        "count": len(rows),
        "total_matches": total,
        "limit": min(max(int(limit), 1), 500),
        "offset": max(int(offset), 0),
        "runs": [run_row_to_api_dict(row, include_output=include_output) for row in rows],
    }


@app.get("/api/runs", response_model=None)
def api_runs(
        request: Request,
        job_id: str = "",
        status: str = "",
        trigger_type: str = "",
        ticket_ref: str = "",
        q: str = "",
        limit: int = 50,
        offset: int = 0,
        include_output: bool = False,
):
    require_get_auth(request)
    rows = query_runs(
        job_id=job_id.strip(),
        status=status.strip(),
        trigger_type=trigger_type.strip(),
        ticket_ref=ticket_ref.strip(),
        q=q.strip(),
        limit=limit,
        offset=offset,
    )
    total = query_runs_count(
        job_id=job_id.strip(),
        status=status.strip(),
        trigger_type=trigger_type.strip(),
        ticket_ref=ticket_ref.strip(),
        q=q.strip(),
    )
    return {
        "count": len(rows),
        "total_matches": total,
        "limit": min(max(int(limit), 1), 500),
        "offset": max(int(offset), 0),
        "runs": [run_row_to_api_dict(row, include_output=include_output) for row in rows],
    }


@app.get("/api/runs/{run_id}", response_model=None)
def api_run_details(request: Request, run_id: int, include_output: bool = True):
    require_get_auth(request)
    row = get_run(run_id)
    if not row:
        raise HTTPException(status_code=404, detail="Run not found")
    return run_row_to_api_dict(row, include_output=include_output)


@app.get("/api/tickets/{ticket_ref}/runs", response_model=None)
def api_ticket_runs(
        request: Request,
        ticket_ref: str,
        limit: int = 50,
        offset: int = 0,
        include_output: bool = False,
):
    require_get_auth(request)
    rows = query_runs(ticket_ref=ticket_ref.strip(), limit=limit, offset=offset)
    total = query_runs_count(ticket_ref=ticket_ref.strip())
    return {
        "ticket_ref": ticket_ref,
        "count": len(rows),
        "total_matches": total,
        "runs": [run_row_to_api_dict(row, include_output=include_output) for row in rows],
    }


@app.get("/api/scheduler", response_model=None)
def api_scheduler(request: Request):
    require_get_auth(request)
    jobs = get_all_jobs()
    return {
        "running": scheduler.running,
        "configured_timezone": str(APP_TIMEZONE),
        "currently_running_job_ids": sorted(running_jobs),
        "scheduled_jobs": [
            {
                "id": job.id,
                "name": job.name,
                "enabled": bool(job.enabled),
                "cron_expr": job.cron_expr,
                "next_run": next_run_for(job.id),
            }
            for job in jobs
        ],
    }


@app.get("/", response_class=HTMLResponse, response_model=None)
def home(
        request: Request,
        job_id: Optional[str] = None,
        edit_job_id: Optional[str] = None,
        view_job_id: Optional[str] = None,
        view_run_id: Optional[str] = None,
        show_form: int = 0,
        error: str = "",
        info: str = "",
        warning: str = "",
):
    token_redirect = auth_redirect_if_token_in_query(request)
    if token_redirect:
        return token_redirect
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)

    selected_job = get_job(edit_job_id) if edit_job_id else None
    view_job = get_job(view_job_id) if view_job_id else None
    view_run = get_run(int(view_run_id)) if view_run_id and view_run_id.isdigit() else None

    content = f"""
    {render_message_banner(error=error, info=info, warning=warning)}
    {render_jobs_table(selected_job_id=job_id)}
    {render_runs_panel(job_id=job_id)}
    """

    if show_form:
        content += f'<div class="modal-overlay">{render_job_form(selected_job)}</div>'
    elif view_job:
        content += f'<div class="modal-overlay">{render_job_details_modal(view_job)}</div>'
    elif view_run:
        content += f'<div class="modal-overlay">{render_run_details_modal(view_run)}</div>'

    return layout(APP_TITLE, content)


@app.post("/jobs", response_model=None)
def save_job(
        request: Request,
        csrf_token: str = Form(""),
        job_id: str = Form(""),
        name: str = Form(""),
        description: str = Form(""),
        source_type: str = Form("file"),
        script_path: str = Form(""),
        script_inline: str = Form(""),
        schedule_mode: str = Form("weekdays"),
        schedule_time: str = Form("09:00"),
        schedule_days: list[str] = Form([]),
        cron_expr: str = Form("0 9 * * mon-fri"),
        enabled: Optional[str] = Form(None),
        timeout_seconds: int = Form(300),
        launcher_kind: str = Form("python"),
        launcher_command: str = Form(""),
        working_directory: str = Form(""),
):
    require_post_auth(request, csrf_token)
    try:
        if schedule_mode != "cron":
            cron_expr = build_cron_from_friendly(schedule_mode, schedule_time, schedule_days)
        else:
            cron_expr = convert_legacy_cron_expr(cron_expr)
        saved_id = upsert_job(
            job_id=job_id or None,
            name=name,
            description=description,
            source_type=source_type,
            script_path=script_path,
            script_inline=script_inline,
            cron_expr=cron_expr,
            enabled=1 if enabled else 0,
            timeout_seconds=timeout_seconds,
            launcher_kind=launcher_kind,
            launcher_command=launcher_command,
            working_directory=working_directory,
        )
        return redirect_home(info="Job saved", job_id=saved_id)
    except sqlite3.IntegrityError as exc:
        return redirect_home(error=f"Database error: {exc}")
    except Exception as exc:
        return redirect_home(error=str(exc))


@app.post("/jobs/{job_id}/run", response_model=None)
def run_job_now(request: Request, job_id: str, csrf_token: str = Form("")):
    require_post_auth(request, csrf_token)
    job = get_job(job_id)
    if not job:
        return redirect_home(error="Job not found")
    start_job_thread(job_id, "manual")
    return redirect_home(info=f"Started job: {job.name}", job_id=job_id)


@app.post("/jobs/{job_id}/toggle", response_model=None)
def toggle_job(request: Request, job_id: str, csrf_token: str = Form("")):
    require_post_auth(request, csrf_token)
    job = get_job(job_id)
    if not job:
        return redirect_home(error="Job not found")
    new_enabled = 0 if job.enabled else 1
    with closing(get_connection()) as conn:
        conn.execute("UPDATE jobs SET enabled = ?, updated_at = ? WHERE id = ?", (new_enabled, now_utc_iso(), job_id))
        conn.commit()
    sync_scheduler_job(job_id)
    return redirect_home(info=f"{'Enabled' if new_enabled else 'Paused'} job: {job.name}", job_id=job_id)


@app.post("/jobs/{job_id}/delete", response_model=None)
def remove_job(request: Request, job_id: str, csrf_token: str = Form("")):
    require_post_auth(request, csrf_token)
    job = get_job(job_id)
    if not job:
        return redirect_home(error="Job not found")
    delete_job(job_id)
    return redirect_home(info=f"Deleted job: {job.name}")


@app.post("/cleanup", response_model=None)
def cleanup_runs_route(
        request: Request,
        csrf_token: str = Form(""),
        cleanup_days: int = Form(7),
        keep_latest: int = Form(100),
        clear_all: Optional[str] = Form(None),
):
    require_post_auth(request, csrf_token)
    try:
        if clear_all:
            deleted = cleanup_run_history(days=0, keep_latest=0)
            return redirect_home(info=f"Cleared {deleted} run record(s)")
        deleted = cleanup_run_history(days=cleanup_days, keep_latest=keep_latest)
        if deleted:
            return redirect_home(info=f"Deleted {deleted} old run record(s)")
        return redirect_home(warning="No run records matched the cleanup rule")
    except Exception as exc:
        return redirect_home(error=f"Cleanup failed: {exc}")


# -----------------------------------------------------------------------------
# Entrypoint
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    print(f"{APP_TITLE} v{APP_VERSION}")
    print(f"Data directory: {DATA_DIR}")
    print(f"Database: {DB_PATH}")
    print(f"Timezone: {APP_TIMEZONE}")
    if AUTH_TOKEN:
        print("Local token protection: enabled")
        print(f"Open: http://{APP_HOST}:{APP_PORT}/?token={AUTH_TOKEN}")
        print("API docs: http://%s:%s/docs" % (APP_HOST, APP_PORT))
    else:
        print("Local token protection: disabled")
        print(f"Open: http://{APP_HOST}:{APP_PORT}")
        print("API docs: http://%s:%s/docs" % (APP_HOST, APP_PORT))

    uvicorn.run(app, host=APP_HOST, port=APP_PORT)
