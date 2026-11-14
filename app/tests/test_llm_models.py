"""Testy jednostkowe i benchmarkowe dla poszczególnych modeli językowych (LLM).

Zakres testów:
1. Czas odpowiedzi (latency / response time w ms).
2. Schemat odpowiedzi (Pydantic DispatchDecision dla structured output oraz struktura tekstu z odnośnikami).
3. Ocena przez Weryfikatora (check_draft) – badanie przyczyn odrzucenia (liczby, daty, brak odnośników, uprawnienia).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from baw_assistant.agents.dispatcher import DispatchDecision, Intent, classify_by_rules
from baw_assistant.agents.editor_verifier import COMPOSE_PROMPT, check_draft
from baw_assistant.llm import create_llm
from baw_assistant.state import Fact

TEST_FACTS: list[Fact] = [
    {"id": "F1", "text": "Termin SLA sprawy REK-2026-0412 mija 21.10.2026, zostały 3 dni.", "source": "instancje", "agent": "Agent Instancji"},
    {"id": "F2", "text": "Ostatnia modyfikacja: 14.10.2026 11:30 (4 dni bez aktywności).", "source": "historia zdarzeń", "agent": "Agent Instancji"},
    {"id": "F3", "text": "Oczekujący krok: Protokół szkody od klienta.", "source": "zadania", "agent": "Agent Instancji"},
    {"id": "F4", "text": "Brak aktywnych alertów monitoringu dla tego procesu.", "source": "monitoring", "agent": "Agent Monitor"},
]


@dataclass
class SimulatedModelProfile:
    """Profil symulowanego modelu do testów porównawczych."""
    name: str
    simulated_delay_s: float
    routing_output: dict[str, Any]
    editor_output: str


PROFILES = [
    SimulatedModelProfile(
        name="baw-fast (Qwen-3B / Triage)",
        simulated_delay_s=0.015,
        routing_output={"intent": "instance_diagnosis", "reason": "Pytanie o przyczynę postoju sprawy."},
        editor_output="Sprawa REK-2026-0412 czeka na Protokół szkody od klienta [F3]. Termin SLA: 21.10.2026 [F1].",
    ),
    SimulatedModelProfile(
        name="baw-moe-main (Gemma-4-26B / MoE)",
        simulated_delay_s=0.045,
        routing_output={"intent": "instance_diagnosis", "reason": "Pytanie o diagnozę przestoju w realizacji sprawy."},
        editor_output=(
            "Sprawa REK-2026-0412 nie porusza się od 4 dni z powodu oczekiwania na Protokół szkody [F2][F3]. "
            "Termin SLA upływa 21.10.2026, pozostały 3 dni [F1].\n\n"
            "Sugerowane kroki:\n1. Wysłanie przypomnienia do klienta"
        ),
    ),
    SimulatedModelProfile(
        name="baw-cloud-fallback (GPT-4o / Fallback)",
        simulated_delay_s=0.080,
        routing_output={"intent": "instance_diagnosis", "reason": "Diagnoza braku postępu w sprawie reklamacji."},
        editor_output=(
            "Główną przyczyną zatrzymania sprawy REK-2026-0412 jest brak Protokołu szkody od klienta [F3]. "
            "Brak aktywności trwa od 14.10.2026 11:30 [F2], a termin SLA mija 21.10.2026 [F1]."
        ),
    ),
]


class MockModelRunner:
    """Wrapper udający model LangChain ze śledzeniem czasu wykonania i walidacją wywołań."""

    def __init__(self, profile: SimulatedModelProfile):
        self.profile = profile
        self.call_history: list[dict[str, Any]] = []

    def with_structured_output(self, schema):
        runner = self

        class _StructuredMock:
            def invoke(self, messages):
                t_start = time.perf_counter()
                if runner.profile.simulated_delay_s > 0:
                    time.sleep(runner.profile.simulated_delay_s)
                latency_ms = (time.perf_counter() - t_start) * 1000

                raw = runner.profile.routing_output
                decision = schema(**raw)
                runner.call_history.append({
                    "type": "structured",
                    "latency_ms": latency_ms,
                    "input": messages,
                    "output": decision,
                })
                return decision

        return _StructuredMock()

    def invoke(self, messages):
        t_start = time.perf_counter()
        if self.profile.simulated_delay_s > 0:
            time.sleep(self.profile.simulated_delay_s)
        latency_ms = (time.perf_counter() - t_start) * 1000

        text = self.profile.editor_output
        self.call_history.append({
            "type": "invoke",
            "latency_ms": latency_ms,
            "input": messages,
            "output": text,
        })
        mock_resp = MagicMock()
        mock_resp.content = text
        return mock_resp


# ==============================================================================
# 1. TESTY CZASU ODPOWIEDZI (RESPONSE TIME / LATENCY)
# ==============================================================================

@pytest.mark.parametrize("profile", PROFILES, ids=[p.name for p in PROFILES])
def test_model_latency_measurement(profile: SimulatedModelProfile):
    """Weryfikuje dokładność pomiaru czasu odpowiedzi modelu dla structured output i generation."""
    runner = MockModelRunner(profile)

    # Pomiar czasu structured output (np. Dyspozytor)
    structured = runner.with_structured_output(DispatchDecision)
    decision = structured.invoke([("user", "Dlaczego ta sprawa stoi?")])
    assert decision.intent == "instance_diagnosis"
    assert len(runner.call_history) == 1
    latency_structured = runner.call_history[0]["latency_ms"]
    assert latency_structured >= (profile.simulated_delay_s * 1000) * 0.8

    # Pomiar czasu generowania tekstu (np. Redaktor)
    resp = runner.invoke([("user", "Złóż odpowiedź z faktów.")])
    assert resp.content == profile.editor_output
    assert len(runner.call_history) == 2
    latency_invoke = runner.call_history[1]["latency_ms"]
    assert latency_invoke >= (profile.simulated_delay_s * 1000) * 0.8


def test_fast_model_is_faster_than_moe_and_cloud():
    """Weryfikuje relacje czasowe między modelami lekkimi (triage) a większymi."""
    runners = {p.name: MockModelRunner(p) for p in PROFILES}

    latencies: dict[str, float] = {}
    for name, runner in runners.items():
        runner.invoke([("user", "test")])
        latencies[name] = runner.call_history[-1]["latency_ms"]

    assert latencies["baw-fast (Qwen-3B / Triage)"] < latencies["baw-moe-main (Gemma-4-26B / MoE)"]
    assert latencies["baw-moe-main (Gemma-4-26B / MoE)"] < latencies["baw-cloud-fallback (GPT-4o / Fallback)"]


# ==============================================================================
# 2. TESTY SCHEMATU ODPOWIEDZI (RESPONSE SCHEMA)
# ==============================================================================

@pytest.mark.parametrize("valid_intent", [
    "instance_diagnosis",
    "instance_details",
    "my_work",
    "team_overview",
    "process_analytics",
    "platform_status",
    "out_of_scope",
])
def test_dispatch_decision_schema_valid_intents(valid_intent: str):
    """Weryfikuje poprawność schematu Pydantic dla wszystkich dozwolonych intencji."""
    decision = DispatchDecision(intent=valid_intent, reason="Poprawne uzasadnienie")
    assert decision.intent == valid_intent
    assert decision.reason == "Poprawne uzasadnienie"


@pytest.mark.parametrize("invalid_intent", [
    "unknown_action",
    "hack_prompt",
    "random_intent",
    "",
])
def test_dispatch_decision_schema_rejects_invalid_intents(invalid_intent: str):
    """Weryfikuje odrzucenie odpowiedzi modelu, gdy intencja narusza schemat Pydantic."""
    with pytest.raises(ValidationError):
        DispatchDecision(intent=invalid_intent, reason="Błędna intencja")


def test_dispatch_decision_schema_missing_fields():
    """Weryfikuje odrzucenie odpowiedzi modelu przy braku wymaganych pól schematu."""
    with pytest.raises(ValidationError):
        DispatchDecision.model_validate({"intent": "instance_diagnosis"})  # brak 'reason'
    with pytest.raises(ValidationError):
        DispatchDecision.model_validate({"reason": "Uzasadnienie bez intencji"})  # brak 'intent'


# ==============================================================================
# 3. TESTY WERYFIKATORA (CHECK_DRAFT / ODRZUCENIA I ZATWIERDZENIA)
# ==============================================================================

@pytest.mark.parametrize("profile", PROFILES, ids=[p.name for p in PROFILES])
def test_profiles_pass_verifier_when_grounded(profile: SimulatedModelProfile):
    """Weryfikuje, że poprawnie ułożone odpowiedzi modeli przechodzą przez weryfikator bez zastrzeżeń."""
    problems = check_draft(profile.editor_output, TEST_FACTS, "Dlaczego ta sprawa stoi?", "u_anna")
    assert problems == [], f"Model {profile.name} został niesłusznie odrzucony: {problems}"


@pytest.mark.parametrize("candidate_draft,expected_error_keyword", [
    # 1. Halucynacja liczb (np. zmyślona liczba dni lub kwota)
    ("Sprawa stoi od 9 dni [F2]. Termin SLA: 21.10.2026 [F1].", "liczby bez pokrycia"),
    ("W sprawie zidentyfikowano 5 zaległych zadań [F3].", "liczby bez pokrycia"),
    # 2. Zmiana formatu daty lub zmyślona data
    ("Termin upływa 25.10.2026 [F1].", "daty bez pokrycia"),
    ("Termin mija 21.10 [F1].", "daty bez pokrycia"),
    # 3. Brak jakichkolwiek odnośników [F#]
    ("Sprawa REK-2026-0412 stoi od 4 dni z powodu braku Protokołu szkody.", "brak odnośników"),
    # 4. Zmyślony identyfikator faktu
    ("Sprawa stoi od 4 dni [F99]. Termin SLA: 21.10.2026 [F1].", "nieistniejących faktów"),
    # 5. Wyciek innej sprawy, do której użytkownik nie ma uprawnień
    ("Podobna sytuacja wystąpiła w REK-2026-0377 [F1].", "numery spraw"),
])
def test_verifier_detects_and_rejects_hallucinations(candidate_draft: str, expected_error_keyword: str):
    """Weryfikuje, że weryfikator poprawnie identyfikuje i blokuje konkretne rodzaje błędów modelu."""
    problems = check_draft(candidate_draft, TEST_FACTS, "Dlaczego ta sprawa stoi?", "u_anna")
    assert len(problems) > 0, "Weryfikator powinien był zablokować tę odpowiedź!"
    assert any(expected_error_keyword in p for p in problems), f"Brak oczekiwanego błędu '{expected_error_keyword}' w: {problems}"


# ==============================================================================
# 4. KOMPLEKSOWA EWALUACJA MODELI (BENCHMARK RUNNER)
# ==============================================================================

@dataclass
class ModelEvaluationReport:
    model_name: str
    dispatch_latency_ms: float
    dispatch_schema_valid: bool
    dispatch_intent: str
    editor_latency_ms: float
    editor_accepted: bool
    editor_rejection_reasons: list[str]


def evaluate_model_pipeline(profile: SimulatedModelProfile) -> ModelEvaluationReport:
    """Przeprowadza pełną ścieżkę testową: Dispatcher -> Czas -> Schemat -> Editor -> Weryfikator."""
    runner = MockModelRunner(profile)

    # Krok 1: Dyspozytor
    structured = runner.with_structured_output(DispatchDecision)
    t0 = time.perf_counter()
    decision = structured.invoke([("user", "Dlaczego sprawa REK-2026-0412 stoi?")])
    t1 = time.perf_counter()
    dispatch_latency = (t1 - t0) * 1000

    # Krok 2: Redaktor
    t2 = time.perf_counter()
    resp = runner.invoke([("system", COMPOSE_PROMPT), ("user", "Fakty: ...")])
    t3 = time.perf_counter()
    editor_latency = (t3 - t2) * 1000

    # Krok 3: Weryfikacja
    problems = check_draft(resp.content, TEST_FACTS, "Dlaczego sprawa stoi?", "u_anna")

    return ModelEvaluationReport(
        model_name=profile.name,
        dispatch_latency_ms=dispatch_latency,
        dispatch_schema_valid=isinstance(decision, DispatchDecision),
        dispatch_intent=decision.intent,
        editor_latency_ms=editor_latency,
        editor_accepted=(len(problems) == 0),
        editor_rejection_reasons=problems,
    )


@pytest.mark.parametrize("profile", PROFILES, ids=[p.name for p in PROFILES])
def test_full_model_evaluation_pipeline(profile: SimulatedModelProfile):
    """Weryfikuje kompletny raport ewaluacyjny dla każdego modelu w architekturze."""
    report = evaluate_model_pipeline(profile)

    assert report.model_name == profile.name
    assert report.dispatch_latency_ms > 0
    assert report.dispatch_schema_valid is True
    assert report.dispatch_intent == "instance_diagnosis"
    assert report.editor_latency_ms > 0
    assert report.editor_accepted is True
    assert report.editor_rejection_reasons == []


# ==============================================================================
# 5. TEST FABRYKI create_llm I OPCJONALNYCH MODELI RZECZYWISTYCH
# ==============================================================================

def test_create_llm_factory_mock():
    """Weryfikuje, że tryb mock zwraca None (praca offline/regułowa)."""
    assert create_llm("mock") is None
    assert create_llm("mock:rules") is None


def test_create_llm_factory_invalid_provider():
    """Weryfikuje, że błędy tworzenia modelu są bezpiecznie przechwytywane i nie wywalają aplikacji."""
    llm = create_llm("unsupported_provider:non_existent_model_12345")
    assert llm is None


@pytest.mark.skipif(
    not os.getenv("RUN_LIVE_LLM_TESTS") or not os.getenv("LLM_API_KEY"),
    reason="Wymaga skonfigurowanego środowiska live LLM (zmienna RUN_LIVE_LLM_TESTS=1 oraz LLM_API_KEY)",
)
def test_live_llm_model_benchmark():
    """Test na żywo dla skonfigurowanego modelu (uruchamiany na żądanie przy podaniu klucza/endpointu)."""
    llm = create_llm()
    assert llm is not None, "Nie udało się zainicjalizować skonfigurowanego modelu LLM"

    # Pomiar czasu klasyfikacji i schematu
    t0 = time.perf_counter()
    structured = llm.with_structured_output(DispatchDecision)
    decision = structured.invoke([("user", "Dlaczego sprawa REK-2026-0412 stoi?")])
    latency_dispatch = (time.perf_counter() - t0) * 1000

    assert isinstance(decision, DispatchDecision)
    assert decision.intent in {"instance_diagnosis", "instance_details"}

    # Pomiar czasu generowania i ocena weryfikatora
    t1 = time.perf_counter()
    prompt = f"{COMPOSE_PROMPT}\n\nFakty:\n" + "\n".join(f"[{f['id']}] {f['text']}" for f in TEST_FACTS)
    resp = llm.invoke([("user", prompt)])
    latency_editor = (time.perf_counter() - t1) * 1000

    draft = getattr(resp, "content", str(resp))
    problems = check_draft(draft, TEST_FACTS, "Dlaczego sprawa REK-2026-0412 stoi?", "u_anna")

    print(f"\n[LIVE BENCHMARK] Model: {os.getenv('LLM_MODEL')}")
    print(f"Dispatcher Latency: {latency_dispatch:.1f} ms | Decision: {decision.intent}")
    print(f"Editor Latency: {latency_editor:.1f} ms")
    print(f"Draft:\n{draft}")
    print(f"Verifier Status: {'ACCEPTED' if not problems else 'REJECTED: ' + str(problems)}")
