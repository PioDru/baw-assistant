"""Strona „Pulpit zespołu” – miary zespołu (Agent Analityk), sprawy wymagające uwagi i pytania o zespół."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from baw_assistant import analytics, tools
from baw_assistant.graph import ask
from baw_assistant.rbac import User, visible_active_instances
from ui.components import approval_panel, render_answer, team_chart


def render() -> None:
    user: User = st.session_state["current_user"]
    st.title("Pulpit zespołu")
    t = analytics.team_overview(user)
    st.caption(f"Stan aktywnych spraw: {t['team_name']}. Miary liczone w kodzie (pandas), bez udziału modelu.")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Aktywne instancje", t["active"])
    c2.metric("Po terminie SLA", t.get("overdue", 0))
    c3.metric("Termin w ciągu 2 dni", t.get("at_risk", 0))
    c4.metric("Najmniej obciążona osoba", t["least_loaded"]["name"] if t.get("least_loaded") else "—")

    approval_panel(user)
    left, right = st.columns([3, 2], gap="large")
    with left:
        st.subheader("Obciążenie pracowników")
        team_chart(t.get("per_assignee", []), height=260)
    with right, st.container(border=True):
        st.subheader("Zapytaj o zespół")
        key = f"team_chat_{user.id}"
        with st.form(f"team_q_{user.id}", clear_on_submit=True):
            q = st.text_input("Pytanie", placeholder="Np. kto ma najwięcej przeterminowanych zadań?")
            sent = st.form_submit_button("Zapytaj", type="primary")
        if sent and q:
            with st.spinner("Agenci pracują…"):
                st.session_state[key] = ask(q, user.id, user.role)
        if st.session_state.get(key):
            render_answer(st.session_state[key], f"team_{user.id}", user, None)

    st.subheader("Instancje wymagające uwagi")
    rows = tools.active_instances_for([i["id"] for i in visible_active_instances(user)])
    df = pd.DataFrame(rows)
    if df.empty:
        st.write("Brak aktywnych instancji w zakresie uprawnień.")
        return
    df = df[df["days_to_sla"] <= 3].copy()
    df["Status"] = df["days_to_sla"].map(lambda d: "Po terminie" if d < 0 else "Zagrożona")
    st.dataframe(df.rename(columns={"id": "Instancja", "process_name": "Proces", "current_step": "Krok",
                                    "assignee_name": "Przypisana", "sla_due": "Termin SLA",
                                    "days_to_sla": "Dni do SLA"})
                 [["Instancja", "Proces", "Krok", "Przypisana", "Termin SLA", "Dni do SLA", "Status"]],
                 hide_index=True, width="stretch")
    st.download_button("Pobierz CSV", df.to_csv(index=False).encode("utf-8"), "instancje_zespolu.csv", "text/csv")
