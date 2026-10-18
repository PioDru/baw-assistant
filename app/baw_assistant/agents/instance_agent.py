"""Agent 2 – Agent Instancji: fakty o sprawach.

Zbiera fakty narzędziami odczytu (liczby i daty liczy kod), a potem LLM pisze z nich
odpowiedź z odnośnikami [F#]. W trybie mock (lub przy błędzie modelu) odpowiedź
składa szablon – też wyłącznie z faktów.
"""
from __future__ import annotations

import logging
import time

from .. import tools
from ..llm import get_llm
from ..rbac import get_user, visible_active_instances
from ..state import AssistantState, Fact

log = logging.getLogger(__name__)

ACTION_REMIND = "Przygotuj przypomnienie do klienta"
ACTION_ESCALATE_SLA = "Zgłoś ryzyko SLA kierownikowi"
ACTION_INCIDENT = "Zgłoś incydent techniczny administratorowi"
ACTION_PING_DEPT = "Wyślij monit do działu realizującego zadanie"


def _num(value: float) -> str:
    """Liczba w zapisie polskim: 1.7 → „1,7”."""
    return f"{value:.1f}".replace(".", ",") if value % 1 else str(int(value))


def _days_word(n: int) -> str:
    return "dzień" if abs(n) == 1 else "dni"


def _left(n: int) -> str:
    """„został 1 dzień”, „zostały 3 dni”, „zostało 5 dni”."""
    if n == 1:
        return "został 1 dzień"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"zostały {n} dni"
    return f"zostało {n} dni"


def _days_dec(value: float) -> str:
    """Mediana w dniach: „1,8 dnia”, „4 dni”."""
    return f"{_num(value)} {'dnia' if value % 1 else _days_word(int(value))}"


class FactBook:
    def __init__(self) -> None:
        self.facts: list[Fact] = []

    def add(self, text: str, source: str) -> str:
        fid = f"F{len(self.facts) + 1}"
        self.facts.append({"id": fid, "text": text, "source": source})
        return fid


# --- zbieranie faktów -------------------------------------------------------------

def _instance_core(book: FactBook, inst: dict) -> None:
    book.add(f"Sprawa {inst['id']} ({inst['process_name']}) ma status „{inst['status']}” i jest w kroku "
             f"„{inst['current_step']}”. Przypisana osoba: {inst['assignee_name']}.", "instancje")
    left = tools.days_between(tools.now(), inst["sla_due"])
    if left >= 0:
        book.add(f"Termin SLA: {tools.fmt_date(inst['sla_due'])}, {_left(left)}.", "instancje")
    else:
        book.add(f"Termin SLA: {tools.fmt_date(inst['sla_due'])} – przekroczony o {-left} {_days_word(-left)}.",
                 "instancje")


def diagnosis_facts(instance_id: str) -> tuple[list[Fact], str, list[str]]:
    book = FactBook()
    inst = tools.get_instance(instance_id)
    tasks = tools.get_tasks(instance_id)
    events = tools.get_events(instance_id)
    docs = tools.get_documents(instance_id)
    logs = tools.get_instance_logs(instance_id)
    actions: list[str] = []

    _instance_core(book, inst)
    idle = tools.days_between(inst["last_change_at"])
    ago = "dzisiaj" if idle == 0 else f"czyli {idle} {_days_word(idle)} temu"
    book.add(f"Ostatnia zmiana w sprawie: {tools.fmt_datetime(inst['last_change_at'])}, {ago}.", "historia zdarzeń")

    open_tasks = [t for t in tasks if t["status"] in ("Otwarte", "Wstrzymane")]
    waiting_customer = waiting_dept = None
    for t in open_tasks:
        overdue = ""
        if t["due_at"] and tools.days_between(t["due_at"]) > 0:
            d = tools.days_between(t["due_at"])
            overdue = f" – przekroczony o {d} {_days_word(d)}"
        book.add(f"Zadanie „{t['name']}” ({t['status'].lower()}) przypisane do: {t['assignee_label']}, "
                 f"utworzone {tools.fmt_date(t['created_at'])}, termin {tools.fmt_date(t['due_at'])}{overdue}.",
                 "zadania")
        if t["assignee_type"] == "customer":
            waiting_customer = t
        elif t["assignee_type"] == "department":
            waiting_dept = t

    requested = [e for e in events if e["type"] == "DOCUMENT_REQUESTED"]
    if inst["missing_docs"]:
        when = f" (prośba wysłana {tools.fmt_date(requested[-1]['ts'])})" if requested else ""
        book.add(f"Brakujące dokumenty: {', '.join(inst['missing_docs'])}{when}.", "dokumenty")
    if docs:
        book.add("Dokumenty w sprawie: " + ", ".join(d["name"] for d in docs) + ".", "dokumenty")

    errors = [lg for lg in logs if lg["level"] == "ERROR"]
    if logs:
        last = errors[-1] if errors else logs[-1]
        book.add(f"Logi techniczne z ostatnich 7 dni dla tej sprawy – ostrzeżenia: {len(logs) - len(errors)}, "
                 f"błędy: {len(errors)}; najważniejszy wpis {tools.fmt_datetime(last['ts'])} "
                 f"({last['component']}, {last['level']}): {last['message']}.", "logi")
    else:
        book.add("Logi techniczne z ostatnich 7 dni: brak ostrzeżeń i błędów dla tej sprawy.", "logi")

    for doc in inst["missing_docs"]:
        stats = tools.similar_cases_stats(inst["process_id"], inst["current_step"], doc)
        if stats["count"] and stats["median_days_with_reminder"] is not None \
                and stats["median_days_without_reminder"] is not None:
            book.add(f"Podobne zakończone sprawy (brak dokumentu „{doc}” w tym kroku): {stats['count']}. Mediana "
                     f"oczekiwania na dokument: {_days_dec(stats['median_days_with_reminder'])} po przypomnieniu "
                     f"telefonicznym (spraw z przypomnieniem: {stats['with_reminder']}) i "
                     f"{_days_dec(stats['median_days_without_reminder'])} bez przypomnienia.",
                     "historia podobnych spraw")

    # Wniosek główny (lead) i akcje sugerowane – reguły na faktach.
    left = tools.days_between(tools.now(), inst["sla_due"])
    if errors:
        lead = "Sprawa jest zatrzymana przez błąd techniczny."
        actions.append(ACTION_INCIDENT)
    elif inst["missing_docs"] and waiting_customer:
        lead = "Sprawa czeka na brakujący dokument od klienta."
        actions.append(ACTION_REMIND)
    elif waiting_dept:
        lead = f"Sprawa czeka na zadanie po stronie: {waiting_dept['assignee_label']}."
        actions.append(ACTION_PING_DEPT)
    else:
        lead = f"Sprawa jest w toku w kroku „{inst['current_step']}”."
    if left <= 3:
        actions.append(ACTION_ESCALATE_SLA)
    return book.facts, lead, actions


def details_facts(instance_id: str) -> tuple[list[Fact], str, list[str]]:
    book = FactBook()
    inst = tools.get_instance(instance_id)
    events = tools.get_events(instance_id)
    docs = tools.get_documents(instance_id)
    _instance_core(book, inst)
    if docs:
        listing = "; ".join(f"{d['name']} (wersja {d['version']}, {tools.fmt_date(d['uploaded_at'])})" for d in docs)
        book.add(f"Dokumenty w sprawie ({len(docs)}): {listing}.", "dokumenty")
    if inst["missing_docs"]:
        book.add(f"Brakujące dokumenty: {', '.join(inst['missing_docs'])}.", "dokumenty")
    people = sorted({e["actor"] for e in events if e["actor"] not in ("Klient", "System", None)})
    if people:
        book.add("Osoby, które pracowały przy sprawie: " + ", ".join(people) + ".", "historia zdarzeń")
    recent = events[-8:]
    lines = "; ".join(f"{tools.fmt_datetime(e['ts'])} {e['label']}"
                      + (f" – {e['details']}" if e["details"] else "") for e in recent)
    book.add(f"Historia sprawy: {len(events)} zdarzeń, ostatnie: {lines}.", "historia zdarzeń")
    return book.facts, f"Szczegóły sprawy {instance_id}:", []


def my_work_facts(user_id: str) -> tuple[list[Fact], str, list[str]]:
    book = FactBook()
    user = get_user(user_id)
    visible = visible_active_instances(user)
    rows = tools.active_instances_for([v["id"] for v in visible])
    overdue = [r for r in rows if r["days_to_sla"] < 0]
    risky = [r for r in rows if 0 <= r["days_to_sla"] <= 2]
    book.add(f"Aktywne sprawy w zakresie uprawnień: {len(rows)}; po terminie SLA: {len(overdue)}; "
             f"z terminem w ciągu 2 dni: {len(risky)}.", "instancje")
    for r in rows[:8]:
        due = (f"po terminie o {-r['days_to_sla']} {_days_word(-r['days_to_sla'])}" if r["days_to_sla"] < 0
               else _left(r["days_to_sla"]))
        who = "" if user.role == "pracownik" else f", przypisana: {r['assignee_name']}"
        book.add(f"{r['id']} ({r['process_name']}) – krok „{r['current_step']}”, termin SLA "
                 f"{tools.fmt_date(r['sla_due'])} ({due}){who}.", "instancje")
    lead = ("Najpilniejsze są sprawy po terminie SLA." if overdue
            else "Nie masz spraw po terminie SLA." if rows else "Nie masz aktywnych spraw.")
    return book.facts, lead, []


# --- pisanie odpowiedzi -------------------------------------------------------------

DRAFT_PROMPT = """Jesteś Agentem Instancji w asystencie operacyjnym IBM BAW. Odpowiadasz po polsku,
zwięźle (do ok. 120 słów, potem opcjonalnie lista kroków).
Zasady:
- Korzystaj WYŁĄCZNIE z faktów z listy. Nie dodawaj liczb, dat, nazw ani numerów spraw spoza faktów.
- Po każdym zdaniu z liczbą, datą lub nazwą dodaj odnośnik do faktu, np. [F2].
- Zacznij od bezpośredniej odpowiedzi na pytanie.
- Jeśli podano akcje sugerowane, zakończ sekcją „Sugerowane kroki:” z listą numerowaną tylko tych akcji."""


def template_answer(lead: str, facts: list[Fact], actions: list[str]) -> str:
    parts = [lead, ""]
    parts += [f"- {f['text']} [{f['id']}]" for f in facts]
    if actions:
        parts += ["", "Sugerowane kroki:"] + [f"{i}. {a}" for i, a in enumerate(actions, 1)]
    return "\n".join(parts)


def write_draft(question: str, role_label: str, lead: str, facts: list[Fact], actions: list[str],
                feedback: str = "") -> tuple[str, str]:
    """Zwraca (tekst, metoda)."""
    llm = get_llm()
    if llm is None:
        return template_answer(lead, facts, actions), "szablon"
    facts_txt = "\n".join(f"[{f['id']}] {f['text']}" for f in facts)
    actions_txt = "\n".join(f"- {a}" for a in actions) or "(brak)"
    user_msg = (f"Rola użytkownika: {role_label}\nPytanie: {question}\n\nWniosek wstępny: {lead}\n\n"
                f"Fakty:\n{facts_txt}\n\nAkcje sugerowane:\n{actions_txt}")
    if feedback:
        user_msg += f"\n\nPoprzednia wersja została odrzucona przez Weryfikatora: {feedback}\nPopraw odpowiedź."
    try:
        result = llm.invoke([("system", DRAFT_PROMPT), ("user", user_msg)])
        content = result.content if isinstance(result.content, str) else "".join(
            part.get("text", "") for part in result.content if isinstance(part, dict))
        return content.strip(), "LLM"
    except Exception as exc:
        log.warning("Generowanie odpowiedzi przez LLM nie powiodło się, używam szablonu: %s", exc)
        return template_answer(lead, facts, actions), "szablon"


def instance_agent_node(state: AssistantState) -> AssistantState:
    started = time.perf_counter()
    intent = state["intent"]
    user = get_user(state["user_id"])
    if state.get("facts") and state.get("attempts", 0) > 0:
        facts, lead, actions = state["facts"], state["lead"], state.get("suggested_actions", [])
    elif intent == "instance_diagnosis":
        facts, lead, actions = diagnosis_facts(state["instance_id"])
    elif intent == "instance_details":
        facts, lead, actions = details_facts(state["instance_id"])
    else:
        facts, lead, actions = my_work_facts(state["user_id"])

    draft, method = write_draft(state["question"], user.role_label, lead, facts, actions,
                                state.get("feedback", ""))
    ms = int((time.perf_counter() - started) * 1000)
    attempt = state.get("attempts", 0) + 1
    summary = (f"Zebrał {len(facts)} faktów (instancje, zadania, dokumenty, historia, logi). "
               f"Odpowiedź: {method}{', poprawka po uwagach Weryfikatora' if attempt > 1 else ''}.")
    return {"facts": facts, "lead": lead, "suggested_actions": actions, "draft": draft, "attempts": attempt,
            "trace": [{"agent": "Agent Instancji", "summary": summary, "ms": ms}]}
