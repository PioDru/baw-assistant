"""Agent 1 – Dyspozytor: intencja, kontrola uprawnień, routing.

Intencję rozpoznaje LLM (structured output) albo – w trybie mock lub przy błędzie modelu –
reguły słów kluczowych. Uprawnienia są zawsze sprawdzane w kodzie.
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from typing import Literal

from pydantic import BaseModel, Field

from ..llm import get_llm
from ..rbac import (V1_INTENTS, can_view_instance, get_user, instance_exists, intent_allowed)
from ..state import AssistantState

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
}

V2_AGENT = {
    "team_overview": "Agent Analityk",
    "process_analytics": "Agent Analityk",
    "platform_status": "Agent Monitor",
}


class DispatchDecision(BaseModel):
    intent: Intent = Field(description="Intencja pytania użytkownika")
    reason: str = Field(description="Krótkie uzasadnienie po polsku")


SYSTEM_PROMPT = """Jesteś Dyspozytorem w asystencie operacyjnym IBM Business Automation Workflow.
Klasyfikujesz pytanie użytkownika do jednej intencji:
- instance_diagnosis: dlaczego sprawa stoi, jaki ma status, co zrobić dalej, ryzyko terminu
- instance_details: dokumenty, historia, kto zajmował się sprawą, zadania w sprawie
- my_work: lista moich spraw/zadań, co jest pilne
- team_overview: obciążenie zespołu, sprawy podwładnych, zaległości zespołu, sprawy całego działu
- process_analytics: KPI, trendy, czasy realizacji, wąskie gardła procesu
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
    ("process_analytics", ("kpi", "trend", "waskie gardl", "sredni czas", "statystyk", "raport")),
    ("instance_diagnosis", ("dlaczego", "stoi", "co dalej", "zrobic dalej", "nastepny krok", "co mam zrobic",
                            "opozni", "utknel", "zablokowan")),
    ("instance_details", ("dokument", "historia", "historie", "kto zajmowal", "zalacznik", "zdarzen",
                          "kto prowadzil", "zadania w sprawie")),
    ("my_work", ("moje sprawy", "moich spraw", "moje zadania", "moich zadan", "co mam", "pilne", "pilnego",
                 "lista spraw")),
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


def classify(question: str, has_instance: bool) -> tuple[str, str, str]:
    """Zwraca (intencja, uzasadnienie, metoda)."""
    llm = get_llm()
    if llm is not None:
        try:
            structured = llm.with_structured_output(DispatchDecision)
            ctx = "Użytkownik ma wybraną sprawę w kontekście." if has_instance else "Brak wybranej sprawy."
            result = structured.invoke([("system", SYSTEM_PROMPT), ("user", f"{ctx}\nPytanie: {question}")])
            return result.intent, result.reason, "LLM"
        except Exception as exc:  # model niedostępny → reguły, aplikacja działa dalej
            log.warning("Klasyfikacja LLM nie powiodła się, używam reguł: %s", exc)
    intent, reason = classify_by_rules(question, has_instance)
    return intent, reason, "reguły"


def dispatcher_node(state: AssistantState) -> AssistantState:
    started = time.perf_counter()
    user = get_user(state["user_id"])
    question = state["question"]

    match = INSTANCE_ID_RE.search(question)
    instance_id = (f"{match.group(1).upper()}-{match.group(2)}-{match.group(3)}" if match
                   else state.get("context_instance_id"))

    intent, reason, method = classify(question, bool(instance_id))
    decision, decision_reason = "route", ""

    if intent == "out_of_scope":
        decision, decision_reason = "refuse", "Pytanie wykracza poza zakres asystenta procesów BAW."
    elif not intent_allowed(user, intent):
        decision = "refuse"
        decision_reason = (f"Rola „{user.role_label}” nie ma dostępu do funkcji: {INTENT_LABELS[intent]}.")
    elif intent not in V1_INTENTS:
        decision = "not_available"
        decision_reason = f"Tę funkcję obsłuży {V2_AGENT[intent]}, który pojawi się w PoC v2."
    elif intent in ("instance_diagnosis", "instance_details"):
        if not instance_id:
            decision, decision_reason = "need_context", "Wybierz sprawę w polu kontekstu albo podaj jej numer."
        elif not instance_exists(instance_id):
            decision, decision_reason = "not_found", f"Nie znaleziono sprawy {instance_id}."
        elif not can_view_instance(user, instance_id):
            decision = "refuse"
            decision_reason = f"Brak uprawnień do sprawy {instance_id} dla roli „{user.role_label}”."

    ms = int((time.perf_counter() - started) * 1000)
    target = "Agent Instancji" if decision == "route" else "Redaktor-Weryfikator"
    access = {"route": "dostęp potwierdzony", "refuse": "odmowa",
              "need_context": "brak kontekstu", "not_available": "funkcja PoC v2",
              "not_found": "brak sprawy"}[decision]
    summary = (f"Intencja: {INTENT_LABELS[intent]} ({method}). Rola: {user.role_label}. "
               f"{'Sprawa: ' + instance_id + '. ' if instance_id else ''}{access[0].upper() + access[1:]} → {target}.")
    return {
        "intent": intent,
        "decision": decision,
        "decision_reason": decision_reason or reason,
        "instance_id": instance_id,
        "attempts": 0,
        "trace": [{"agent": "Dyspozytor", "summary": summary, "ms": ms}],
    }
