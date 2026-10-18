"""Wspólne elementy agentów: zapis faktów, odmiana liczebników, katalog akcji sugerowanych."""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator

from ..state import Fact, Lead

# Akcje sugerowane – wykonywane dopiero po zatwierdzeniu przez człowieka (moduł actions).
ACTION_REMIND = "Przygotuj przypomnienie do klienta"
ACTION_ESCALATE_SLA = "Zgłoś ryzyko SLA kierownikowi"
ACTION_INCIDENT = "Zgłoś incydent techniczny administratorowi"
ACTION_PING_DEPT = "Wyślij monit do działu realizującego zadanie"
ACTION_REBALANCE = "Zaproponuj przesunięcie spraw w zespole"

AGENT_INSTANCE = "Agent Instancji"
AGENT_ANALYST = "Agent Analityk"
AGENT_MONITOR = "Agent Monitor"
AGENT_ORDER = [AGENT_INSTANCE, AGENT_ANALYST, AGENT_MONITOR]


def num(value: float) -> str:
    """Liczba w zapisie polskim: 1.7 → „1,7”."""
    return f"{value:.1f}".replace(".", ",") if value % 1 else str(int(value))


def days_word(n: int) -> str:
    return "dzień" if abs(n) == 1 else "dni"


def left(n: int) -> str:
    """„został 1 dzień”, „zostały 3 dni”, „zostało 5 dni”."""
    if n == 1:
        return "został 1 dzień"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"zostały {n} dni"
    return f"zostało {n} dni"


def days_dec(value: float) -> str:
    """Wartość w dniach: „1,8 dnia”, „4 dni”."""
    return f"{num(value)} {'dnia' if value % 1 else days_word(int(value))}"


class Findings:
    """Wynik pracy agenta: fakty, wnioski, akcje i rekomendacje."""

    def __init__(self, agent: str) -> None:
        self.agent = agent
        self.facts: list[Fact] = []
        self.leads: list[Lead] = []
        self.actions: list[str] = []
        self.recommendations: list[str] = []
        self.data: dict = {}
        self.alerts: list[dict] = []

    def fact(self, text: str, source: str) -> None:
        self.facts.append({"agent": self.agent, "text": text, "source": source})

    def lead(self, text: str, priority: int = 1) -> None:
        self.leads.append({"agent": self.agent, "text": text, "priority": priority})

    def update(self, summary: str, ms: int) -> dict:
        out = {"facts": self.facts, "leads": self.leads, "suggested_actions": self.actions,
               "recommendations": self.recommendations, "alerts": self.alerts,
               "trace": [{"agent": self.agent, "summary": summary, "ms": ms}]}
        if self.data:
            out["data"] = self.data
        return out


@contextmanager
def timer() -> Iterator[dict]:
    box = {"ms": 0}
    started = time.perf_counter()
    try:
        yield box
    finally:
        box["ms"] = int((time.perf_counter() - started) * 1000)


def elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
