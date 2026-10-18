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
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
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
