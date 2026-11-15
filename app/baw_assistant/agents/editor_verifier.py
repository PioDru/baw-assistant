"""Agent 5 – Redaktor-Weryfikator: składa odpowiedź z wyników agentów i kontroluje jej jakość.

1. Scala fakty agentów pracujących równolegle i numeruje je [F1…], wybiera główny wniosek.
2. Składa odpowiedź: LLM (z odnośnikami do faktów) albo szablon (tryb mock / błąd modelu).
3. Sprawdza w kodzie (deterministycznie): każda liczba, data, godzina i numer sprawy występuje w faktach,
   odnośniki [F#] istnieją, numery spraw należą do zakresu uprawnień użytkownika.
4. Przy problemie prosi model o poprawkę (raz), a potem blokuje tekst modelu i zwraca wersję z samych faktów.
W trybie systemowym ta sama treść staje się powiadomieniem (wysyła je węzeł notifier).
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class EditorResponse(BaseModel):
    """Ustrukturyzowany schemat odpowiedzi Redaktora walidowany przez Pydantic."""
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    summary: str = Field(
        default="",
        description="Główna odpowiedź lub wniosek z odnośnikami [F#] do faktów"
    )
    used_facts: list[str] = Field(
        default_factory=list,
        description="Lista identyfikatorów użytych faktów, np. ['F1', 'F2']"
    )
    recommendations: list[str] = Field(
        default_factory=list,
        description="Rekomendacje dla użytkownika na podstawie faktów"
    )
    suggested_actions: list[str] = Field(
        default_factory=list,
        description="Sugerowane kroki / akcje do wykonania"
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_payload(cls, data: Any) -> Any:
        if isinstance(data, str):
            trimmed = data.strip()
            # Obsługa bloków kodu Markdown ```json ... ```
            code_block_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", trimmed, re.DOTALL)
            if code_block_match:
                try:
                    data = json.loads(code_block_match.group(1))
                except Exception:
                    return {"summary": trimmed}
            elif trimmed.startswith("{") and trimmed.endswith("}"):
                try:
                    data = json.loads(trimmed)
                except Exception:
                    return {"summary": trimmed}
            else:
                return {"summary": trimmed}

        if not isinstance(data, dict):
            return data

        res = dict(data)
        for key in ["direct_answer", "answer", "odpowiedz", "podsumowanie", "tekst", "text", "content"]:
            if key in res and "summary" not in res:
                res["summary"] = res.pop(key)
        for key in ["facts", "fakty", "uzyte_fakty", "citations", "zrodla", "sources"]:
            if key in res and "used_facts" not in res:
                res["used_facts"] = res.pop(key)
        for key in ["rekomendacje", "recommendation"]:
            if key in res and "recommendations" not in res:
                res["recommendations"] = res.pop(key)
        for key in ["actions", "kroki", "akcje", "sugerowane_kroki", "sugerowane_akcje", "steps"]:
            if key in res and "suggested_actions" not in res:
                res["suggested_actions"] = res.pop(key)
        return res

    def to_formatted_text(self) -> str:
        parts: list[str] = []
        if self.summary:
            parts.append(self.summary.strip())
        if self.recommendations:
            parts.append("Rekomendacje:\n" + "\n".join(f"- {r}" for r in self.recommendations))
        if self.suggested_actions:
            parts.append("Sugerowane kroki:\n" + "\n".join(f"{i}. {a}" for i, a in enumerate(self.suggested_actions, 1)))
        return "\n\n".join(parts) if parts else self.summary


def parse_editor_response(raw: Any) -> tuple[EditorResponse, str]:
    """Waliduje odpowiedź z wykorzystaniem schematu Pydantic EditorResponse."""
    if isinstance(raw, EditorResponse):
        return raw, "LLM"
    try:
        validated = EditorResponse.model_validate(raw)
        return validated, "LLM"
    except Exception as exc:
        log.warning("Błąd walidacji schematu Pydantic dla odpowiedzi: %s", exc)
        if isinstance(raw, str):
            return EditorResponse(summary=raw), "LLM"
        return EditorResponse(summary=str(raw)), "LLM"


COMPOSE_PROMPT = """Jesteś Redaktorem-Weryfikatorem w asystencie operacyjnym IBM BAW. Składasz zwięzłą odpowiedź po polsku
(do ok. 150 słów) wyłącznie na podstawie podanych faktów.

KRYTYCZNE ZASADY SPÓJNOŚCI DANYCH:
1. UŻYWAJ WYŁĄCZNIE DANYCH Z FAKTÓW:
   - Nie dodawaj żadnych własnych liczb, dat, godzin, wyliczeń, statystyk ani numerów spraw.
   - Przepisuj liczby i daty w zgodzie z faktami.
   - Nie twórz własnych sumarycznych statystyk (np. nie pisz „masz łącznie 3 zadania”, jeśli w faktach nie ma liczby 3).

2. OBOWIĄZKOWE PRZYPISY [F#]:
   - Każde zdanie zawierające informację z faktów (w szczególności liczbę, datę, nazwisko lub identyfikator) MUSI kończyć się odpowiednim znacznikiem, np. [F1] lub [F1][F2].
   - Używaj wyłącznie identyfikatorów istniejących na poniższej liście faktów.

3. STRUKTURA ODPOWIEDZI:
   - Zacznij od bezpośredniej odpowiedzi (wniosek główny), po czym przedstaw najważniejsze szczegóły.
   - Jeśli podano rekomendacje, umieść je na końcu w sekcji „Rekomendacje:” (lista punktowana, bez dopisywania nowych liczb).
   - Jeśli podano akcje sugerowane, umieść je w sekcji „Sugerowane kroki:” (dokładna lista podanych akcji).
   - Dopuszczalny jest format JSON zgodny ze schematem: {"summary": "...", "used_facts": ["F1", ...], "recommendations": [...], "suggested_actions": [...]}.

4. Po sformułowaniu odpowiedzi natychmiast zakończ generowanie.

PRZYKŁAD WEJŚCIA:
Fakty:
[F1] Sprawa REK-2026-0412 oczekuje na decyzję od 4 dni.
[F2] Termin upływa 21.10.2026.
Wniosek główny: Sprawa wymaga pilnej akcji.

PRZYKŁAD POPRAWNEJ ODPOWIEDZI:
Sprawa REK-2026-0412 wymaga pilnej akcji ze względu na zbliżający się termin [F1][F2]. Postępowanie oczekuje na decyzję od 4 dni [F1], a ostateczny termin realizacji upływa 21.10.2026 [F2]."""

EVENT_PROMPT = """Jesteś Redaktorem-Weryfikatorem w asystencie IBM BAW. Piszesz zwięzłe powiadomienie po polsku (do 50-80 słów) na podstawie faktów.
ZASADY:
1. Używaj WYŁĄCZNIE podanych faktów. Nie wymyślaj żadnych danych.
2. Po KAŻDYM zdaniu z liczbą, datą, statystyką lub nazwą sprawy MUSISZ dodać odnośnik do faktu, np. [F1], [F2].
3. Zacznij bezpośrednio od tego, co adresat powinien wiedzieć lub zrobić.
4. Po napisaniu powiadomienia natychmiast zakończ generowanie.

Przykład:
Fakty: [F1] Skan cykliczny: nowe alerty 0.
Odpowiedź: Skan cykliczny zakończony pomyślnie – nie wykryto nowych alertów [F1]. Brak wymaganych działań."""


# --- weryfikacja ----------------------------------------------------------------------

POLISH_MONTHS = {
    "stycznia": "01", "styczeń": "01", "styczniu": "01",
    "lutego": "02", "luty": "02", "lutym": "02",
    "marca": "03", "marzec": "03", "marcu": "03",
    "kwietnia": "04", "kwiecień": "04", "kwietniu": "04",
    "maja": "05", "maj": "05", "maju": "05",
    "czerwca": "06", "czerwiec": "06", "czerwcu": "06",
    "lipca": "07", "lipiec": "07", "lipcu": "07",
    "sierpnia": "08", "sierpień": "08", "sierpniu": "08",
    "września": "09", "wrzesień": "09", "wrzesniu": "09", "wrześniu": "09",
    "października": "10", "październik": "10", "pazdziernika": "10", "październiku": "10", "pazdzierniku": "10",
    "listopada": "11", "listopad": "11", "listopadzie": "11",
    "grudnia": "12", "grudzień": "12", "grudniu": "12", "grudzien": "12",
}

POLISH_WORD_NUMBERS = {
    "zero": "0", "zerem": "0", "zerze": "0",
    "jeden": "1", "jedna": "1", "jedno": "1", "jednego": "1", "jednej": "1", "jednym": "1", "jedną": "1",
    "dwa": "2", "dwie": "2", "dwóch": "2", "dwoch": "2", "dwom": "2", "dwoma": "2", "dwiema": "2",
    "trzy": "3", "trzech": "3", "trzem": "3", "trzema": "3",
    "cztery": "4", "czterech": "4", "czterem": "4", "czterema": "4",
    "pięć": "5", "piec": "5", "pięciu": "5", "pieciu": "5", "pięcioma": "5",
    "sześć": "6", "szesc": "6", "sześciu": "6", "szesciu": "6", "sześcioma": "6",
    "siedem": "7", "siedmiu": "7", "siedmioma": "7",
    "osiem": "8", "ośmiu": "8", "osmiu": "8", "ośmioma": "8", "osmioma": "8",
    "dziewięć": "9", "dziewiec": "9", "dziewięciu": "9", "dziewieciu": "9", "dziewięcioma": "9",
    "dziesięć": "10", "dziesiec": "10", "dziesięciu": "10", "dziesieciu": "10", "dziesięcioma": "10",
}

def normalize_citations(text: str) -> str:
    """Normalizuje różne warianty formatowania odnośników generowanych przez modele do standardu [F#].

    Obsługiwane warianty:
    - [F1, F2], (F1, F2), [F1; F2], (F1; F2), [F1 i F2], (F1 oraz F2), [F1 and F2], 【F1, F2】 -> [F1][F2]
    - (F1), (f1), [f1], 【F1】, [^F1], [F 1], (F 1), [F1.], (F1.) -> [F1]
    - [F1-F3], (F1-F3), [1-3], [1 - 3], [F1 - F3] -> [F1][F2][F3]
    - [Fakt 1], (Fakt 1), [Fakt F1], (Fakt F1), [fakt 1], (fakt 1) -> [F1]
    - [Fakt 1, 2], [Fakty 1, 2], (Fakty: F1, F2), (fakty 1 i 2) -> [F1][F2]
    - [źródło: F1], (źródła: F1, F2), [źródło: 1, 2], (zob. fakt 1) -> [F1], [F1][F2]
    - [1], [1, 2], [1; 2], [1 i 2], 【1】, 【1, 2】, [^1] -> [F1], [F1][F2]
    - Wzmianki tekstowe: zgodnie z faktem 1, wg F1, na podstawie faktów F1 i F2 -> [F1], [F1][F2]
    """
    if not text:
        return text

    # Zamiana nawiasów pełnej szerokości Unicode na standardowe
    text = text.replace("【", "[").replace("】", "]")

    # 1. Rozwijanie zakresów faktów, np. [F1-F3], (F1-F3), [1-3], [F1 - F3]
    def _expand_range(m: re.Match) -> str:
        s_num, e_num = int(m.group(1)), int(m.group(2))
        if 1 <= s_num <= e_num <= 50 and (e_num - s_num) <= 10:
            return "".join(f"[F{i}]" for i in range(s_num, e_num + 1))
        return m.group(0)

    text = re.sub(
        r"[\[\(]\s*(?:fakt(?:y)?|źródł[oa]|źródła|source(?:s)?|ref)?\s*:?\s*F?(\d+)\s*[-–]\s*F?(\d+)\s*[\]\)]",
        _expand_range,
        text,
        flags=re.IGNORECASE,
    )

    # 2. Prefiksy słowne typu: [Fakt 1], (Fakt 1), [Fakt F1], (źródło: F1), (źródła: 1, 2), (zob. fakt 1)
    def _replace_keyword_group(m: re.Match) -> str:
        nums = re.findall(r"\bF?(\d+)\b", m.group(0), flags=re.IGNORECASE)
        if nums:
            return "".join(f"[F{n}]" for n in nums)
        return m.group(0)

    text = re.sub(
        r"[\[\(]\s*(?:(?:zob\.|patrz|wg|według)\s+)?(?:fakt(?:y)?|źródł[oa]|źródła|source(?:s)?|ref(?:erence)?)\s*:?\s*[^\]\)]+[\]\)]",
        _replace_keyword_group,
        text,
        flags=re.IGNORECASE,
    )

    # 3. Grupy z literą F/f w nawiasach kwadratowych lub okrągłych, np. (F1), [f1], [F1, F2], [F 1], [F1.]
    def _replace_f_group(m: re.Match) -> str:
        nums = re.findall(r"F\s*(\d+)", m.group(0), flags=re.IGNORECASE)
        if nums:
            return "".join(f"[F{n}]" for n in nums)
        return m.group(0)

    text = re.sub(
        r"[\[\(]\s*(?:\^?F\s*\d+\.?\s*(?:[,;/& ]|\bi\b|\boraz\b|\band\b)*)+\s*[\]\)]",
        _replace_f_group,
        text,
        flags=re.IGNORECASE,
    )

    # 4. Przypisy w nawiasach kwadratowych z samymi liczbami / przypisy dolne, np. [1], [1, 2], [^1], [1 i 2]
    def _replace_num_bracket_group(m: re.Match) -> str:
        nums = re.findall(r"\d+", m.group(0))
        if nums:
            return "".join(f"[F{n}]" for n in nums)
        return m.group(0)

    text = re.sub(
        r"\[\s*\^?\d+(?:\s*(?:[,;/& ]|\bi\b|\boraz\b|\band\b)\s*\^?\d+)*\s*\]",
        _replace_num_bracket_group,
        text,
    )

    # 5. Odwołania tekstowe bez nawiasów, np. „zgodnie z faktem 1”, „wg F1”, „w fakcie F2”
    text = re.sub(
        r"\b(?:zgodnie z|wg|według|na podstawie|w oparciu o|patrz|zob\.)\s+(?:(?:fakt(?:em|u|ów|y|ie)?|źródł(?:em|a|o|ach)?)\s+)?F?(\d+)\b",
        r"[F\1]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(?:fakt(?:u|y|ów|em|ie)?|źródł(?:o|a|em|ach)?)\s+F(\d+)\b",
        r"[F\1]",
        text,
        flags=re.IGNORECASE,
    )

    return text


def _clean_formatting_and_markers(text: str) -> str:
    """Usuwa znaczniki list, numerację kroków, nagłówki i przypisy, aby nie były traktowane jako dane."""
    text = normalize_citations(text)
    text = CITATION_RE.sub(" ", text)

    # Usuwanie nagłówków Markdown (np. ### 1., ## Krok 1)
    text = re.sub(r"^\s*#{1,6}\s*(?:\d+[.)]\s*)?", " ", text, flags=re.MULTILINE)

    # Usuwanie znaczników list numerowanych (np. 1., 1), - 1., * 1., **1.**, (1))
    text = re.sub(r"^\s*(?:[-*+•]\s*)?(?:\*\*)?\d+[.)]\s*(?:\*\*)?\s*", " ", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*(?:[-*+•]\s*)?\(\d+\)\s*", " ", text, flags=re.MULTILINE)

    # Usuwanie etykiet kroków/wariantów/zadań (np. Krok 1:, Punkt 2., Zadanie 1:, Wariant 1:)
    text = re.sub(
        r"(?:\b|\s)(?:Krok|Kroki|Punkt|Punkty|Zadanie|Zadania|Wariant|Warianty|Opcja|Opcje|Etap|Etapy)\s+\d+(?:[:.)\s]|\b)",
        " ",
        text,
        flags=re.IGNORECASE,
    )

    # Usuwanie numeracji wewnątrz tekstu po interpunkcji (np. „kroki: 1. ... 2. ...”)
    text = re.sub(r"(?<=[,.;:\n])\s*(?:[-*+•]\s*)?\d+[.)]\s+", " ", text)

    # Usuwanie oznaczeń wersji / PoC (np. PoC v1, PoC v2, wersja 1.0)
    text = re.sub(r"\b(?:PoC\s+)?v\d+(?:\.\d+)?\b", " ", text, flags=re.IGNORECASE)

    # Usuwanie procentów i zwrotów typu 100%, 0%, 24/7
    text = re.sub(r"\b\d+(?:[.,]\d+)?\s*%", " ", text)
    text = re.sub(r"\b24/7\b", " ", text)

    # Usuwanie pozostałych symboli F#
    text = re.sub(r"\bF\d+\b", " ", text, flags=re.IGNORECASE)

    return text


def _extract_instances(text: str) -> tuple[set[str], str]:
    """Wyszukuje identyfikatory spraw (np. REK-2026-0412, FAK-2026-1162)."""
    instances: set[str] = set()

    def _replace_inst(m: re.Match) -> str:
        val = m.group(0).upper()
        instances.add(val)
        return " "

    text = re.sub(r"\b[A-Za-z]{2,6}-\d{4}-\d{4}\b", _replace_inst, text)
    return instances, text


def _extract_times(text: str) -> tuple[set[str], str]:
    """Wyszukuje i normalizuje godziny (np. 11:30, 11:30:00, godz. 11.30)."""
    times: set[str] = set()

    def _replace_time_colon(m: re.Match) -> str:
        h, mn = m.group(1), m.group(2)
        times.add(f"{h}:{mn}")
        return " "

    # Godziny z dwukropkiem (11:30, 11:30:00)
    text = re.sub(r"\b(\d{1,2}):(\d{2})(?::\d{2})?\b", _replace_time_colon, text)

    # Godziny z kropką po słowie godz./godzinie/o godz./g. (np. o godz. 11.30, godz. 11.30)
    def _replace_time_dot(m: re.Match) -> str:
        h, mn = m.group(1), m.group(2)
        times.add(f"{h}:{mn}")
        return " "

    text = re.sub(r"\b(?:godz(?:inie|\.)?\s+|g\.\s*|o\s+godz(?:inie|\.)?\s*)(\d{1,2})\.(\d{2})\b", _replace_time_dot, text, flags=re.IGNORECASE)

    return times, text


def _extract_dates(text: str) -> tuple[set[str], str]:
    """Wyszukuje i normalizuje daty (np. 21.10.2026, 21.10, 2026-10-21, 21 października 2026)."""
    dates: set[str] = set()

    # 1. Daty w formacie słownym z polskim miesiącem: np. 21 października 2026, 14 października
    month_pattern = "|".join(POLISH_MONTHS.keys())

    def _replace_named_month(m: re.Match) -> str:
        dd, m_name, yyyy = m.group(1), m.group(2).lower(), m.group(3)
        mm = POLISH_MONTHS.get(m_name, "01")
        if yyyy:
            formatted = f"{int(dd):02d}.{mm}.{yyyy}"
            short_formatted = f"{int(dd):02d}.{mm}"
            dates.add(formatted)
            dates.add(short_formatted)
        else:
            short_formatted = f"{int(dd):02d}.{mm}"
            dates.add(short_formatted)
        return " "

    text = re.sub(
        rf"\b(\d{{1,2}})\.?(?:\s+dzień\s+)?\s+({month_pattern})(?:\s+(\d{{4}}))?\b",
        _replace_named_month,
        text,
        flags=re.IGNORECASE,
    )

    # 2. Daty w formacie ISO (YYYY-MM-DD) -> zamiana na DD.MM.YYYY i DD.MM
    def _replace_iso_date(m: re.Match) -> str:
        y, mm, dd = m.group(1), m.group(2), m.group(3)
        formatted = f"{int(dd):02d}.{int(mm):02d}.{y}"
        short_formatted = f"{int(dd):02d}.{int(mm):02d}"
        dates.add(formatted)
        dates.add(short_formatted)
        return " "

    text = re.sub(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", _replace_iso_date, text)

    # 3. Daty standardowe DD.MM.YYYY lub DD.MM (z kropkami)
    def _replace_std_date(m: re.Match) -> str:
        val = m.group(0)
        dates.add(val)
        parts = val.split(".")
        if len(parts) == 3:
            dates.add(f"{int(parts[0]):02d}.{int(parts[1]):02d}")
        return " "

    text = re.sub(r"\b\d{1,2}\.\d{1,2}(?:\.\d{4})?\b", _replace_std_date, text)
    return dates, text


def _parse_num(val: str) -> float | None:
    try:
        return float(val.replace(",", "."))
    except (ValueError, TypeError):
        return None


def _tokens(text: str, is_allowed_source: bool = False) -> dict[str, set[str]]:
    text = _clean_formatting_and_markers(text)

    # 1. Wyciągamy identyfikatory spraw
    instances, text = _extract_instances(text)
    found: dict[str, set[str]] = {"instance": instances}

    # 2. Wyciągamy godziny z prefiksem lub dwukropkiem PRZED ogólnymi datami
    times, text = _extract_times(text)
    found["time"] = times

    # 3. Wyciągamy daty
    dates, text = _extract_dates(text)
    found["date"] = dates

    # 4. Wyciągamy liczby słowne i zamieniamy na cyfry
    word_pattern = r"\b(" + "|".join(POLISH_WORD_NUMBERS.keys()) + r")\b"
    word_numbers: set[str] = set()
    for w in re.findall(word_pattern, text, flags=re.IGNORECASE):
        d = POLISH_WORD_NUMBERS.get(w.lower())
        if d:
            word_numbers.add(d)
    text = re.sub(word_pattern, " ", text, flags=re.IGNORECASE)

    # 5. Wyciągamy liczby
    raw_numbers = NUMBER_RE.findall(text)
    numbers: set[str] = {n.replace(".", ",") for n in raw_numbers} | word_numbers

    # Normalizacja liczb zmiennoprzecinkowych (np. 4.0 -> 4)
    for n in list(numbers):
        num_f = _parse_num(n)
        if num_f is not None:
            if num_f.is_integer():
                numbers.add(str(int(num_f)))
            else:
                numbers.add(f"{num_f:g}".replace(".", ","))

    # Dla dozwolonego źródła dodajemy pod-tokeny (składowe spraw oraz słowa oznaczające brak/0)
    if is_allowed_source:
        for inst in instances:
            for part in re.findall(r"\d+", inst):
                numbers.add(part)
                num_f = _parse_num(part)
                if num_f is not None:
                    numbers.add(str(int(num_f)))

        # Słowa oznaczające brak / 0
        if re.search(r"\b(?:brak(?:u|iem)?|brakujące|nie wykryto|żadnych|zero)\b", text, re.IGNORECASE):
            numbers.add("0")

    found["number"] = numbers
    return found


def check_draft(draft: str, facts: list[Fact], question: str = "", user_id: str = "u_system",
                extra_context: list[str] | str | None = None) -> list[str]:
    """Zwraca listę problemów (pusta = odpowiedź przechodzi weryfikację)."""
    normalized_draft = normalize_citations(draft)
    problems: list[str] = []
    fact_ids = {f["id"] for f in facts}

    allowed_parts = [f["text"] for f in facts] + [question]
    if extra_context:
        if isinstance(extra_context, str):
            allowed_parts.append(extra_context)
        elif isinstance(extra_context, (list, tuple, set)):
            allowed_parts.extend(str(x) for x in extra_context if x)

    allowed = _tokens(" ".join(allowed_parts), is_allowed_source=True)
    got = _tokens(normalized_draft, is_allowed_source=False)

    unknown_refs = sorted(set(CITATION_RE.findall(normalized_draft)) - fact_ids)
    if unknown_refs:
        problems.append("odnośniki do nieistniejących faktów: " + ", ".join(unknown_refs))
    if facts and not CITATION_RE.search(normalized_draft):
        problems.append("brak odnośników [F#] do faktów")

    # Sprawdzenie spraw
    extra_instances = sorted(got["instance"] - allowed["instance"])
    if extra_instances:
        problems.append("numery spraw bez pokrycia w danych: " + ", ".join(extra_instances))

    # Sprawdzenie dat
    extra_dates = sorted(got["date"] - allowed["date"])
    if extra_dates:
        problems.append("daty bez pokrycia w danych: " + ", ".join(extra_dates))

    # Sprawdzenie godzin
    extra_times = sorted(got["time"] - allowed["time"])
    if extra_times:
        problems.append("godziny bez pokrycia w danych: " + ", ".join(extra_times))

    # Sprawdzenie liczb (z tolerancją na równoważność numeryczną)
    allowed_numeric_vals = {_parse_num(n) for n in allowed["number"] if _parse_num(n) is not None}
    extra_numbers = []
    for num_str in sorted(got["number"]):
        num_f = _parse_num(num_str)
        if num_str not in allowed["number"] and (num_f is None or num_f not in allowed_numeric_vals):
            extra_numbers.append(num_str)

    if extra_numbers:
        problems.append("liczby bez pokrycia w danych: " + ", ".join(extra_numbers))

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
    on_token = state.get("on_token")
    try:
        messages = [("system", EVENT_PROMPT if event_mode else COMPOSE_PROMPT), ("user", msg)]
        if hasattr(llm, "stream"):
            chunks: list[str] = []
            for chunk in llm.stream(messages):
                text = chunk.content if isinstance(chunk.content, str) else "".join(
                    part.get("text", "") for part in chunk.content if isinstance(part, dict))
                chunks.append(text)
                if on_token and callable(on_token) and text:
                    on_token(text)
            content = "".join(chunks)
        else:
            result = llm.invoke(messages)
            content = result.content if isinstance(result.content, str) else "".join(
                part.get("text", "") for part in result.content if isinstance(part, dict))
            if on_token and callable(on_token) and content:
                on_token(content)
        parsed_resp, method = parse_editor_response(content)
        formatted = parsed_resp.to_formatted_text()
        return (formatted.strip() or content.strip()), method
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
                                      "bez udziału modelu.", "ms": elapsed_ms(started),
                           "details": {"verdict": verdict, "decision": decision,
                                       "decision_reason": state.get("decision_reason", "")}}]}

    facts = merge_facts(state.get("facts", []))
    lead = pick_lead(state.get("leads", []))
    actions = _dedupe(state.get("suggested_actions", []))
    recs = _dedupe(state.get("recommendations", []))
    question = state.get("question", "")
    steps, feedback, problems, draft, method = [], "", [], "", ""
    attempts_details: list[dict] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        draft, method = compose(state, facts, lead, actions, recs, feedback)
        draft = normalize_citations(draft)
        problems = check_draft(draft, facts, question, state["user_id"],
                               extra_context=[lead] + actions + recs + [state.get("context_instance_id") or ""])
        attempts_details.append({
            "attempt": attempt,
            "method": method,
            "draft": draft,
            "problems": list(problems),
            "accepted": not bool(problems),
        })
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
                                      f"({len(cited)} odnośników). Uprawnienia: OK.{fixes}", "ms": elapsed_ms(started),
                           "details": {"verdict": "ok", "attempts_count": attempt,
                                       "attempts": attempts_details, "rejection_reasons": steps}}]}

    safe = template_answer(lead, facts, actions, recs)
    note = "\n\n_Weryfikator zablokował tekst modelu (brak pokrycia w danych) i pokazuje same fakty._"
    return {**base, "answer": safe + note, "verdict": "blocked", "sources": facts,
            "trace": [{"agent": "Redaktor-Weryfikator",
                       "summary": f"Zablokował tekst modelu po {MAX_ATTEMPTS} próbach ({'; '.join(steps)}). "
                                  "Zwrócił wersję faktograficzną.", "ms": elapsed_ms(started),
                       "details": {"verdict": "blocked", "attempts_count": MAX_ATTEMPTS,
                                   "attempts": attempts_details, "rejection_reasons": steps}}]}
