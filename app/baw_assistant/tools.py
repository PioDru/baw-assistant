"""Narzędzia odczytu danych (tylko odczyt). W PoC czytają SQLite, docelowo BAW REST API.

Wszystkie liczby i daty są wyliczane tutaj, w kodzie – LLM dostaje gotowe fakty.
"""
from __future__ import annotations

import json
from datetime import datetime
from statistics import median

from .config import get_settings
from .db import query, query_one

EVENT_LABELS = {
    "CREATED": "Utworzenie sprawy",
    "STEP_ENTERED": "Wejście w krok",
    "TASK_CREATED": "Utworzenie zadania",
    "TASK_COMPLETED": "Zakończenie zadania",
    "DOCUMENT_ADDED": "Dodanie dokumentu",
    "DOCUMENT_REQUESTED": "Prośba o dokument",
    "COMMENT": "Komentarz",
    "SUSPENDED": "Wstrzymanie",
    "COMPLETED": "Zakończenie sprawy",
}


def now() -> datetime:
    return get_settings().sim_now


def fmt_date(value: str | datetime | None) -> str:
    if not value:
        return "—"
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return dt.strftime("%d.%m.%Y")


def fmt_datetime(value: str | datetime | None) -> str:
    if not value:
        return "—"
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return dt.strftime("%d.%m.%Y %H:%M")


def days_between(start: str | datetime, end: str | datetime | None = None) -> int:
    s = start if isinstance(start, datetime) else datetime.fromisoformat(start)
    e = (end if isinstance(end, datetime) else datetime.fromisoformat(end)) if end else now()
    return (e.date() - s.date()).days


def get_instance(instance_id: str) -> dict | None:
    row = query_one(
        "SELECT i.*, p.name AS process_name, p.sla_days, u.name AS assignee_name"
        " FROM instances i JOIN processes p ON p.id = i.process_id"
        " LEFT JOIN users u ON u.id = i.assignee_id WHERE i.id = ?",
        (instance_id,),
    )
    if row:
        row["missing_docs"] = json.loads(row.pop("missing_docs_json") or "[]")
    return row


def get_tasks(instance_id: str) -> list[dict]:
    rows = query(
        "SELECT t.*, u.name AS user_name FROM tasks t LEFT JOIN users u ON u.id = t.assignee"
        " WHERE t.instance_id = ? ORDER BY t.created_at", (instance_id,))
    for r in rows:
        r["assignee_label"] = r["user_name"] or r["assignee"]
    return rows


def get_events(instance_id: str) -> list[dict]:
    rows = query("SELECT * FROM events WHERE instance_id = ? ORDER BY ts, id", (instance_id,))
    for r in rows:
        r["label"] = EVENT_LABELS.get(r["type"], r["type"])
    return rows


def get_documents(instance_id: str) -> list[dict]:
    return query("SELECT name, version, uploaded_at FROM documents WHERE instance_id = ? ORDER BY uploaded_at",
                 (instance_id,))


def get_instance_logs(instance_id: str, days: int = 7) -> list[dict]:
    """Ostrzeżenia i błędy techniczne dotyczące instancji z ostatnich dni."""
    since = now().toordinal() - days
    rows = query("SELECT * FROM logs WHERE instance_id = ? AND level IN ('WARN','ERROR') ORDER BY ts",
                 (instance_id,))
    return [r for r in rows if datetime.fromisoformat(r["ts"]).toordinal() >= since]


def similar_cases_stats(process_id: str, step: str, missing_doc: str) -> dict:
    """Statystyki zakończonych spraw, w których w tym samym kroku brakowało tego samego dokumentu.

    Zwraca tylko agregaty (bez identyfikatorów spraw), więc nie ujawnia danych spoza uprawnień.
    """
    rows = query(
        "SELECT e.instance_id, e.type, e.ts, e.details FROM events e"
        " JOIN instances i ON i.id = e.instance_id"
        " WHERE i.process_id = ? AND i.status = 'Zakończona' AND e.step = ?"
        " AND e.type IN ('DOCUMENT_REQUESTED','COMMENT','DOCUMENT_ADDED') ORDER BY e.instance_id, e.ts",
        (process_id, step),
    )
    by_instance: dict[str, dict] = {}
    for r in rows:
        d = by_instance.setdefault(r["instance_id"], {})
        if r["type"] == "DOCUMENT_REQUESTED" and missing_doc in (r["details"] or ""):
            d["requested"] = r["ts"]
        elif r["type"] == "COMMENT" and "Przypomnienie" in (r["details"] or ""):
            d["reminder"] = True
        elif r["type"] == "DOCUMENT_ADDED" and missing_doc in (r["details"] or ""):
            d["added"] = r["ts"]
    cases = [d for d in by_instance.values() if "requested" in d and "added" in d]

    def wait_days(d: dict) -> float:
        delta = datetime.fromisoformat(d["added"]) - datetime.fromisoformat(d["requested"])
        return delta.total_seconds() / 86400

    with_rem = [wait_days(d) for d in cases if d.get("reminder")]
    without_rem = [wait_days(d) for d in cases if not d.get("reminder")]
    return {
        "count": len(cases),
        "with_reminder": len(with_rem),
        "median_days_with_reminder": round(median(with_rem), 1) if with_rem else None,
        "median_days_without_reminder": round(median(without_rem), 1) if without_rem else None,
    }


def active_instances_for(user_clause_ids: list[str]) -> list[dict]:
    """Szczegóły aktywnych instancji z listy (lista pochodzi już z filtra uprawnień)."""
    if not user_clause_ids:
        return []
    marks = ",".join("?" for _ in user_clause_ids)
    rows = query(
        "SELECT i.id, p.name AS process_name, i.current_step, i.sla_due, i.last_change_at,"
        " u.name AS assignee_name FROM instances i JOIN processes p ON p.id = i.process_id"
        f" LEFT JOIN users u ON u.id = i.assignee_id WHERE i.id IN ({marks}) ORDER BY i.sla_due",
        tuple(user_clause_ids),
    )
    for r in rows:
        r["days_to_sla"] = days_between(now(), r["sla_due"])
    return rows
