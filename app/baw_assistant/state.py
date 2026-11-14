"""Stan grafu LangGraph współdzielony przez agentów.

Agenci pracujący równolegle dopisują swoje fakty, wnioski i akcje do list (reducery),
a Redaktor-Weryfikator scala je, numeruje fakty [F1…] i składa odpowiedź.
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


def merge_dicts(left: dict | None, right: dict | None) -> dict:
    return {**(left or {}), **(right or {})}


class Fact(TypedDict, total=False):
    id: str        # np. "F3" – nadawany przez Redaktora-Weryfikatora
    agent: str     # który agent zebrał fakt
    text: str      # treść faktu (liczby i daty policzone w kodzie)
    source: str    # skąd pochodzi (tabela / narzędzie / baza wiedzy)


class Lead(TypedDict):
    agent: str
    text: str
    priority: int  # 0 = najważniejszy (np. awaria), 1 = główny wniosek, 2 = tło


class TraceStep(TypedDict, total=False):
    agent: str
    summary: str
    ms: int
    details: dict[str, Any]


class AssistantState(TypedDict, total=False):
    # wejście
    mode: str                      # chat | event
    question: str
    user_id: str
    role: str
    context_instance_id: str | None
    params: dict[str, Any]         # np. proces i okres dla raportu analitycznego
    event: dict[str, Any]          # zdarzenie z kolejki (tryb systemowy)
    on_token: Any                  # opcjonalny callback wywoływany dla każdego wygenerowanego tokenu
    # Dyspozytor
    intent: str
    decision: str                  # route | refuse | need_context | not_available | not_found | disabled
    decision_reason: str
    instance_id: str | None
    process_id: str | None
    agents: list[str]              # agenci do uruchomienia (równolegle)
    recipients: list[dict]         # adresaci wyniku w trybie systemowym
    # wyniki agentów (dopisywane równolegle)
    facts: Annotated[list[Fact], operator.add]
    leads: Annotated[list[Lead], operator.add]
    suggested_actions: Annotated[list[str], operator.add]
    recommendations: Annotated[list[str], operator.add]
    alerts: Annotated[list[dict], operator.add]
    data: Annotated[dict[str, Any], merge_dicts]
    # Redaktor-Weryfikator
    numbered_facts: list[Fact]
    lead: str
    draft: str
    attempts: int
    verdict: str                   # ok | blocked | refused | info
    answer: str
    sources: list[Fact]
    notifications: list[int]
    # ślad
    trace: Annotated[list[TraceStep], operator.add]
