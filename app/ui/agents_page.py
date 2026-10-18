"""Strona „Zarządzanie agentami” – rejestr agentów (włączanie), statystyki, akcje i ślad przebiegów."""
from __future__ import annotations

import json
from collections import Counter

import pandas as pd
import streamlit as st

from baw_assistant import db
from baw_assistant.config import get_settings
from baw_assistant.db import query
from baw_assistant.llm import llm_label

REGISTRY = [
    ("Dyspozytor", "intencja, uprawnienia, routing", "Profil użytkownika, macierz uprawnień", False),
    ("Agent Instancji", "fakty o sprawach", "Instancje, zadania, dokumenty, historia (odczyt), opisy procesów", True),
    ("Agent Analityk", "KPI, SLA, wąskie gardła", "Historia instancji (agregaty)", True),
    ("Agent Monitor", "logi, alerty, jakość danych", "Logi, kolejki zdarzeń, instrukcje incydentów", True),
    ("Redaktor-Weryfikator", "odpowiedź, weryfikacja, powiadomienia", "Wyniki agentów, kanały powiadomień", False),
]


def render() -> None:
    settings = get_settings()
    st.title("Zarządzanie agentami")
    st.caption("Centralny rejestr agentów: włączanie, model, dostęp do danych, statystyki i ślad przebiegów. "
               "Dyspozytor i Redaktor-Weryfikator są zawsze aktywni – pilnują uprawnień i jakości.")

    runs = query("SELECT * FROM agent_runs ORDER BY id DESC LIMIT 500")
    calls = Counter(a for r in runs for a in json.loads(r["path"]))
    st.subheader("Rejestr agentów")
    header = st.columns([2, 3, 4, 1, 1])
    for col, title in zip(header, ["Agent", "Rola", "Dostęp do danych", "Wywołania", "Włączony"]):
        col.markdown(f"**{title}**")
    for name, role, access, toggleable in REGISTRY:
        c = st.columns([2, 3, 4, 1, 1], vertical_alignment="center")
        c[0].write(name)
        c[1].write(role)
        c[2].write(access)
        c[3].write(calls.get(name, 0))
        if toggleable:
            enabled = c[4].toggle("włączony", value=db.agent_enabled(name), key=f"toggle_{name}",
                                  label_visibility="collapsed")
            if enabled != db.agent_enabled(name):
                db.set_agent_enabled(name, enabled)
                st.rerun()
        else:
            c[4].write("zawsze")
    st.caption(f"Model wszystkich agentów: {llm_label()}. Zmiana modelu: zmienna LLM_MODEL (np. większy model dla "
               "Analityka i Weryfikatora, jeśli testy wykażą zbyt niską jakość).")
    if settings.langfuse_enabled:
        st.caption("Pełne ślady, czasy i koszty wywołań modelu: Langfuse (http://localhost:3000).")

    st.subheader("Statystyki")
    chat = [r for r in runs if r["role"] != "system"]
    m = st.columns(4)
    m[0].metric("Przebiegi (pytania)", len(chat))
    m[1].metric("Zdarzenia systemowe", len(runs) - len(chat))
    m[2].metric("Śr. czas odpowiedzi", f"{sum(r['latency_ms'] for r in chat) / len(chat) / 1000:.1f} s" if chat else "—")
    m[3].metric("Odmowy i blokady", sum(r["verdict"] in ("refused", "blocked") for r in runs))

    st.subheader("Akcje zatwierdzane przez człowieka")
    acts = query("SELECT ts, user_id, action, instance_id, status, channel, recipient FROM action_log "
                 "WHERE status != 'Oczekuje na zatwierdzenie' ORDER BY id DESC LIMIT 30")
    if acts:
        st.dataframe(pd.DataFrame(acts).rename(columns={"ts": "Czas", "user_id": "Użytkownik", "action": "Akcja",
                                                        "instance_id": "Sprawa", "status": "Status", "channel": "Kanał",
                                                        "recipient": "Adresat"}), hide_index=True, width="stretch")
    else:
        st.caption("Brak zatwierdzonych ani odrzuconych akcji.")

    st.subheader("Ślad ostatnich przebiegów")
    if not runs:
        st.caption("Brak przebiegów – zadaj pytanie na stronie Asystent.")
        return
    df = pd.DataFrame(runs[:50])
    df["path"] = df["path"].map(lambda p: " → ".join(json.loads(p)))
    st.dataframe(df.rename(columns={"ts": "Czas", "role": "Rola", "question": "Pytanie / zdarzenie",
                                    "intent": "Intencja", "path": "Ścieżka agentów", "verdict": "Wynik",
                                    "latency_ms": "Czas [ms]"})
                 [["Czas", "Rola", "Pytanie / zdarzenie", "Intencja", "Ścieżka agentów", "Wynik", "Czas [ms]"]],
                 hide_index=True, width="stretch")
