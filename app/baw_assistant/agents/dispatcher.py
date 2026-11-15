"""Agent 1 – Dyspozytor: intencja, kontrola uprawnień, routing (także zdarzeń z kolejki).

Intencję pytania rozpoznaje LLM (structured output) albo – w trybie mock lub przy błędzie modelu –
reguły słów kluczowych. Uprawnienia są zawsze sprawdzane w kodzie. Dla zdarzeń (UCA, EDA, harmonogram)
typ zdarzenia wyznacza agentów i adresatów wyniku.
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from typing import Literal

from pydantic import BaseModel, Field

from .. import db, tools
from ..llm import extract_model_name, get_fast_llm, get_llm
from ..rbac import (can_view_instance, get_user, instance_exists, intent_allowed, visible_process_ids)
from ..state import AssistantState
from .common import AGENT_ANALYST, AGENT_INSTANCE, AGENT_MONITOR, elapsed_ms

log = logging.getLogger(__name__)

INSTANCE_ID_RE = re.compile(r"\b([A-Z]{3})-(\d{4})-(\d{4})\b", re.IGNORECASE)

Intent = Literal["instance_diagnosis", "instance_details", "my_work", "team_overview",
                 "process_analytics", "platform_status", "out_of_scope"]

INTENT_LABELS = {
    "instance_diagnosis": "diagnoza instancji",
    "instance_details": "szczegóły instancji",
    "my_work": "przegląd moich spraw",
    "team_overview": "przegląd zespołu",
    "process_analytics": "analityka procesu",
    "platform_status": "stan platformy",
    "out_of_scope": "poza zakresem",
    "event_document": "zdarzenie: nowa wersja dokumentu",
    "event_summary": "zdarzenie: prośba o podsumowanie sprawy",
    "event_data_quality": "zdarzenie: zmiana danych kontrahenta",
    "event_scan": "zdarzenie: skan cykliczny",
}

# Agenci dla intencji: (wymagany, opcjonalni – uruchamiani równolegle, jeśli włączeni).
ROUTES = {
    "instance_diagnosis": (AGENT_INSTANCE, [AGENT_MONITOR]),
    "instance_details": (AGENT_INSTANCE, []),
    "my_work": (AGENT_INSTANCE, []),
    "team_overview": (AGENT_ANALYST, []),
    "process_analytics": (AGENT_ANALYST, []),
    "platform_status": (AGENT_MONITOR, []),
    "event_document": (AGENT_INSTANCE, []),
    "event_summary": (AGENT_INSTANCE, []),
    "event_data_quality": (AGENT_MONITOR, []),
    "event_scan": (AGENT_MONITOR, []),
}

EVENT_INTENTS = {"document_version": "event_document", "summary_request": "event_summary",
                 "counterparty_change": "event_data_quality", "scan": "event_scan"}

PROCESS_KEYWORDS = {"REK": ("reklamac",), "FAK": ("faktur",), "ONB": ("onboard", "wdrozen", "nowego pracownika")}


class DispatchDecision(BaseModel):
    intent: Intent = Field(description="Intencja pytania użytkownika")
    reason: str = Field(description="Krótkie uzasadnienie po polsku")


SYSTEM_PROMPT = """Jesteś Dyspozytorem w asystencie operacyjnym IBM Business Automation Workflow.
Klasyfikujesz pytanie użytkownika do jednej intencji:
- instance_diagnosis: dlaczego sprawa stoi, jaki ma status, co zrobić dalej, ryzyko terminu
- instance_details: dokumenty, historia, kto zajmował się sprawą, zadania w sprawie
- my_work: lista moich spraw/zadań, co jest pilne
- team_overview: obciążenie zespołu, sprawy podwładnych, zaległości zespołu, sprawy całego działu
- process_analytics: KPI, trendy, czasy realizacji, wąskie gardła procesu, raport procesu
- platform_status: awarie, logi, alerty, dostępność usług, błędy techniczne platformy
- out_of_scope: wszystko niezwiązane z procesami BAW
Odpowiadasz wyłącznie strukturą z polami intent i reason."""


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c)).replace("ł", "l")


RULES: list[tuple[str, tuple[str, ...]]] = [
    ("platform_status", ("awari", "logi", "logow", "alert", "dostepnosc", "platform", "integracj",
                         "stan systemu", "bledy techniczne")),
    ("team_overview", ("zespol", "dzial", "podwladn", "obciazen", "kto ma najwiecej")),
    ("process_analytics", ("kpi", "trend", "waskie gardl", "sredni czas", "statystyk", "raport", "czas realizacji")),
    ("instance_diagnosis", ("dlaczego", "stoi", "co dalej", "zrobic dalej", "nastepny krok", "co mam zrobic",
                            "opozni", "utknel", "zablokowan")),
    ("instance_details", ("dokument", "historia", "historie", "kto zajmowal", "zalacznik", "zdarzen",
                          "kto prowadzil", "zadania w sprawie")),
    ("my_work", ("moje sprawy", "moich spraw", "moje zadania", "moich zadan", "co mam", "pilne", "pilnego",
                 "lista spraw", "po terminie")),
    ("instance_diagnosis", ("status", "termin", "sla", "stan sprawy")),
]


def classify_by_rules(question: str, has_instance: bool) -> tuple[str, str]:
    q = _normalize(question)

    def matches(keyword: str) -> bool:
        # Dopasowanie od początku słowa; krótkie słowa (np. „sla”) tylko w całości.
        end = r"\b" if len(keyword) <= 4 else ""
        return re.search(r"\b" + re.escape(keyword) + end, q) is not None

    for intent, keywords in RULES:
        hit = next((k for k in keywords if matches(k)), None)
        if hit:
            return intent, f"reguła: słowo kluczowe „{hit}”"
    if has_instance and re.search(r"\b(sprawa|sprawie|sprawy|ta|tej|te|ja)\b", q):
        return "instance_diagnosis", "reguła: pytanie o sprawę w kontekście"
    return "out_of_scope", "reguła: brak dopasowania do zadań asystenta"


def classify(question: str, has_instance: bool) -> tuple[str, str, str, str | None]:
    """Zwraca (intencja, uzasadnienie, metoda, użyty_model)."""
    llm = get_fast_llm() or get_llm()
    if llm is not None:
        try:
            structured = llm.with_structured_output(DispatchDecision)
            ctx = "Użytkownik ma wybraną sprawę w kontekście." if has_instance else "Brak wybranej sprawy."
            result = structured.invoke([("system", SYSTEM_PROMPT), ("user", f"{ctx}\nPytanie: {question}")])
            model_name = extract_model_name(result, llm)
            return result.intent, result.reason, "LLM", model_name
        except Exception as exc:  # model niedostępny → reguły, aplikacja działa dalej
            log.warning("Klasyfikacja LLM nie powiodła się, używam reguł: %s", exc)
    intent, reason = classify_by_rules(question, has_instance)
    return intent, reason, "reguły", None


def _resolve_process(question: str, params: dict, allowed: list[str]) -> str | None:
    if params.get("process_id"):
        return params["process_id"]
    q = _normalize(question)
    for pid, keys in PROCESS_KEYWORDS.items():
        if any(k in q for k in keys):
            return pid
    return allowed[0] if allowed else None


def _agents_for(intent: str) -> tuple[list[str], str | None]:
    """Lista agentów do uruchomienia; drugi element = nazwa wyłączonego agenta wymaganego."""
    required, optional = ROUTES[intent]
    if not db.agent_enabled(required):
        return [], required
    return [required] + [a for a in optional if db.agent_enabled(a)], None


def _event_plan(state: AssistantState) -> AssistantState:
    event = state["event"]
    intent = EVENT_INTENTS.get(event["type"])
    if not intent:
        return {"intent": "out_of_scope", "decision": "refuse", "agents": [], "recipients": [],
                "decision_reason": f"Nieznany typ zdarzenia: {event['type']}."}
    payload = event.get("payload", {})
    recipients: list[dict] = []
    instance_id = payload.get("instance_id")
    if instance_id:
        inst = tools.get_instance(instance_id)
        if not inst:
            return {"intent": intent, "decision": "not_found", "agents": [], "recipients": [],
                    "decision_reason": f"Nie znaleziono sprawy {instance_id}."}
        channels = ["Teams", "Zadanie BAW"] if intent == "event_document" else ["Zadanie BAW"]
        recipients = [{"channel": ch, "audience": f"user:{inst['assignee_name']}"} for ch in channels]
    elif intent == "event_data_quality":
        recipients = [{"channel": "Zadanie BAW", "audience": "owner"}, {"channel": "E-mail", "audience": "owner"}]
    agents, disabled = _agents_for(intent)
    if disabled:
        return {"intent": intent, "decision": "disabled", "agents": [], "recipients": [], "instance_id": instance_id,
                "decision_reason": f"{disabled} jest wyłączony przez administratora."}
    return {"intent": intent, "decision": "route", "agents": agents, "recipients": recipients,
            "instance_id": instance_id, "decision_reason": f"Zdarzenie {event['source']} · {event['type']}"}


def dispatcher_node(state: AssistantState) -> AssistantState:
    started = time.perf_counter()
    user = get_user(state["user_id"])

    if state.get("mode") == "event":
        plan = _event_plan(state)
        ev = state["event"]
        target = " + ".join(plan["agents"]) or "Redaktor-Weryfikator"
        summary = (f"Zdarzenie {ev['source']} · {ev['type']} → intencja: {INTENT_LABELS.get(plan['intent'], '?')}. "
                   f"Adresaci: {len(plan['recipients']) or 'wg alertów'}. → {target}.")
        return {**plan, "trace": [{"agent": "Dyspozytor", "summary": summary, "ms": elapsed_ms(started)}]}

    question = state["question"]
    match = INSTANCE_ID_RE.search(question)
    instance_id = (f"{match.group(1).upper()}-{match.group(2)}-{match.group(3)}" if match
                   else state.get("context_instance_id"))
    intent, reason, method, disp_model = classify(question, bool(instance_id))
    decision, decision_reason, process_id, agents = "route", "", None, []

    if intent == "out_of_scope":
        decision, decision_reason = "refuse", "Pytanie wykracza poza zakres asystenta procesów BAW."
    elif not intent_allowed(user, intent):
        decision = "refuse"
        decision_reason = f"Rola „{user.role_label}” nie ma dostępu do funkcji: {INTENT_LABELS[intent]}."
    elif intent in ("instance_diagnosis", "instance_details"):
        if not instance_id:
            decision, decision_reason = "need_context", "Wybierz sprawę w polu kontekstu albo podaj jej numer."
        elif not instance_exists(instance_id):
            decision, decision_reason = "not_found", f"Nie znaleziono sprawy {instance_id}."
        elif not can_view_instance(user, instance_id):
            decision = "refuse"
            decision_reason = f"Brak uprawnień do sprawy {instance_id} dla roli „{user.role_label}”."
    elif intent == "process_analytics":
        allowed = visible_process_ids(user)
        process_id = _resolve_process(question, state.get("params") or {}, allowed)
        if process_id not in allowed:
            decision = "refuse"
            decision_reason = f"Brak uprawnień do analityki procesu {process_id} dla roli „{user.role_label}”."

    if decision == "route":
        agents, disabled = _agents_for(intent)
        if disabled:
            decision, decision_reason = "disabled", f"{disabled} jest wyłączony przez administratora."

    access = {"route": "dostęp potwierdzony", "refuse": "odmowa", "need_context": "brak kontekstu",
              "not_found": "brak sprawy", "disabled": "agent wyłączony"}[decision]
    target = " + ".join(agents) if agents else "Redaktor-Weryfikator"
    scope = f"Sprawa: {instance_id}. " if instance_id and intent.startswith("instance") else (
        f"Proces: {process_id}. " if process_id else "")
    summary = (f"Intencja: {INTENT_LABELS[intent]} ({method}). Rola: {user.role_label}. {scope}"
               f"{access[0].upper() + access[1:]} → {target}.")
    return {"intent": intent, "decision": decision, "decision_reason": decision_reason or reason,
            "instance_id": instance_id, "process_id": process_id, "agents": agents, "recipients": [],
            "trace": [{"agent": "Dyspozytor", "summary": summary, "ms": elapsed_ms(started),
                       "model": disp_model, "details": {"method": method, "model": disp_model}}]}
