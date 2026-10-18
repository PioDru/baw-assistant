"""Strona „Zarządzanie agentami” – rejestr agentów i ślad ostatnich przebiegów."""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from baw_assistant.config import get_settings
from baw_assistant.db import query
from baw_assistant.llm import llm_label

REGISTRY = [
    ("Dyspozytor", "intencja, uprawnienia, routing", "Profil użytkownika, macierz uprawnień", "aktywny (v1)"),
    ("Agent Instancji", "fakty o sprawach", "Instancje, zadania, dokumenty, historia, logi sprawy (odczyt)",
     "aktywny (v1)"),
    ("Agent Analityk", "KPI, SLA, wąskie gardła", "Historia instancji (agregaty)", "planowany (v2)"),
    ("Agent Monitor", "logi, alerty, jakość danych", "Logi, metryki, kolejki zdarzeń", "planowany (v2)"),
    ("Redaktor-Weryfikator", "grounding, uprawnienia, odpowiedź", "Wyniki agentów", "aktywny (v1)"),
]


def render() -> None:
    settings = get_settings()
    st.title("Zarządzanie agentami")
    st.dataframe(pd.DataFrame(REGISTRY, columns=["Agent", "Rola", "Dostęp do danych", "Status"]).assign(
        Model=llm_label()), hide_index=True, width="stretch")
    if settings.langfuse_enabled:
        st.caption(f"Pełne ślady i koszty wywołań: Langfuse ({settings.langfuse_base_url.replace('langfuse-web', 'localhost')}).")

    runs = query("SELECT ts, role, question, intent, path, verdict, latency_ms FROM agent_runs "
                 "ORDER BY id DESC LIMIT 50")
    st.subheader("Ślad ostatnich przebiegów")
    if not runs:
        st.caption("Brak przebiegów – zadaj pytanie na stronie Asystent.")
        return
    c1, c2, c3 = st.columns(3)
    c1.metric("Przebiegi (ostatnie 50)", len(runs))
    c2.metric("Śr. czas odpowiedzi", f"{sum(r['latency_ms'] for r in runs) / len(runs) / 1000:.1f} s")
    c3.metric("Odmowy i blokady", sum(r["verdict"] in ("refused", "blocked") for r in runs))
    df = pd.DataFrame(runs)
    df["path"] = df["path"].map(lambda p: " → ".join(json.loads(p)))
    st.dataframe(df.rename(columns={"ts": "Czas", "role": "Rola", "question": "Pytanie", "intent": "Intencja",
                                    "path": "Ścieżka agentów", "verdict": "Wynik", "latency_ms": "Czas [ms]"}),
                 hide_index=True, width="stretch")
