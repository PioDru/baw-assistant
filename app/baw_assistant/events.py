"""Kolejka zdarzeń trybu systemowego: UCA (wywołania z procesów BAW), EDA (zdarzenia zewnętrzne), harmonogram.

W PoC kolejka to tabela SQLite, a zdarzenia testowe wstrzykuje się z UI. Docelowo: Kafka (BAW w Cloud Pak
emituje zdarzenia BPMN przez Kafkę) i UCA w BAW.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta

from . import tools
from .db import connect, execute, init_schema, now_iso, query, query_one

log = logging.getLogger(__name__)

TEST_EVENTS = {
    "eda_document": "EDA · nowa wersja dokumentu (Protokół szkody w REK-2026-0412)",
    "uca_summary": "UCA · krok „Decyzja” prosi o podsumowanie sprawy",
    "eda_counterparty": "EDA · zmiana danych kontrahenta w systemie finansowo-księgowym",
    "scan": "Harmonogram · skan cykliczny",
}

SOURCE_LABELS = {"EDA": "EDA", "UCA": "UCA", "SCHEDULER": "Harmonogram"}


def enqueue(source: str, etype: str, payload: dict | None = None) -> int:
    return execute("INSERT INTO event_queue (created_at, source, type, payload_json) VALUES (?,?,?,?)",
                   (now_iso(), source, etype, json.dumps(payload or {}, ensure_ascii=False)))


def _apply_document_upload(instance_id: str, document: str) -> int:
    """Symulacja systemu źródłowego (repozytorium dokumentów): dokument trafia do sprawy."""
    inst = tools.get_instance(instance_id)
    sim_ts = (tools.now() - timedelta(minutes=1)).isoformat(timespec="minutes")
    version_row = query_one("SELECT MAX(version) AS v FROM documents WHERE instance_id = ? AND name = ?",
                            (instance_id, document))
    version = (version_row["v"] or 0) + 1
    missing = [d for d in inst["missing_docs"] if d != document]
    with connect() as conn:
        init_schema(conn)
        conn.execute("INSERT INTO documents (instance_id, name, version, uploaded_at) VALUES (?,?,?,?)",
                     (instance_id, document, version, sim_ts))
        conn.execute("INSERT INTO events (instance_id, ts, type, step, actor, details) VALUES (?,?,?,?,?,?)",
                     (instance_id, sim_ts, "DOCUMENT_ADDED", inst["current_step"], "Klient", document))
        conn.execute("UPDATE instances SET missing_docs_json = ?, last_change_at = ? WHERE id = ?",
                     (json.dumps(missing, ensure_ascii=False), sim_ts, instance_id))
        conn.execute("UPDATE tasks SET status = 'Zakończone', completed_at = ? WHERE instance_id = ?"
                     " AND assignee_type = 'customer' AND status = 'Otwarte'", (sim_ts, instance_id))
    return version


def inject_test_event(kind: str) -> int:
    if kind == "eda_document":
        version = _apply_document_upload("REK-2026-0412", "Protokół szkody")
        return enqueue("EDA", "document_version", {"instance_id": "REK-2026-0412", "document": "Protokół szkody",
                                                   "version": version, "system": "repozytorium dokumentów"})
    if kind == "uca_summary":
        row = query_one("SELECT id FROM instances WHERE process_id = 'REK' AND status != 'Zakończona'"
                        " AND current_step = 'Decyzja' ORDER BY id LIMIT 1") or {"id": "REK-2026-0412"}
        return enqueue("UCA", "summary_request", {"instance_id": row["id"], "step": "Decyzja",
                                                  "process": "Obsługa reklamacji"})
    if kind == "eda_counterparty":
        return enqueue("EDA", "counterparty_change", {"customer": "Kowalczyk i Wspólnicy sp. k.", "field": "NIP",
                                                      "system": "system finansowo-księgowy"})
    if kind == "scan":
        return enqueue("SCHEDULER", "scan", {})
    raise ValueError(f"Nieznany typ zdarzenia testowego: {kind}")


def pending(limit: int = 20) -> list[dict]:
    return query("SELECT * FROM event_queue WHERE status = 'Oczekuje' ORDER BY id LIMIT ?", (limit,))


def recent(limit: int = 50) -> list[dict]:
    return query("SELECT * FROM event_queue ORDER BY id DESC LIMIT ?", (limit,))


def process_pending(limit: int = 20) -> list[dict]:
    """Przetwarza oczekujące zdarzenia przez graf agentów (tryb systemowy)."""
    from .graph import handle_event

    done = []
    for ev in pending(limit):
        execute("UPDATE event_queue SET status = 'W toku' WHERE id = ? AND status = 'Oczekuje'", (ev["id"],))
        ev["payload"] = json.loads(ev["payload_json"])
        try:
            result = handle_event(ev)
            path = " → ".join(s["agent"] for s in result.get("trace", []))
            status = "Zakończone" if result.get("verdict") in ("ok", "blocked") else "Pominięte"
            execute("UPDATE event_queue SET status = ?, routed_to = ?, result = ?, processed_at = ? WHERE id = ?",
                    (status, path, result.get("answer", ""), now_iso(), ev["id"]))
            done.append({**ev, "status": status, "result": result})
        except Exception as exc:  # błąd jednego zdarzenia nie zatrzymuje kolejki
            log.exception("Błąd przetwarzania zdarzenia %s", ev["id"])
            execute("UPDATE event_queue SET status = 'Błąd', result = ?, processed_at = ? WHERE id = ?",
                    (str(exc), now_iso(), ev["id"]))
    return done
