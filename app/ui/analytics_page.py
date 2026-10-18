"""Strona „Analityka procesu” – KPI, przepustowość, wąskie gardła i raport AI (Agent Analityk)."""
from __future__ import annotations

import streamlit as st

from baw_assistant import analytics
from baw_assistant.db import query
from baw_assistant.graph import ask
from baw_assistant.rbac import User, visible_process_ids
from ui.components import render_answer, steps_chart, weekly_chart

PERIODS = {"Ostatnie 12 tygodni": 84, "Ostatnie 30 dni": 30, "Ostatnie 6 miesięcy": 180}


def render() -> None:
    user: User = st.session_state["current_user"]
    st.title("Analityka procesu")
    st.caption("Historia realizacji, wąskie gardła i trendy. Podsumowanie z rekomendacjami przygotowuje "
               "Agent Analityk, a Redaktor-Weryfikator sprawdza je z danymi.")
    allowed = visible_process_ids(user)
    procs = [p for p in query("SELECT id, name FROM processes ORDER BY name") if p["id"] in allowed]
    if not procs:
        st.info("Brak procesów w zakresie Twoich uprawnień.")
        return
    c1, c2, c3 = st.columns([2, 2, 1], vertical_alignment="bottom")
    proc = c1.selectbox("Proces", procs, format_func=lambda p: p["name"], key=f"proc_{user.id}")
    period_label = c2.selectbox("Okres", list(PERIODS), key=f"period_{user.id}")
    days = PERIODS[period_label]
    generate = c3.button("Generuj raport AI", type="primary", width="stretch")

    k = analytics.process_kpis(proc["id"], days)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Zakończone instancje", k["completed"])
    m2.metric("Mediana czasu realizacji", f"{k['median_cycle_days']:.1f} dnia".replace(".", ",")
              if k["median_cycle_days"] is not None else "—")
    delta = (k["sla_pct_recent"] - k["sla_pct_previous"]) if None not in (k["sla_pct_recent"], k["sla_pct_previous"]) else None
    m3.metric(f"Zrealizowane w SLA ({k['sla_days']} dni)", f"{k['sla_pct']}%" if k["sla_pct"] is not None else "—",
              delta=f"{delta:+d} p.p. (4 tyg.)" if delta is not None else None)
    m4.metric("Aktywne / po terminie", f"{k['active']} / {k['active_overdue']}")

    g1, g2 = st.columns(2, gap="large")
    with g1:
        st.subheader("Zakończone instancje tygodniowo")
        weekly_chart(k["weekly"].to_dict("records"))
    with g2:
        st.subheader("Średni czas w kroku (wąskie gardła)")
        steps_chart(k["steps"].to_dict("records"))

    key = f"report_{user.id}"
    if generate:
        with st.spinner("Agent Analityk przygotowuje raport…"):
            st.session_state[key] = ask(f"Raport analityczny procesu {proc['name']} – {period_label.lower()}",
                                        user.id, user.role, params={"process_id": proc["id"], "days": days})
    if st.session_state.get(key):
        with st.container(border=True):
            st.subheader("Raport AI")
            result = st.session_state[key]
            render_answer({**result, "data": {}}, f"report_{user.id}", user, None)
            st.download_button("Pobierz raport (Markdown)", result.get("answer", "").encode("utf-8"),
                               f"raport_{proc['id']}.md", "text/markdown")
