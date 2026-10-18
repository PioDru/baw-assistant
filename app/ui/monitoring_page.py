"""Strona „Centrum monitoringu” – w PoC v1 podgląd logów (Agent Monitor i kolejka zdarzeń w PoC v2)."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from baw_assistant.db import query
from ui.placeholders import v2_notice


def render() -> None:
    st.title("Centrum monitoringu")
    v2_notice("Agent Monitor", "Kolejka zdarzeń (UCA, EDA, harmonogram), alerty z diagnozą i powiadomienia.")
    levels = st.multiselect("Poziom", ["ERROR", "WARN", "INFO"], default=["ERROR", "WARN"])
    if not levels:
        return
    marks = ",".join("?" for _ in levels)
    rows = query(f"SELECT ts, level, component, instance_id, message FROM logs WHERE level IN ({marks}) "
                 "ORDER BY ts DESC LIMIT 200", tuple(levels))
    st.dataframe(pd.DataFrame(rows).rename(columns={"ts": "Czas", "level": "Poziom", "component": "Komponent",
                                                    "instance_id": "Instancja", "message": "Komunikat"}),
                 hide_index=True, width="stretch")
