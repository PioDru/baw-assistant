"""Agent 5 – Redaktor-Weryfikator: kontrola jakości i jedyny kanał do użytkownika.

Sprawdza w kodzie (deterministycznie):
- grounding: każda liczba, data, godzina i numer sprawy z odpowiedzi występuje w faktach,
- odnośniki [F#] wskazują istniejące fakty,
- numery spraw w odpowiedzi należą do zakresu uprawnień użytkownika.
Przy problemie raz odsyła odpowiedź do Agenta Instancji z uwagami; za drugim razem blokuje
tekst modelu i zwraca wersję złożoną wyłącznie z faktów.
"""
from __future__ import annotations

import re
import time

from ..rbac import can_view_instance, get_user
from ..state import AssistantState, Fact
from .instance_agent import template_answer

MAX_ATTEMPTS = 2

CITATION_RE = re.compile(r"\[(F\d+)\]")
INSTANCE_RE = re.compile(r"\b[A-Z]{3}-\d{4}-\d{4}\b")
DATE_RE = re.compile(r"\b\d{1,2}\.\d{1,2}(?:\.\d{4})?\b")
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\b")
NUMBER_RE = re.compile(r"\b\d+(?:[.,]\d+)?\b")
LIST_MARKER_RE = re.compile(r"^\s*\d+[.)]\s", re.MULTILINE)


def _tokens(text: str) -> dict[str, set[str]]:
    text = CITATION_RE.sub(" ", text)
    text = LIST_MARKER_RE.sub(" ", text)
    found: dict[str, set[str]] = {"instance": set(INSTANCE_RE.findall(text))}
    text = INSTANCE_RE.sub(" ", text)
    found["date"] = set(DATE_RE.findall(text))
    text = DATE_RE.sub(" ", text)
    found["time"] = set(TIME_RE.findall(text))
    text = TIME_RE.sub(" ", text)
    found["number"] = {n.replace(".", ",") for n in NUMBER_RE.findall(text)}
    return found


def check_draft(draft: str, facts: list[Fact], question: str, user_id: str) -> list[str]:
    """Zwraca listę problemów (pusta = odpowiedź przechodzi weryfikację)."""
    problems: list[str] = []
    fact_ids = {f["id"] for f in facts}
    corpus = " ".join(f["text"] for f in facts) + " " + question
    allowed = _tokens(corpus)
    got = _tokens(draft)

    unknown_refs = sorted(set(CITATION_RE.findall(draft)) - fact_ids)
    if unknown_refs:
        problems.append("odnośniki do nieistniejących faktów: " + ", ".join(unknown_refs))
    if facts and not CITATION_RE.search(draft):
        problems.append("brak odnośników [F#] do faktów")
    for kind, label in (("instance", "numery spraw"), ("date", "daty"), ("time", "godziny"),
                        ("number", "liczby")):
        extra = sorted(got[kind] - allowed[kind])
        if extra:
            problems.append(f"{label} bez pokrycia w danych: " + ", ".join(extra))
    user = get_user(user_id)
    forbidden = sorted(i for i in got["instance"] if not can_view_instance(user, i))
    if forbidden:
        problems.append("numery spraw spoza uprawnień: " + ", ".join(forbidden))
    return problems


def editor_verifier_node(state: AssistantState) -> AssistantState:
    started = time.perf_counter()
    decision = state.get("decision", "route")

    if decision != "route":
        # Odmowa lub informacja od Dyspozytora – bez udziału LLM.
        prefix = {"refuse": "Nie mogę odpowiedzieć na to pytanie.", "need_context": "Potrzebuję kontekstu.",
                  "not_available": "Ta funkcja nie jest jeszcze dostępna.",
                  "not_found": "Nie znalazłem tej sprawy."}[decision]
        answer = f"{prefix} {state.get('decision_reason', '')}".strip()
        verdict = "refused" if decision == "refuse" else "info"
        ms = int((time.perf_counter() - started) * 1000)
        return {"answer": answer, "verdict": verdict, "sources": [], "suggested_actions": [],
                "trace": [{"agent": "Redaktor-Weryfikator",
                           "summary": f"{'Odmowa przekazana' if verdict == 'refused' else 'Komunikat przekazany'} "
                                      "użytkownikowi bez udziału modelu.", "ms": ms}]}

    facts = state.get("facts", [])
    problems = check_draft(state.get("draft", ""), facts, state["question"], state["user_id"])
    ms = int((time.perf_counter() - started) * 1000)
    cited = sorted(set(CITATION_RE.findall(state.get("draft", ""))), key=lambda x: int(x[1:]))
    sources = [f for f in facts if f["id"] in cited] or facts

    if not problems:
        return {"answer": state["draft"], "verdict": "ok", "sources": sources,
                "trace": [{"agent": "Redaktor-Weryfikator",
                           "summary": f"Zgodność z danymi: OK ({len(cited)} odnośników). Uprawnienia: OK. "
                                      "Odpowiedź przekazana.", "ms": ms}]}

    if state.get("attempts", 0) < MAX_ATTEMPTS:
        return {"verdict": "retry", "feedback": "; ".join(problems),
                "trace": [{"agent": "Redaktor-Weryfikator",
                           "summary": "Odesłano do poprawy: " + "; ".join(problems), "ms": ms}]}

    safe = template_answer(state["lead"], facts, state.get("suggested_actions", []))
    note = "\n\n_Weryfikator zablokował tekst modelu (brak pokrycia w danych) i pokazuje same fakty._"
    return {"answer": safe + note, "verdict": "blocked", "sources": facts,
            "trace": [{"agent": "Redaktor-Weryfikator",
                       "summary": "Zablokowano tekst modelu po 2 próbach: " + "; ".join(problems)
                                  + ". Zwrócono wersję faktograficzną.", "ms": ms}]}
