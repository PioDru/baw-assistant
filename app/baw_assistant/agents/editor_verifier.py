"""Agent 5 – Redaktor-Weryfikator: składa odpowiedź z wyników agentów i kontroluje jej jakość.

1. Scala fakty agentów pracujących równolegle i numeruje je [F1…], wybiera główny wniosek.
2. Składa odpowiedź: LLM (z odnośnikami do faktów) albo szablon (tryb mock / błąd modelu).
3. Sprawdza w kodzie (deterministycznie): każda liczba, data, godzina i numer sprawy występuje w faktach,
   odnośniki [F#] istnieją, numery spraw należą do zakresu uprawnień użytkownika.
4. Przy problemie prosi model o poprawkę (raz), a potem blokuje tekst modelu i zwraca wersję z samych faktów.
W trybie systemowym ta sama treść staje się powiadomieniem (wysyła je węzeł notifier).
"""
from __future__ import annotations

import logging
import re
import time

from ..llm import get_llm
from ..rbac import can_view_instance, get_user
from ..state import AssistantState, Fact
from .common import AGENT_ORDER, elapsed_ms

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 2

CITATION_RE = re.compile(r"\[(F\d+)\]")
INSTANCE_RE = re.compile(r"\b[A-Z]{3}-\d{4}-\d{4}\b")
DATE_RE = re.compile(r"\b\d{1,2}\.\d{1,2}(?:\.\d{4})?\b")
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\b")
NUMBER_RE = re.compile(r"\b\d+(?:[.,]\d+)?\b")
LIST_MARKER_RE = re.compile(r"^\s*\d+[.)]\s", re.MULTILINE)

DECISION_PREFIX = {"refuse": "Nie mogę odpowiedzieć na to pytanie.", "need_context": "Potrzebuję kontekstu.",
                   "not_found": "Nie znalazłem tej sprawy.", "disabled": "Ta funkcja jest chwilowo niedostępna."}

COMPOSE_PROMPT = """Jesteś Redaktorem-Weryfikatorem w asystencie operacyjnym IBM BAW. Składasz odpowiedź po polsku
z faktów zebranych przez agentów. Pisz zwięźle (do ok. 150 słów, potem listy).
Zasady:
- Korzystaj WYŁĄCZNIE z faktów z listy. Nie dodawaj liczb, dat, godzin, nazw ani numerów spraw spoza faktów.
- Po każdym zdaniu z liczbą, datą lub nazwą dodaj odnośnik do faktu, np. [F2].
- Zacznij od bezpośredniej odpowiedzi (wniosek główny), potem najważniejsze fakty.
- Jeśli podano rekomendacje, dodaj sekcję „Rekomendacje:” (lista punktowana, bez nowych liczb).
- Jeśli podano akcje sugerowane, zakończ sekcją „Sugerowane kroki:” z listą numerowaną tylko tych akcji."""

EVENT_PROMPT = """Jesteś Redaktorem-Weryfikatorem w asystencie IBM BAW. Piszesz krótkie powiadomienie po polsku
(do ok. 80 słów) dla adresata zdarzenia systemowego. Korzystaj WYŁĄCZNIE z faktów; po zdaniach z liczbą, datą
lub nazwą dodaj odnośnik [F#]. Zacznij od tego, co adresat powinien wiedzieć lub zrobić."""


# --- weryfikacja ----------------------------------------------------------------------

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
    allowed = _tokens(" ".join(f["text"] for f in facts) + " " + question)
    got = _tokens(draft)

    unknown_refs = sorted(set(CITATION_RE.findall(draft)) - fact_ids)
    if unknown_refs:
        problems.append("odnośniki do nieistniejących faktów: " + ", ".join(unknown_refs))
    if facts and not CITATION_RE.search(draft):
        problems.append("brak odnośników [F#] do faktów")
    for kind, label in (("instance", "numery spraw"), ("date", "daty"), ("time", "godziny"), ("number", "liczby")):
        extra = sorted(got[kind] - allowed[kind])
        if extra:
            problems.append(f"{label} bez pokrycia w danych: " + ", ".join(extra))
    user = get_user(user_id)
    forbidden = sorted(i for i in got["instance"] if not can_view_instance(user, i))
    if forbidden:
        problems.append("numery spraw spoza uprawnień: " + ", ".join(forbidden))
    return problems


# --- składanie odpowiedzi -----------------------------------------------------------

def merge_facts(facts: list[Fact]) -> list[Fact]:
    """Kolejność agentów jest stała (niezależna od kolejności zakończenia pracy równoległej)."""
    order = {a: i for i, a in enumerate(AGENT_ORDER)}
    ranked = sorted(enumerate(facts), key=lambda p: (order.get(p[1].get("agent", ""), 9), p[0]))
    return [{**f, "id": f"F{n}"} for n, (_, f) in enumerate(ranked, 1)]


def pick_lead(leads: list[dict]) -> str:
    if not leads:
        return "Oto, co wiem:"
    order = {a: i for i, a in enumerate(AGENT_ORDER)}
    return sorted(leads, key=lambda x: (x["priority"], order.get(x["agent"], 9)))[0]["text"]


def template_answer(lead: str, facts: list[Fact], actions: list[str], recommendations: list[str] | None = None) -> str:
    parts = [lead, ""] + [f"- {f['text']} [{f['id']}]" for f in facts]
    if recommendations:
        parts += ["", "Rekomendacje:"] + [f"- {r}" for r in recommendations]
    if actions:
        parts += ["", "Sugerowane kroki:"] + [f"{i}. {a}" for i, a in enumerate(actions, 1)]
    return "\n".join(parts)


def compose(state: AssistantState, facts: list[Fact], lead: str, actions: list[str], recs: list[str],
            feedback: str = "") -> tuple[str, str]:
    """Zwraca (tekst, metoda)."""
    llm = get_llm()
    if llm is None:
        return template_answer(lead, facts, actions, recs), "szablon"
    event_mode = state.get("mode") == "event"
    facts_txt = "\n".join(f"[{f['id']}] {f['text']}" for f in facts)
    msg = (f"{'Zdarzenie' if event_mode else 'Pytanie'}: {state.get('question', '')}\n"
           f"Rola użytkownika: {get_user(state['user_id']).role_label}\n\nWniosek główny: {lead}\n\nFakty:\n{facts_txt}"
           f"\n\nRekomendacje:\n" + ("\n".join(f"- {r}" for r in recs) or "(brak)")
           + "\n\nAkcje sugerowane:\n" + ("\n".join(f"- {a}" for a in actions) or "(brak)"))
    if feedback:
        msg += f"\n\nPoprzednia wersja nie przeszła weryfikacji: {feedback}\nPopraw odpowiedź."
    try:
        result = llm.invoke([("system", EVENT_PROMPT if event_mode else COMPOSE_PROMPT), ("user", msg)])
        content = result.content if isinstance(result.content, str) else "".join(
            part.get("text", "") for part in result.content if isinstance(part, dict))
        return content.strip(), "LLM"
    except Exception as exc:
        log.warning("Składanie odpowiedzi przez LLM nie powiodło się, używam szablonu: %s", exc)
        return template_answer(lead, facts, actions, recs), "szablon"


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def editor_verifier_node(state: AssistantState) -> AssistantState:
    started = time.perf_counter()
    decision = state.get("decision", "route")
    if decision != "route":
        answer = f"{DECISION_PREFIX[decision]} {state.get('decision_reason', '')}".strip()
        verdict = "refused" if decision == "refuse" else "info"
        return {"answer": answer, "verdict": verdict, "sources": [], "numbered_facts": [],
                "trace": [{"agent": "Redaktor-Weryfikator",
                           "summary": f"{'Odmowa przekazana' if verdict == 'refused' else 'Komunikat przekazany'} "
                                      "bez udziału modelu.", "ms": elapsed_ms(started)}]}

    facts = merge_facts(state.get("facts", []))
    lead = pick_lead(state.get("leads", []))
    actions = _dedupe(state.get("suggested_actions", []))
    recs = _dedupe(state.get("recommendations", []))
    question = state.get("question", "")
    steps, feedback, problems, draft, method = [], "", [], "", ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        draft, method = compose(state, facts, lead, actions, recs, feedback)
        problems = check_draft(draft, facts, question, state["user_id"])
        if not problems:
            break
        feedback = "; ".join(problems)
        steps.append(f"próba {attempt}: {feedback}")

    cited = sorted(set(CITATION_RE.findall(draft)), key=lambda x: int(x[1:]))
    base = {"numbered_facts": facts, "lead": lead, "draft": draft, "attempts": attempt}
    if not problems:
        fixes = f" Poprawki: {len(steps)} ({'; '.join(steps)})." if steps else ""
        return {**base, "answer": draft, "verdict": "ok", "sources": [f for f in facts if f["id"] in cited] or facts,
                "trace": [{"agent": "Redaktor-Weryfikator",
                           "summary": f"Złożył odpowiedź ({method}, {len(facts)} faktów). Zgodność z danymi: OK "
                                      f"({len(cited)} odnośników). Uprawnienia: OK.{fixes}", "ms": elapsed_ms(started)}]}

    safe = template_answer(lead, facts, actions, recs)
    note = "\n\n_Weryfikator zablokował tekst modelu (brak pokrycia w danych) i pokazuje same fakty._"
    return {**base, "answer": safe + note, "verdict": "blocked", "sources": facts,
            "trace": [{"agent": "Redaktor-Weryfikator",
                       "summary": f"Zablokował tekst modelu po {MAX_ATTEMPTS} próbach ({'; '.join(steps)}). "
                                  "Zwrócił wersję faktograficzną.", "ms": elapsed_ms(started)}]}
