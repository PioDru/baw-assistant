"""Weryfikator (grounding, uprawnienia) i ścieżka z LLM – model zastąpiony atrapą."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from baw_assistant.agents import dispatcher, editor_verifier
from baw_assistant.agents.editor_verifier import COMPOSE_PROMPT, check_draft, normalize_citations
from baw_assistant.graph import ask

FACTS = [{"id": "F1", "text": "Termin SLA: 21.10.2026, zostały 3 dni.", "source": "instancje"},
         {"id": "F2", "text": "Ostatnia zmiana w sprawie: 14.10.2026 11:30, czyli 4 dni temu.",
          "source": "historia zdarzeń"}]


def test_compose_prompt_contains_few_shot_examples():
    """Weryfikuje, że COMPOSE_PROMPT zawiera przykłady wejścia/wyjścia (few-shot)."""
    assert "PRZYKŁAD WEJŚCIA:" in COMPOSE_PROMPT
    assert "PRZYKŁAD POPRAWNEJ ODPOWIEDZI:" in COMPOSE_PROMPT
    assert "[F1]" in COMPOSE_PROMPT and "[F2]" in COMPOSE_PROMPT


@pytest.mark.parametrize("input_text,expected_output", [
    ("Sprawa stoi od 4 dni (F2).", "Sprawa stoi od 4 dni [F2]."),
    ("Sprawa stoi od 4 dni (f2).", "Sprawa stoi od 4 dni [F2]."),
    ("Sprawa stoi od 4 dni [f2].", "Sprawa stoi od 4 dni [F2]."),
    ("Sprawa stoi [F1, F2].", "Sprawa stoi [F1][F2]."),
    ("Sprawa stoi [F1; F2].", "Sprawa stoi [F1][F2]."),
    ("Sprawa stoi (F1, F2).", "Sprawa stoi [F1][F2]."),
    ("Sprawa stoi [Fakt 2].", "Sprawa stoi [F2]."),
    ("Sprawa stoi (Fakt 2).", "Sprawa stoi [F2]."),
    ("Sprawa stoi [Fakt F2].", "Sprawa stoi [F2]."),
    ("Sprawa stoi [fakt 2].", "Sprawa stoi [F2]."),
    ("Sprawa stoi (źródło: F2).", "Sprawa stoi [F2]."),
    ("Sprawa stoi (źródła: F1, F2).", "Sprawa stoi [F1][F2]."),
    ("Sprawa stoi [źródło: 1, 2].", "Sprawa stoi [F1][F2]."),
    ("Sprawa stoi od 4 dni [2].", "Sprawa stoi od 4 dni [F2]."),
    ("Sprawa stoi [1, 2].", "Sprawa stoi [F1][F2]."),
    ("Sprawa stoi [F1-F2].", "Sprawa stoi [F1][F2]."),
    ("Sprawa stoi [F1 i F2].", "Sprawa stoi [F1][F2]."),
    ("Sprawa stoi (F1 oraz F2).", "Sprawa stoi [F1][F2]."),
    ("Sprawa stoi (F 2).", "Sprawa stoi [F2]."),
    ("Sprawa stoi [F2.].", "Sprawa stoi [F2]."),
    ("Sprawa stoi zgodnie z faktem 2.", "Sprawa stoi [F2]."),
    ("Sprawa stoi wg F2.", "Sprawa stoi [F2]."),
    ("Sprawa stoi 【F1, F2】.", "Sprawa stoi [F1][F2]."),
])
def test_normalize_citations_formats(input_text: str, expected_output: str):
    """Weryfikuje poprawną normalizację różnych formatów przypisów generowanych przez modele."""
    assert normalize_citations(input_text) == expected_output


@pytest.mark.parametrize("draft", [
    "Sprawa stoi od 4 dni (F2). Termin SLA mija 21.10.2026 (F1).",
    "Sprawa stoi od 4 dni [Fakt 2]. Termin SLA mija 21.10.2026 [Fakt 1].",
    "Sprawa stoi od 4 dni (źródło: F2). Termin SLA mija 21.10.2026 (źródło: F1).",
    "Sprawa stoi od 4 dni [2]. Termin SLA mija 21.10.2026 [1].",
    "Sprawa stoi od 4 dni [F1, F2]. Termin SLA mija 21.10.2026 [F1].",
    "Sprawa stoi od 4 dni [F1-F2]. Ostatnia zmiana o godz. 11.30 [F2].",
    "Sprawa stoi od 4,0 dni [F2]. Termin: 21.10.2026 [F1].",
    "### Podsumowanie\nSprawa stoi od 4 dni [F2].\n\nSugerowane kroki:\n1. Przypomnienie\n2. Kontakt z klientem",
    "Sprawa stoi od 4 dni [F2].\n- 1. Przypomnienie do klienta\n- 2. Eskalacja",
    "Sprawa stoi od 4 dni [F2].\nKrok 1: Wysłanie ponaglenia.",
])
def test_alternative_citation_formats_pass_verifier(draft: str):
    """Odpowiedzi z alternatywnymi formatami przypisów i listami przechodzą przez weryfikator bez blokady."""
    assert check_draft(draft, FACTS, "Dlaczego stoi?", "u_anna") == []


def test_extra_context_allowed_in_verifier():
    """Weryfikuje, że liczby i fakty z wiodącego wniosku (lead) oraz rekomendacji nie powodują blokady."""
    facts = [{"id": "F1", "text": "Proces reklamacji: średni czas 4,2 dni.", "source": "analityka"}]
    lead = "Wąskim gardłem jest krok Weryfikacja (średnio 4,2 dni)."
    actions = ["1. Wysłanie przypomnienia", "2. Eskalacja"]
    recs = ["Ustalić z zespołem termin obsługi."]
    draft = "Wąskim gardłem jest krok Weryfikacja (średnio 4,2 dni) [F1].\n\nSugerowane kroki:\n1. Wysłanie przypomnienia"
    assert check_draft(draft, facts, "Wąskie gardła", "u_marek", extra_context=[lead] + actions + recs) == []


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
    monkeypatch.setattr(editor_verifier, "get_llm", lambda: llm)


def test_llm_answer_with_citations_is_accepted(monkeypatch):
    llm = FakeLLM(["Sprawa czeka na Protokół szkody od klienta [F6]. Termin SLA: 21.10.2026 [F2]."])
    _patch(monkeypatch, llm)
    result = ask("Dlaczego ta sprawa stoi?", "u_anna", "pracownik", "REK-2026-0412")
    assert result["verdict"] == "ok"
    assert "Intencja: diagnoza instancji (LLM)" in result["trace"][0]["summary"]
    assert {f["id"] for f in result["sources"]} == {"F2", "F6"}


def test_llm_answer_with_alternative_citation_syntax_is_accepted(monkeypatch):
    """Model używający (F6) lub [Fakt 2] nie zostaje odrzucony przez weryfikator."""
    llm = FakeLLM(["Sprawa czeka na Protokół szkody od klienta (F6). Termin SLA: 21.10.2026 [Fakt 2]."])
    _patch(monkeypatch, llm)
    result = ask("Dlaczego ta sprawa stoi?", "u_anna", "pracownik", "REK-2026-0412")
    assert result["verdict"] == "ok"
    assert {f["id"] for f in result["sources"]} == {"F2", "F6"}
    # Upewniamy się, że w ostatecznej odpowiedzi znaczniki są znormalizowane do [F#]
    assert "[F6]" in result["answer"] and "[F2]" in result["answer"]


def test_hallucination_is_corrected_on_second_attempt(monkeypatch):
    llm = FakeLLM(["Sprawa stoi od 9 dni [F3].", "Sprawa stoi od 4 dni [F3]."])
    _patch(monkeypatch, llm)
    result = ask("Dlaczego ta sprawa stoi?", "u_anna", "pracownik", "REK-2026-0412")
    assert result["verdict"] == "ok" and llm.calls == 2 and result["attempts"] == 2
    assert "Poprawki: 1" in result["trace"][-1]["summary"]


def test_repeated_hallucination_is_blocked(monkeypatch):
    llm = FakeLLM(["Sprawa stoi od 9 dni [F3].", "Termin minął 01.10.2026 [F2]."])
    _patch(monkeypatch, llm)
    result = ask("Dlaczego ta sprawa stoi?", "u_anna", "pracownik", "REK-2026-0412")
    assert result["verdict"] == "blocked"
    assert "9 dni" not in result["answer"] and "Protokół szkody" in result["answer"]

    # Weryfikacja obecności szczegółowej diagnostyki w metadanych trace
    verifier_trace = result["trace"][-1]
    assert verifier_trace["agent"] == "Redaktor-Weryfikator"
    assert "details" in verifier_trace
    details = verifier_trace["details"]
    assert details["verdict"] == "blocked"
    assert details["attempts_count"] == 2
    assert len(details["attempts"]) == 2
    assert details["attempts"][0]["accepted"] is False
    assert any("liczby bez pokrycia" in p for p in details["attempts"][0]["problems"])
    assert details["attempts"][1]["accepted"] is False
    assert any("daty bez pokrycia" in p for p in details["attempts"][1]["problems"])
    assert len(details["rejection_reasons"]) == 2


def test_trace_contains_correction_diagnostics(monkeypatch):
    llm = FakeLLM(["Sprawa stoi od 9 dni [F3].", "Sprawa stoi od 4 dni [F3]."])
    _patch(monkeypatch, llm)
    result = ask("Dlaczego ta sprawa stoi?", "u_anna", "pracownik", "REK-2026-0412")
    assert result["verdict"] == "ok"
    verifier_trace = result["trace"][-1]
    details = verifier_trace.get("details", {})
    assert details["verdict"] == "ok"
    assert details["attempts_count"] == 2
    assert details["attempts"][0]["accepted"] is False
    assert details["attempts"][1]["accepted"] is True
    assert len(details["rejection_reasons"]) == 1


def test_llm_cannot_bypass_permissions(monkeypatch):
    """Nawet jeśli model źle sklasyfikuje pytanie, uprawnienia sprawdza kod."""
    _patch(monkeypatch, FakeLLM([], intent="team_overview"))
    result = ask("Pokaż wszystko", "u_anna", "pracownik", None)
    assert result["verdict"] == "refused"


def test_llm_cannot_leak_other_instance(monkeypatch):
    llm = FakeLLM(["Podobnie jak w REK-2026-0377 [F1].", "Podobnie jak w REK-2026-0377 [F1]."])
    _patch(monkeypatch, llm)
    result = ask("Dlaczego ta sprawa stoi?", "u_anna", "pracownik", "REK-2026-0412")
    assert result["verdict"] == "blocked" and "REK-2026-0377" not in result["answer"]


def test_render_answer_blocked_shows_blocked_draft_expander(monkeypatch):
    """Weryfikuje, że w przypadku zablokowanej odpowiedzi komponent render_answer wyświetla zwijany element 'Zablokowana odpowiedź'."""
    from ui.components import render_answer
    from baw_assistant.rbac import User
    import streamlit as st

    expanders_created = []
    markdown_calls = []

    class DummyExpander:
        def __init__(self, label):
            self.label = label

        def __enter__(self):
            expanders_created.append(self.label)
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(st, "expander", DummyExpander)
    monkeypatch.setattr(st, "markdown", lambda text, **kwargs: markdown_calls.append(text))
    monkeypatch.setattr(st, "badge", lambda *args, **kwargs: None)

    user = User(id="u_anna", name="Anna Kowalska", role="pracownik", team_id="team_rek")
    result = {
        "verdict": "blocked",
        "answer": "Bezpieczna wersja faktograficzna",
        "draft": "Oryginalna odpowiedź modelu LLM",
        "sources": [{"id": "F1", "text": "Fakt 1", "source": "test"}],
    }
    render_answer(result, "key1", user, "REK-2026-0412")
    assert expanders_created == ["Źródła (1)", "Zablokowana odpowiedź"]
    assert "Oryginalna odpowiedź modelu LLM" in markdown_calls
