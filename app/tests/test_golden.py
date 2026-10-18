"""Zestaw pytań testowych (sekcja 6 dokumentu): routing, uprawnienia, grounding – wszystkie 4 role."""
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
    if "agents" in case:
        assert result["agents"] == case["agents"]
    for text in case.get("must_contain", []):
        assert text in result["answer"], f"brak „{text}” w odpowiedzi:\n{result['answer']}"
    for text in case.get("must_not_contain", []):
        assert text not in result["answer"]


def test_routing_accuracy_meets_target():
    """Kryterium akceptacji: trafność routingu Dyspozytora ≥ 90%."""
    hits = sum(ask(c["question"], c["user"], c["role"], c.get("context"))["intent"] == c["intent"] for c in CASES)
    assert hits / len(CASES) >= 0.9


def test_parallel_agents_in_trace():
    result = ask("Dlaczego ta sprawa stoi?", "u_anna", "pracownik", "REK-2026-0412")
    agents = [s["agent"] for s in result["trace"]]
    assert agents[0] == "Dyspozytor" and agents[-1] == "Redaktor-Weryfikator"
    assert set(agents[1:-1]) == {"Agent Instancji", "Agent Monitor"}
    # numeracja faktów nie zależy od kolejności zakończenia pracy równoległej
    assert result["numbered_facts"][0]["agent"] == "Agent Instancji"
    assert result["numbered_facts"][-1]["agent"] == "Agent Monitor"
