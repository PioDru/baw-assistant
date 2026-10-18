"""Dostęp do bazy SQLite z danymi syntetycznymi i śladem przebiegów."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, manager_id TEXT
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL, team_id TEXT, email TEXT
);
CREATE TABLE IF NOT EXISTS processes (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, sla_days INTEGER NOT NULL,
    owner_id TEXT, team_id TEXT, steps_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS instances (
    id TEXT PRIMARY KEY, process_id TEXT NOT NULL, status TEXT NOT NULL,
    current_step TEXT, assignee_id TEXT, team_id TEXT, customer TEXT, channel TEXT,
    created_at TEXT NOT NULL, sla_due TEXT NOT NULL, last_change_at TEXT NOT NULL,
    completed_at TEXT, missing_docs_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, instance_id TEXT NOT NULL, name TEXT NOT NULL,
    assignee_type TEXT NOT NULL, assignee TEXT NOT NULL, status TEXT NOT NULL,
    created_at TEXT NOT NULL, due_at TEXT, completed_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, instance_id TEXT NOT NULL, ts TEXT NOT NULL,
    type TEXT NOT NULL, step TEXT, actor TEXT, details TEXT
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT, instance_id TEXT NOT NULL, name TEXT NOT NULL,
    version INTEGER NOT NULL, uploaded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, level TEXT NOT NULL,
    component TEXT NOT NULL, instance_id TEXT, message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, user_id TEXT, role TEXT,
    question TEXT, intent TEXT, path TEXT, verdict TEXT, latency_ms INTEGER, answer TEXT
);
CREATE TABLE IF NOT EXISTS event_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, source TEXT NOT NULL, type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'Oczekuje', routed_to TEXT,
    result TEXT, processed_at TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, fingerprint TEXT UNIQUE NOT NULL,
    event_ts TEXT NOT NULL, priority TEXT NOT NULL, category TEXT NOT NULL, component TEXT,
    description TEXT NOT NULL, diagnosis TEXT, suggested_action TEXT, evidence TEXT,
    channels_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'Nowy'
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, channel TEXT NOT NULL, recipient TEXT NOT NULL,
    subject TEXT NOT NULL, body TEXT NOT NULL, origin TEXT, status TEXT NOT NULL DEFAULT 'Symulowane'
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_config (name TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS action_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, thread_id TEXT, user_id TEXT, action TEXT,
    instance_id TEXT, status TEXT, channel TEXT, recipient TEXT, text TEXT
);
CREATE INDEX IF NOT EXISTS ix_instances_assignee ON instances(assignee_id);
CREATE INDEX IF NOT EXISTS ix_instances_team ON instances(team_id);
CREATE INDEX IF NOT EXISTS ix_tasks_instance ON tasks(instance_id);
CREATE INDEX IF NOT EXISTS ix_events_instance ON events(instance_id);
CREATE INDEX IF NOT EXISTS ix_docs_instance ON documents(instance_id);
CREATE INDEX IF NOT EXISTS ix_logs_instance ON logs(instance_id);
"""


def db_path() -> Path:
    return get_settings().db_path


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL: aplikacja i worker harmonogramu korzystają z tej samej bazy jednocześnie.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def query(sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def query_one(sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple | dict = ()) -> int:
    """Zapis; zwraca id ostatnio wstawionego wiersza."""
    with connect() as conn:
        init_schema(conn)
        cur = conn.execute(sql, params)
        return cur.lastrowid


def now_iso() -> str:
    """Czas rzeczywisty dla zapisów operacyjnych (kolejka, alerty, powiadomienia)."""
    return datetime.now().isoformat(timespec="seconds")


DEFAULT_SETTINGS = {
    "integration_errors_15min": "2",
    "queue_delay_s": "300",
    "step_growth_pct": "50",
    "slow_response_warnings_24h": "5",
    "scan_interval_min": "5",
}


def get_setting(key: str) -> str:
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else DEFAULT_SETTINGS[key]


def set_setting(key: str, value: str) -> None:
    execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)))


def agent_enabled(name: str) -> bool:
    row = query_one("SELECT enabled FROM agent_config WHERE name = ?", (name,))
    return bool(row["enabled"]) if row else True


def set_agent_enabled(name: str, enabled: bool) -> None:
    execute("INSERT INTO agent_config (name, enabled) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET enabled = excluded.enabled", (name, int(enabled)))


def database_ready() -> bool:
    p = db_path()
    if not p.exists():
        return False
    try:
        row = query_one("SELECT COUNT(*) AS n FROM instances")
        return bool(row and row["n"] > 0)
    except sqlite3.Error:
        return False


def log_run(user_id: str, role: str, question: str, intent: str, path: list[str],
            verdict: str, latency_ms: int, answer: str) -> None:
    with connect() as conn:
        init_schema(conn)
        conn.execute(
            "INSERT INTO agent_runs (ts, user_id, role, question, intent, path, verdict, latency_ms, answer)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), user_id, role, question, intent,
             json.dumps(path, ensure_ascii=False), verdict, latency_ms, answer),
        )
