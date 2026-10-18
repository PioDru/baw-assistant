"""Węzeł powiadomień (tryb systemowy): wysyła zweryfikowany wynik do adresatów zdarzenia.

To część odpowiedzialności Redaktora-Weryfikatora („jedyny agent komunikujący się na zewnątrz”),
wydzielona jako osobny węzeł grafu. W PoC wysyłka jest symulowana (tabela notifications).
"""
from __future__ import annotations

import re
import time

from .. import notifications
from ..state import AssistantState
from .common import elapsed_ms

SUBJECTS = {"event_document": "Nowy dokument w sprawie", "event_summary": "Podsumowanie sprawy",
            "event_data_quality": "Zmiana danych kontrahenta – sprawdź sprawy"}


def notifier_node(state: AssistantState) -> AssistantState:
    started = time.perf_counter()
    ids: list[int] = []
    origin = f"zdarzenie #{state['event'].get('id', '?')} ({state['event']['source']})"
    for alert in state.get("alerts", []):          # skan: powiadomienia wg alertów
        ids += notifications.notify_alert(alert)
    body = re.sub(r"\s*\[F\d+\]", "", state.get("answer", ""))   # odnośniki są dla UI, nie dla e-maila
    subject = SUBJECTS.get(state["intent"], "Powiadomienie asystenta BAW")
    if state.get("instance_id"):
        subject += f" {state['instance_id']}"
    for r in state.get("recipients", []):
        ids.append(notifications.send(r["channel"], notifications.recipient_for(r["audience"], r["channel"]),
                                      subject, body, origin))
    return {"notifications": ids,
            "trace": [{"agent": "Redaktor-Weryfikator",
                       "summary": f"Wysłał powiadomienia: {len(ids)} (symulacja: zadanie BAW, e-mail, Teams).",
                       "ms": elapsed_ms(started)}]}
