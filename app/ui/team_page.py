"""Strona „Pulpit zespołu” – w PoC v1 dane bez analizy AI (Agent Analityk w PoC v2)."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from baw_assistant import tools
from baw_assistant.rbac import User, visible_active_instances
from ui.placeholders import v2_notice


def render() -> None:
    user: User = st.session_state["current_user"]
    st.title("Pulpit zespołu")
    v2_notice("Agent Analityk", "Miary obciążenia, zagrożone SLA i pytania o zespół w języku naturalnym.")
    rows = tools.active_instances_for([i["id"] for i in visible_active_instances(user)])
    if not rows:
        st.write("Brak aktywnych instancji w zakresie uprawnień.")
        return
    df = pd.DataFrame(rows)
    c1, c2, c3 = st.columns(3)
    c1.metric("Aktywne instancje", len(df))
    c2.metric("Po terminie SLA", int((df["days_to_sla"] < 0).sum()))
    c3.metric("Termin w ciągu 2 dni", int(df["days_to_sla"].between(0, 2).sum()))
    st.dataframe(
        df.rename(columns={"id": "Instancja", "process_name": "Proces", "current_step": "Krok",
                           "assignee_name": "Przypisana", "sla_due": "Termin SLA", "days_to_sla": "Dni do SLA"})
        [["Instancja", "Proces", "Krok", "Przypisana", "Termin SLA", "Dni do SLA"]],
        hide_index=True, width="stretch",
    )
