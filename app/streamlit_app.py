"""Asystent operacyjny IBM BAW – aplikacja Streamlit (PoC v2).

Uruchomienie lokalne:  streamlit run streamlit_app.py
"""
from __future__ import annotations

import streamlit as st

from baw_assistant import db, kb
from baw_assistant.config import get_settings
from baw_assistant.data_generator import generate
from baw_assistant.llm import llm_error, llm_label
from baw_assistant.rbac import ROLE_LABELS, users_with_role
from ui import agents_page, analytics_page, assistant_page, monitoring_page, team_page

st.set_page_config(page_title="Asystent BAW", page_icon=":material/smart_toy:", layout="wide")


@st.cache_resource(show_spinner="Przygotowuję dane syntetyczne…")
def ensure_data() -> bool:
    if not db.database_ready():
        generate()
    return True


ensure_data()
# Baza wiedzy: przy każdym odświeżeniu (tanio po sukcesie); gdy Qdrant był niedostępny, ponawia indeksowanie.
kb.ensure_ready()
kb_state = kb.status()
settings = get_settings()

with st.sidebar:
    st.markdown("### Asystent BAW")
    st.caption("PoC v2 · dane syntetyczne")
    role = st.selectbox("Rola (symulacja logowania)", list(ROLE_LABELS), format_func=ROLE_LABELS.get,
                        key="role")
    users = users_with_role(role)
    demo_user = {"pracownik": "u_anna", "kierownik": "u_marek"}.get(role)  # scenariusze demo
    default_idx = next((i for i, u in enumerate(users) if u.id == demo_user), 0)
    user = st.selectbox("Użytkownik", users, index=default_idx, format_func=lambda u: u.name,
                        key=f"user_{role}")
    st.session_state["current_user"] = user

pages = [st.Page(assistant_page.render, title="Asystent", icon=":material/chat:", url_path="asystent",
                 default=True)]
if role in ("kierownik", "admin"):
    pages.append(st.Page(team_page.render, title="Pulpit zespołu", icon=":material/groups:",
                         url_path="pulpit"))
if role in ("wlasciciel", "kierownik", "admin"):
    pages.append(st.Page(analytics_page.render, title="Analityka procesu", icon=":material/insights:",
                         url_path="analityka"))
if role == "admin":
    pages.append(st.Page(monitoring_page.render, title="Centrum monitoringu", icon=":material/monitoring:",
                         url_path="monitoring"))
    pages.append(st.Page(agents_page.render, title="Zarządzanie agentami", icon=":material/hub:",
                         url_path="agenci"))

nav = st.navigation(pages, position="sidebar")

with st.sidebar:
    st.divider()
    st.caption(f"Model: {llm_label()}")
    if llm_error():
        st.warning("Model LLM niedostępny – agenci działają na regułach i szablonach. "
                   "Sprawdź OPENAI_API_KEY i LLM_MODEL w pliku .env.", icon=":material/warning:")
    st.caption(f"Symulowany czas: {settings.sim_now:%d.%m.%Y %H:%M}")
    st.caption("Langfuse: " + ("włączony" if settings.langfuse_enabled else "wyłączony"))
    if kb_state["points"]:
        st.caption(f"Baza wiedzy (Qdrant, {kb_state['mode']}): {kb_state['points']} sekcji")
    else:
        st.warning(f"Baza wiedzy pusta lub niedostępna ({kb_state['url']}). Agenci działają bez niej; "
                   "ponowię indeksowanie automatycznie.", icon=":material/database_off:")
    st.caption("Widoczne strony i zakres danych agentów zależą od wybranej roli.")

nav.run()
