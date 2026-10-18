"""Strona „Analityka procesu” – w PoC v1 proste statystyki z danych (Agent Analityk w PoC v2)."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from baw_assistant.db import query
from ui.placeholders import v2_notice


def render() -> None:
    st.title("Analityka procesu")
    v2_notice("Agent Analityk", "Czasy w krokach, wąskie gardła, trendy oraz raport AI z rekomendacjami.")
    procs = query("SELECT id, name FROM processes ORDER BY name")
    proc = st.selectbox("Proces", procs, format_func=lambda p: p["name"])
    rows = query("SELECT created_at, completed_at, sla_due FROM instances WHERE process_id = ? "
                 "AND status = 'Zakończona'", (proc["id"],))
    if not rows:
        st.write("Brak zakończonych instancji.")
        return
    df = pd.DataFrame(rows)
    for col in ("created_at", "completed_at", "sla_due"):
        df[col] = pd.to_datetime(df[col])
    df["dni"] = (df["completed_at"] - df["created_at"]).dt.total_seconds() / 86400
    c1, c2, c3 = st.columns(3)
    c1.metric("Zakończone instancje", len(df))
    c2.metric("Mediana czasu realizacji", f"{df['dni'].median():.1f} dnia".replace(".", ","))
    c3.metric("Zrealizowane w SLA", f"{(df['completed_at'] <= df['sla_due']).mean():.0%}")
    weekly = df.set_index("completed_at").resample("W").size().rename("Zakończone tygodniowo")
    st.bar_chart(weekly)
