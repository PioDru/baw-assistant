"""PoC v2: Agent Analityk, Agent Monitor, kolejka zdarzeń, powiadomienia, akcje z zatwierdzeniem, baza wiedzy."""
from __future__ import annotations

import pandas as pd
import pytest

from baw_assistant import actions, analytics, db, events, kb, monitoring, notifications
from baw_assistant.agents.common import ACTION_REBALANCE, ACTION_REMIND
from baw_assistant.graph import ask
from baw_assistant.rbac import get_user


# --- Agent Analityk ----------------------------------------------------------------

def test_team_overview_matches_raw_data():
    t = analytics.team_overview(get_user("u_marek"))
    raw = pd.DataFrame(db.query("SELECT id, assignee_id, sla_due FROM instances WHERE team_id = 'T_REK' "
                                "AND status != 'Zakończona'"))
    assert t["active"] == len(raw)
    assert t["overdue"] == int((raw["sla_due"] < "2026-10-18").sum())
    assert t["per_assignee"][0]["name"] == "Jan Mazur" and t["per_assignee"][0]["overdue"] == 3
    assert t["overdue_waiting"] == {"Dział techniczny": 3}


def test_process_kpis_bottleneck_and_channels():
    k = analytics.process_kpis("REK", 84)
    assert k["bottleneck"]["step"] == "Weryfikacja dokumentów"
    shares = k["missing_doc_by_channel"]
    assert shares["telefon"] > shares["online"]          # tak skonstruowano dane
    assert k["completed"] == int(k["weekly"]["completed"].sum())


# --- Agent Monitor -----------------------------------------------------------------

def test_alerts_detected_with_kb_diagnosis():
    alerts = {a["category"]: a for a in monitoring.detect_alerts()}
    outage = alerts["Awaria integracji"]
    assert outage["priority"] == "Krytyczny" and "repozytorium dokumentów" in outage["description"]
    assert "restartem bramy" in outage["diagnosis"] and outage["channels"] == ["Zadanie BAW", "E-mail", "Teams"]
    assert alerts["Dostępność"]["priority"] == "Ostrzeżenie"
    assert alerts["Błąd danych"]["audience"] == "owner"


def test_threshold_change_suppresses_alert(fresh_db):
    db.set_setting("integration_errors_15min", "5")
    assert "Awaria integracji" not in {a["category"] for a in monitoring.detect_alerts()}
    db.set_setting("integration_errors_15min", "2")


def test_scan_event_notifies_once(fresh_db):
    events.inject_test_event("scan")
    first = events.process_pending()[0]
    assert first["status"] == "Zakończone" and first["result"]["notifications"]
    sent = len(notifications.recent(200))
    events.inject_test_event("scan")
    second = events.process_pending()[0]
    assert "nie wykrył nowych alertów" in second["result"]["answer"]
    assert len(notifications.recent(200)) == sent       # deduplikacja alertów


def test_document_event_updates_case_and_notifies_assignee(fresh_db):
    events.inject_test_event("eda_document")
    done = events.process_pending()[0]
    assert "może przejść dalej" in done["result"]["answer"]
    notes = [n for n in notifications.recent(10) if n["recipient"] == "Anna Kowalska"]
    assert {n["channel"] for n in notes} == {"Teams", "Zadanie BAW"}
    assert "[F" not in notes[0]["body"]                 # odnośniki tylko w UI


def test_counterparty_event_routes_to_monitor(fresh_db):
    events.inject_test_event("eda_counterparty")
    done = events.process_pending()[0]
    assert done["result"]["agents"] == ["Agent Monitor"]
    assert "FAK-2026-1162" in done["result"]["answer"]


def test_uca_summary_event(fresh_db):
    events.inject_test_event("uca_summary")
    done = events.process_pending()[0]
    assert done["result"]["intent"] == "event_summary" and done["status"] == "Zakończone"


# --- Akcje z zatwierdzeniem (interrupt) ----------------------------------------------

def test_action_waits_for_approval_then_sends(fresh_db):
    pending = actions.start_action(ACTION_REMIND, "u_anna", "REK-2026-0412")
    assert "Protokół szkody" in pending["draft"] and pending["channel"] == "E-mail"
    before = len(notifications.recent(200))
    result = actions.resume_action(pending["thread_id"], True, "Treść poprawiona przez użytkownika")
    assert result["status"].startswith("Zatwierdzona")
    last = notifications.recent(1)[0]
    assert last["body"] == "Treść poprawiona przez użytkownika" and len(notifications.recent(200)) == before + 1


def test_rejected_action_sends_nothing(fresh_db):
    pending = actions.start_action(ACTION_REBALANCE, "u_marek")
    before = len(notifications.recent(200))
    assert actions.resume_action(pending["thread_id"], False)["status"] == "Odrzucona"
    assert len(notifications.recent(200)) == before


def test_action_respects_permissions():
    with pytest.raises(PermissionError):
        actions.start_action(ACTION_REMIND, "u_anna", "REK-2026-0377")


# --- Baza wiedzy i rejestr agentów ---------------------------------------------------

def test_kb_filters_by_role():
    assert kb.search("Connection refused ecm-gateway", kind="runbook", role="admin")
    assert kb.search("Connection refused ecm-gateway", kind="runbook", role="pracownik") == []


def test_disabled_agent(fresh_db):
    db.set_agent_enabled("Agent Analityk", False)
    result = ask("Kto ma najwięcej przeterminowanych zadań w zespole?", "u_marek", "kierownik")
    assert result["decision"] == "disabled" and "wyłączony" in result["answer"]
    db.set_agent_enabled("Agent Monitor", False)
    diag = ask("Dlaczego ta sprawa stoi?", "u_anna", "pracownik", "REK-2026-0412")
    assert diag["agents"] == ["Agent Instancji"]       # opcjonalny agent pominięty, odpowiedź działa
    db.set_agent_enabled("Agent Analityk", True)
    db.set_agent_enabled("Agent Monitor", True)


def test_kb_self_heals_after_collection_loss(monkeypatch):
    monkeypatch.setattr(kb, "RETRY_EVERY_S", 0)
    kb.client().delete_collection(kb.collection_name())
    kb.search("ecm-gateway", kind="runbook", role="admin")          # pierwsza próba wykrywa brak kolekcji
    assert kb.search("Connection refused ecm-gateway", kind="runbook", role="admin")
    assert kb.status()["points"] == len(kb.load_chunks())


def test_openai_embedding_failure_falls_back_to_local(monkeypatch):
    def broken():
        raise RuntimeError("brak dostępu do API embeddingów")
    monkeypatch.setenv("EMBEDDING_MODEL", "openai")
    monkeypatch.setattr(kb, "_openai_embedder", broken)
    try:
        assert kb.ensure_index(attempts=1) == len(kb.load_chunks())
        assert kb.embedding_mode() == "hash"
    finally:
        kb._mode_override = None


def test_unreachable_qdrant_raises_clear_error(monkeypatch):
    monkeypatch.setattr(kb, "client", lambda: (_ for _ in ()).throw(ConnectionError("refused")))
    with pytest.raises(ConnectionError, match="Qdrant niedostępny"):
        kb.wait_for_qdrant(attempts=2, delay=0)
