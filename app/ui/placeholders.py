"""Wspólny komunikat dla funkcji planowanych w PoC v2."""
from __future__ import annotations

import streamlit as st


def v2_notice(agent: str, what: str) -> None:
    st.info(f"**{agent} – PoC v2.** {what} Poniżej surowe dane, z których agent będzie korzystał.",
            icon=":material/construction:")
