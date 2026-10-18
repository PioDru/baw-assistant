"""Zestaw pytań testowych (sekcja 6 dokumentu): routing, uprawnienia, grounding."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from baw_assistant.graph import ask

CASES = yaml.safe_load((Path(__file__).parent / "golden_questions.yaml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_golden_question(case):
    result = ask(case["question"], case["user"], case["role"], case.get("context"))
    assert result["intent"] == case["intent"]
    assert result["decision"] == case["decision"]
    assert result["verdict"] == case["verdict"]
    for text in case.get("must_contain", []):
        assert text in result["answer"], f"brak „{text}” w odpowiedzi:\n{result['answer']}"
    for text in case.get("must_not_contain", []):
        assert text not in result["answer"]


def test_routing_accuracy_meets_target():
    """Kryterium akceptacji: trafność routingu Dyspozytora ≥ 90%."""
    hits = sum(ask(c["question"], c["user"], c["role"], c.get("context"))["intent"] == c["intent"] for c in CASES)
    assert hits / len(CASES) >= 0.9


def test_trace_lists_agents_in_order():
    result = ask("Dlaczego ta sprawa stoi?", "u_anna", "pracownik", "REK-2026-0412")
    assert [s["agent"] for s in result["trace"]] == ["Dyspozytor", "Agent Instancji", "Redaktor-Weryfikator"]
