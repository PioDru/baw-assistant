"""Weryfikator (grounding, uprawnienia) i ścieżka z LLM – model zastąpiony atrapą."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from baw_assistant.agents import dispatcher, instance_agent
from baw_assistant.agents.editor_verifier import check_draft
from baw_assistant.graph import ask

FACTS = [{"id": "F1", "text": "Termin SLA: 21.10.2026, zostały 3 dni.", "source": "instancje"},
         {"id": "F2", "text": "Ostatnia zmiana w sprawie: 14.10.2026 11:30, czyli 4 dni temu.",
          "source": "historia zdarzeń"}]


def test_grounded_draft_passes():
    draft = "Sprawa stoi od 4 dni [F2]. Termin SLA mija 21.10.2026 [F1].\n\nSugerowane kroki:\n1. Przypomnienie"
    assert check_draft(draft, FACTS, "Dlaczego stoi?", "u_anna") == []


@pytest.mark.parametrize("draft,expected", [
    ("Sprawa stoi od 6 dni [F2].", "liczby bez pokrycia"),
    ("Termin SLA mija 25.10.2026 [F1].", "daty bez pokrycia"),
    ("Sprawa stoi od 4 dni.", "brak odnośników"),
    ("Sprawa stoi od 4 dni [F7].", "nieistniejących faktów"),
    ("Podobnie jak REK-2026-0377 [F1].", "numery spraw"),
])
def test_ungrounded_draft_is_rejected(draft, expected):
    problems = check_draft(draft, FACTS, "Dlaczego stoi?", "u_anna")
    assert any(expected in p for p in problems), problems


class FakeLLM:
    """Atrapa modelu: klasyfikacja przez structured output i z góry zadane odpowiedzi."""

    def __init__(self, answers: list[str], intent: str = "instance_diagnosis"):
        self.answers = list(answers)
        self.intent = intent
        self.calls = 0

    def with_structured_output(self, schema):
        return SimpleNamespace(invoke=lambda _msgs: schema(intent=self.intent, reason="atrapa"))

    def invoke(self, _messages):
        self.calls += 1
        return SimpleNamespace(content=self.answers.pop(0) if self.answers else "brak")


def _patch(monkeypatch, llm):
    monkeypatch.setattr(dispatcher, "get_llm", lambda: llm)
    monkeypatch.setattr(instance_agent, "get_llm", lambda: llm)


def test_llm_answer_with_citations_is_accepted(monkeypatch):
    llm = FakeLLM(["Sprawa czeka na Protokół szkody od klienta [F6]. Termin SLA: 21.10.2026 [F2]."])
    _patch(monkeypatch, llm)
    result = ask("Dlaczego ta sprawa stoi?", "u_anna", "pracownik", "REK-2026-0412")
    assert result["verdict"] == "ok"
    assert "Intencja: diagnoza instancji (LLM)" in result["trace"][0]["summary"]
    assert {f["id"] for f in result["sources"]} == {"F2", "F6"}


def test_hallucination_is_sent_back_then_fixed(monkeypatch):
    llm = FakeLLM(["Sprawa stoi od 9 dni [F3].", "Sprawa stoi od 4 dni [F3]."])
    _patch(monkeypatch, llm)
    result = ask("Dlaczego ta sprawa stoi?", "u_anna", "pracownik", "REK-2026-0412")
    assert result["verdict"] == "ok" and llm.calls == 2
    assert [s["agent"] for s in result["trace"]] == ["Dyspozytor", "Agent Instancji", "Redaktor-Weryfikator",
                                                     "Agent Instancji", "Redaktor-Weryfikator"]


def test_repeated_hallucination_is_blocked(monkeypatch):
    llm = FakeLLM(["Sprawa stoi od 9 dni [F3].", "Termin minął 01.10.2026 [F2]."])
    _patch(monkeypatch, llm)
    result = ask("Dlaczego ta sprawa stoi?", "u_anna", "pracownik", "REK-2026-0412")
    assert result["verdict"] == "blocked"
    assert "9 dni" not in result["answer"] and "Protokół szkody" in result["answer"]


def test_llm_cannot_bypass_permissions(monkeypatch):
    """Nawet jeśli model źle sklasyfikuje pytanie, uprawnienia sprawdza kod."""
    _patch(monkeypatch, FakeLLM([], intent="team_overview"))
    result = ask("Pokaż wszystko", "u_anna", "pracownik", None)
    assert result["verdict"] == "refused"
