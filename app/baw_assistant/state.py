"""Stan grafu LangGraph współdzielony przez agentów."""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class Fact(TypedDict):
    id: str        # np. "F3" – odnośnik używany w odpowiedzi
    text: str      # treść faktu (liczby i daty policzone w kodzie)
    source: str    # skąd pochodzi (tabela / narzędzie)


class TraceStep(TypedDict):
    agent: str
    summary: str
    ms: int


class AssistantState(TypedDict, total=False):
    # wejście
    question: str
    user_id: str
    role: str
    context_instance_id: str | None
    # Dyspozytor
    intent: str
    decision: str          # route | refuse | need_context | not_available | not_found
    decision_reason: str
    instance_id: str | None
    # Agent Instancji
    facts: list[Fact]
    lead: str
    suggested_actions: list[str]
    draft: str
    attempts: int
    # Redaktor-Weryfikator
    verdict: str           # ok | retry | blocked | refused | info
    feedback: str
    answer: str
    sources: list[Fact]
    # ślad (reducer: kolejne kroki są dopisywane)
    trace: Annotated[list[TraceStep], operator.add]
    meta: dict[str, Any]
