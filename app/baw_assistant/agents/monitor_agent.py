"""Agent 4 – Agent Monitor: stan platformy i jakość danych.

- przy diagnozie sprawy sprawdza logi i integracje tej instancji,
- na pytanie administratora lub w skanie z harmonogramu wykrywa alerty (moduł monitoring),
- przy zmianie danych kontrahenta (EDA) wskazuje dotknięte aktywne sprawy.
Diagnozę i sugerowane działania bierze z instrukcji w bazie wiedzy (Qdrant).
"""
from __future__ import annotations

import time

from .. import kb, monitoring, tools
from ..db import query
from ..events import pending
from ..state import AssistantState
from .common import ACTION_INCIDENT, AGENT_MONITOR, Findings, elapsed_ms


def instance_check(f: Findings, instance_id: str) -> None:
    check = monitoring.instance_technical_check(instance_id)
    if not check["logs"]:
        f.fact("Logi techniczne z ostatnich 7 dni: brak ostrzeżeń i błędów dla tej sprawy.", "logi")
        return
    last, errors = check["last"], check["errors"]
    f.fact(f"Logi techniczne z ostatnich 7 dni dla tej sprawy – ostrzeżenia: {len(check['logs']) - len(errors)}, "
           f"błędy: {len(errors)}; najważniejszy wpis {tools.fmt_datetime(last['ts'])} ({last['component']}, "
           f"{last['level']}): {last['message']}.", "logi")
    if errors:
        f.lead("Sprawa jest zatrzymana przez błąd techniczny.", priority=0)
        f.actions.append(ACTION_INCIDENT)


def _alert_facts(f: Findings, alerts: list[dict], limit: int = 5) -> None:
    for a in alerts[:limit]:
        f.fact(f"{a['priority']} · {a['category']}: {a['description']}", "logi i dane (reguły monitoringu)")
        if a.get("diagnosis"):
            f.fact(f"Diagnoza dla alertu „{a['category']}”: {a['diagnosis']}", f"baza wiedzy: {a['kb_source']}")
        if a.get("suggested_action"):
            f.fact(f"Zalecane działanie dla alertu „{a['category']}”: {a['suggested_action']}",
                   f"baza wiedzy: {a['kb_source']}")


def platform_status(f: Findings) -> None:
    alerts = monitoring.detect_alerts()   # bez zapisu: alerty zapisuje i zgłasza skan z harmonogramu
    counts = {p: sum(a["priority"] == p for a in alerts) for p in ("Krytyczny", "Ostrzeżenie", "Informacja")}
    f.fact(f"Alerty z ostatnich 24 h: krytyczne {counts['Krytyczny']}, ostrzeżenia {counts['Ostrzeżenie']}, "
           f"informacyjne {counts['Informacja']}.", "reguły monitoringu")
    _alert_facts(f, [a for a in alerts if a["priority"] != "Informacja"])
    f.fact(f"Zdarzenia oczekujące w kolejce: {len(pending(100))}.", "kolejka zdarzeń")
    f.lead(f"Na platformie są alerty krytyczne: {counts['Krytyczny']}." if counts["Krytyczny"]
           else "Brak alertów krytycznych na platformie.", priority=0 if counts["Krytyczny"] else 1)


def scan(f: Findings) -> None:
    new = monitoring.persist_alerts(monitoring.detect_alerts())
    f.alerts.extend(new)
    f.fact(f"Skan cykliczny: nowe alerty {len(new)} (alerty już zgłoszone wcześniej pominięto).", "reguły monitoringu")
    _alert_facts(f, new)
    f.lead(f"Skan wykrył nowe alerty: {len(new)}." if new else "Skan nie wykrył nowych alertów.")


def counterparty_change(f: Findings, payload: dict) -> None:
    rows = query("SELECT i.id, i.current_step, u.name AS assignee FROM instances i LEFT JOIN users u"
                 " ON u.id = i.assignee_id WHERE i.customer = ? AND i.status != 'Zakończona' ORDER BY i.id",
                 (payload["customer"],))
    f.fact(f"Zdarzenie EDA: w systemie „{payload.get('system')}” zmieniono pole „{payload.get('field')}” "
           f"kontrahenta „{payload['customer']}”.", "zdarzenie EDA")
    f.fact(f"Aktywne sprawy tego kontrahenta ({len(rows)})"
           + (": " + "; ".join(f"{r['id']} (krok „{r['current_step']}”, {r['assignee']})" for r in rows) if rows else "")
           + ".", "instancje")
    for r in rows:
        for lg in tools.get_instance_logs(r["id"]):
            if lg["message"].startswith("Walidacja danych"):
                f.fact(f"{r['id']}: wcześniejszy błąd walidacji z {tools.fmt_datetime(lg['ts'])} – {lg['message']}.",
                       "logi")
    hits = kb.search("walidacja danych kontrahenta NIP", kind="runbook", role="admin", limit=1)
    if hits:
        action = kb.doc_section(hits[0]["doc"], "Działania")
        if action:
            f.fact(f"Zalecane działanie: {kb.short(action)}", f"baza wiedzy: {hits[0]['title']}")
    f.lead(f"Zmiana danych kontrahenta dotyczy aktywnych spraw: {len(rows)} – sprawdź ich dane przed akceptacją."
           if rows else "Zmiana danych kontrahenta nie dotyczy aktywnych spraw.")


def monitor_agent_node(state: AssistantState) -> AssistantState:
    started = time.perf_counter()
    f = Findings(AGENT_MONITOR)
    intent = state["intent"]
    if intent == "instance_diagnosis":
        instance_check(f, state["instance_id"])
        summary = "Sprawdził logi i integracje instancji"
    elif intent == "platform_status":
        platform_status(f)
        summary = "Przeanalizował logi, kolejki i dane (alerty z ostatnich 24 h)"
    elif intent == "event_scan":
        scan(f)
        summary = f"Skan cykliczny: nowe alerty {len(f.alerts)}"
    else:
        counterparty_change(f, state["event"]["payload"])
        summary = "Sprawdził sprawy dotknięte zmianą danych kontrahenta"
    return f.update(f"{summary} (faktów: {len(f.facts)}).", elapsed_ms(started))
