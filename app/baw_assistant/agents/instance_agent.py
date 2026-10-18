"""Agent 2 – Agent Instancji: fakty o sprawach.

Zbiera fakty narzędziami odczytu (liczby i daty liczy kod) oraz wskazówki z opisu procesu
w bazie wiedzy (Qdrant). Odpowiedź składa Redaktor-Weryfikator.
Obsługuje pytania (diagnoza, szczegóły, moje sprawy) i zdarzenia (nowa wersja dokumentu – EDA,
prośba procesu o podsumowanie sprawy – UCA).
"""
from __future__ import annotations

import time

from .. import kb, tools
from ..data_generator import DOC_REQUIRED
from ..rbac import get_user, visible_active_instances
from ..state import AssistantState
from .common import (ACTION_ESCALATE_SLA, ACTION_PING_DEPT, ACTION_REMIND, AGENT_INSTANCE, Findings, days_dec,
                     days_word, elapsed_ms, left)


def _core(f: Findings, inst: dict) -> None:
    f.fact(f"Sprawa {inst['id']} ({inst['process_name']}) ma status „{inst['status']}” i jest w kroku "
           f"„{inst['current_step']}”. Przypisana osoba: {inst['assignee_name']}.", "instancje")
    d = tools.days_between(tools.now(), inst["sla_due"])
    if d >= 0:
        f.fact(f"Termin SLA: {tools.fmt_date(inst['sla_due'])}, {left(d)}.", "instancje")
    else:
        f.fact(f"Termin SLA: {tools.fmt_date(inst['sla_due'])} – przekroczony o {-d} {days_word(-d)}.", "instancje")


def _open_tasks(f: Findings, instance_id: str) -> tuple[dict | None, dict | None]:
    waiting_customer = waiting_dept = None
    for t in tools.get_tasks(instance_id):
        if t["status"] not in ("Otwarte", "Wstrzymane"):
            continue
        overdue = ""
        if t["due_at"] and tools.days_between(t["due_at"]) > 0:
            d = tools.days_between(t["due_at"])
            overdue = f" – przekroczony o {d} {days_word(d)}"
        f.fact(f"Zadanie „{t['name']}” ({t['status'].lower()}) przypisane do: {t['assignee_label']}, "
               f"utworzone {tools.fmt_date(t['created_at'])}, termin {tools.fmt_date(t['due_at'])}{overdue}.", "zadania")
        if t["assignee_type"] == "customer":
            waiting_customer = t
        elif t["assignee_type"] == "department":
            waiting_dept = t
    return waiting_customer, waiting_dept


def _process_guidance(f: Findings, inst: dict, role: str) -> None:
    hits = kb.search(f"{inst['process_name']} {inst['current_step']}", kind="process",
                     process_id=inst["process_id"], role=role, limit=1)
    if hits and hits[0]["section"] == inst["current_step"]:
        f.fact(f"Opis procesu, krok „{hits[0]['section']}”: {kb.short(hits[0]['text'])}",
               f"baza wiedzy: {hits[0]['title']}")


def diagnosis(f: Findings, instance_id: str, role: str) -> None:
    inst = tools.get_instance(instance_id)
    _core(f, inst)
    idle = tools.days_between(inst["last_change_at"])
    ago = "dzisiaj" if idle == 0 else f"czyli {idle} {days_word(idle)} temu"
    f.fact(f"Ostatnia zmiana w sprawie: {tools.fmt_datetime(inst['last_change_at'])}, {ago}.", "historia zdarzeń")
    waiting_customer, waiting_dept = _open_tasks(f, instance_id)

    requested = [e for e in tools.get_events(instance_id) if e["type"] == "DOCUMENT_REQUESTED"]
    if inst["missing_docs"]:
        when = f" (prośba wysłana {tools.fmt_date(requested[-1]['ts'])})" if requested else ""
        f.fact(f"Brakujące dokumenty: {', '.join(inst['missing_docs'])}{when}.", "dokumenty")
    docs = tools.get_documents(instance_id)
    if docs:
        f.fact("Dokumenty w sprawie: " + ", ".join(d["name"] for d in docs) + ".", "dokumenty")
    for doc in inst["missing_docs"]:
        s = tools.similar_cases_stats(inst["process_id"], inst["current_step"], doc)
        if s["count"] and s["median_days_with_reminder"] is not None and s["median_days_without_reminder"] is not None:
            f.fact(f"Podobne zakończone sprawy (brak dokumentu „{doc}” w tym kroku): {s['count']}. Mediana "
                   f"oczekiwania na dokument: {days_dec(s['median_days_with_reminder'])} po przypomnieniu "
                   f"telefonicznym (spraw z przypomnieniem: {s['with_reminder']}) i "
                   f"{days_dec(s['median_days_without_reminder'])} bez przypomnienia.", "historia podobnych spraw")
    _process_guidance(f, inst, role)

    if inst["missing_docs"] and waiting_customer:
        f.lead("Sprawa czeka na brakujący dokument od klienta.")
        f.actions.append(ACTION_REMIND)
    elif waiting_dept:
        f.lead(f"Sprawa czeka na zadanie po stronie: {waiting_dept['assignee_label']}.")
        f.actions.append(ACTION_PING_DEPT)
    else:
        f.lead(f"Sprawa jest w toku w kroku „{inst['current_step']}”.", priority=2)
    if tools.days_between(tools.now(), inst["sla_due"]) <= 3:
        f.actions.append(ACTION_ESCALATE_SLA)


def details(f: Findings, instance_id: str, limit_events: int = 8) -> None:
    inst = tools.get_instance(instance_id)
    events = tools.get_events(instance_id)
    docs = tools.get_documents(instance_id)
    _core(f, inst)
    if docs:
        listing = "; ".join(f"{d['name']} (wersja {d['version']}, {tools.fmt_date(d['uploaded_at'])})" for d in docs)
        f.fact(f"Dokumenty w sprawie ({len(docs)}): {listing}.", "dokumenty")
    if inst["missing_docs"]:
        f.fact(f"Brakujące dokumenty: {', '.join(inst['missing_docs'])}.", "dokumenty")
    people = sorted({e["actor"] for e in events if e["actor"] not in ("Klient", "System", None)})
    if people:
        f.fact("Osoby, które pracowały przy sprawie: " + ", ".join(people) + ".", "historia zdarzeń")
    lines = "; ".join(f"{tools.fmt_datetime(e['ts'])} {e['label']}" + (f" – {e['details']}" if e["details"] else "")
                      for e in events[-limit_events:])
    f.fact(f"Historia sprawy: {len(events)} zdarzeń, ostatnie: {lines}.", "historia zdarzeń")
    f.lead(f"Szczegóły sprawy {instance_id}:", priority=2)


def my_work(f: Findings, user_id: str) -> None:
    user = get_user(user_id)
    rows = tools.active_instances_for([v["id"] for v in visible_active_instances(user)])
    overdue = [r for r in rows if r["days_to_sla"] < 0]
    risky = [r for r in rows if 0 <= r["days_to_sla"] <= 2]
    f.fact(f"Aktywne sprawy w zakresie uprawnień: {len(rows)}; po terminie SLA: {len(overdue)}; "
           f"z terminem w ciągu 2 dni: {len(risky)}.", "instancje")
    for r in rows[:8]:
        due = (f"po terminie o {-r['days_to_sla']} {days_word(-r['days_to_sla'])}" if r["days_to_sla"] < 0
               else left(r["days_to_sla"]))
        who = "" if user.role == "pracownik" else f", przypisana: {r['assignee_name']}"
        f.fact(f"{r['id']} ({r['process_name']}) – krok „{r['current_step']}”, termin SLA "
               f"{tools.fmt_date(r['sla_due'])} ({due}){who}.", "instancje")
    f.lead("Najpilniejsze są sprawy po terminie SLA." if overdue
           else "Nie masz spraw po terminie SLA." if rows else "Nie masz aktywnych spraw.")


def document_event(f: Findings, payload: dict) -> None:
    iid = payload["instance_id"]
    inst = tools.get_instance(iid)
    f.fact(f"Zdarzenie EDA: w systemie „{payload.get('system', 'repozytorium dokumentów')}” pojawiła się wersja "
           f"{payload['version']} dokumentu „{payload['document']}” dla sprawy {iid}.", "zdarzenie EDA")
    _core(f, inst)
    present = {d["name"] for d in tools.get_documents(iid)}
    missing = [d for d in DOC_REQUIRED.get(inst["process_id"], []) if d not in present]
    f.fact("Dokumenty w sprawie: " + ", ".join(sorted(present)) + ".", "dokumenty")
    if missing:
        f.fact("Nadal brakuje: " + ", ".join(missing) + ".", "dokumenty")
        f.lead(f"Sprawa {iid} nadal nie ma kompletu dokumentów.")
    else:
        f.fact(f"Komplet wymaganych dokumentów dla kroku „{inst['current_step']}”: tak.", "dokumenty")
        f.lead(f"Klient dostarczył brakujący dokument – sprawa {iid} może przejść dalej.")


def summary_event(f: Findings, payload: dict) -> None:
    iid = payload["instance_id"]
    f.fact(f"Wywołanie UCA: krok „{payload.get('step', '')}” procesu prosi o podsumowanie sprawy {iid}.",
           "zdarzenie UCA")
    details(f, iid, limit_events=5)
    _open_tasks(f, iid)
    f.leads.clear()
    f.lead(f"Podsumowanie sprawy {iid} dla kroku „{payload.get('step', '')}”.")


def instance_agent_node(state: AssistantState) -> AssistantState:
    started = time.perf_counter()
    f = Findings(AGENT_INSTANCE)
    intent = state["intent"]
    if intent == "instance_diagnosis":
        diagnosis(f, state["instance_id"], state["role"])
    elif intent == "instance_details":
        details(f, state["instance_id"])
    elif intent == "my_work":
        my_work(f, state["user_id"])
    elif intent == "event_document":
        document_event(f, state["event"]["payload"])
    elif intent == "event_summary":
        summary_event(f, state["event"]["payload"])
    kb_used = any(x["source"].startswith("baza wiedzy") for x in f.facts)
    return f.update(f"Zebrał fakty: {len(f.facts)} (instancje, zadania, dokumenty, historia"
                    f"{', opis procesu z bazy wiedzy' if kb_used else ''}).", elapsed_ms(started))
