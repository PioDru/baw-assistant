"""Strona „Asystent” – czat w kontekście instancji, odpowiedź ze źródłami, śladem agentów i akcjami."""
from __future__ import annotations

import uuid

import streamlit as st

from baw_assistant import tools
from baw_assistant.graph import ask
from baw_assistant.rbac import User, visible_active_instances
from ui.components import approval_panel, render_answer, render_trace

EXAMPLES = {
    "pracownik": ["Dlaczego ta sprawa stoi? Co mam zrobić dalej?", "Jakie dokumenty są już w sprawie?",
                  "Co mam pilnego?"],
    "kierownik": ["Kto ma najwięcej przeterminowanych zadań w zespole?", "Dlaczego ta sprawa stoi?",
                  "Jakie są wąskie gardła procesu reklamacji?"],
    "wlasciciel": ["Jakie są wąskie gardła procesu reklamacji?", "Raport procesu zatwierdzania faktur",
                   "Dlaczego ta sprawa stoi?"],
    "admin": ["Jaki jest stan platformy, są jakieś awarie?", "Dlaczego ta sprawa stoi?",
              "Kto ma najwięcej przeterminowanych zadań w zespole?"],
}


def _context_selector(user: User) -> str | None:
    instances = visible_active_instances(user)
    options = [None] + [i["id"] for i in instances]
    labels = {i["id"]: f"{i['id']} · {i['process']} · {i['current_step']}" for i in instances}
    default = 1 if user.role == "pracownik" and instances else 0
    demo = {"u_anna": "REK-2026-0412", "u_tomasz": "REK-2026-0419", "u_marek": "REK-2026-0377"}.get(user.id)
    if demo in labels:  # scenariusze demo
        default = options.index(demo)
    return st.selectbox("Kontekst: instancja procesu", options, index=default,
                        format_func=lambda x: "— bez kontekstu —" if x is None else labels[x], key=f"ctx_{user.id}")


def _context_summary(instance_id: str) -> None:
    inst = tools.get_instance(instance_id)
    left = tools.days_between(tools.now(), inst["sla_due"])
    idle = tools.days_between(inst["last_change_at"])
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Status", inst["status"])
    c2.metric("Bieżący krok", inst["current_step"] or "—")
    c3.metric("Termin SLA", tools.fmt_date(inst["sla_due"]),
              delta=f"{left} dni" if left >= 0 else f"po terminie {-left} dni",
              delta_color="normal" if left > 3 else "inverse")
    c4.metric("Bez zmian od", f"{idle} dni")


def render() -> None:
    user: User = st.session_state["current_user"]
    st.title("Asystent")
    st.caption("Zadaj pytanie o sprawę, zespół, proces albo platformę. Odpowiedź uwzględnia kontekst wybranej "
               "instancji i Twoje uprawnienia.")

    context_id = _context_selector(user)
    if context_id:
        _context_summary(context_id)
    approval_panel(user)

    key = f"chat_{user.id}"
    history: list[dict] = st.session_state.setdefault(key, [])
    session_id = st.session_state.setdefault(f"session_{user.id}", str(uuid.uuid4()))

    chat_col, trace_col = st.columns([2, 1], gap="large")
    with chat_col:
        for idx, msg in enumerate(history):
            with st.chat_message(msg["role"], avatar=":material/person:" if msg["role"] == "user"
                                 else ":material/smart_toy:"):
                if msg["role"] == "user":
                    st.markdown(msg["content"])
                else:
                    render_answer(msg["result"], f"{user.id}_{idx}", user, context_id)
        st.caption("Przykładowe pytania:")
        cols = st.columns(len(EXAMPLES[user.role]))
        clicked = None
        for col, example in zip(cols, EXAMPLES[user.role]):
            if col.button(example, key=f"ex_{user.id}_{example}", width="stretch"):
                clicked = example

    with trace_col:
        last = next((m["result"] for m in reversed(history) if m["role"] == "assistant"), None)
        with st.container(border=True):
            render_trace(last)
        if history and st.button("Wyczyść rozmowę", key=f"clear_{user.id}"):
            st.session_state[key] = []
            st.rerun()

    prompt = st.chat_input("Napisz pytanie…", key=f"input_{user.id}") or clicked
    if prompt:
        history.append({"role": "user", "content": prompt})
        with chat_col:
            with st.chat_message("user", avatar=":material/person:"):
                st.markdown(prompt)
            with st.chat_message("assistant", avatar=":material/smart_toy:"):
                response_placeholder = st.empty()
                streamed_tokens: list[str] = []

                def _on_token(token: str) -> None:
                    streamed_tokens.append(token)
                    response_placeholder.markdown("".join(streamed_tokens) + "▌")

                with st.spinner("Agenci pracują…"):
                    result = ask(prompt, user.id, user.role, context_id, session_id=session_id, on_token=_on_token)

                response_placeholder.empty()
        history.append({"role": "assistant", "result": result})
        st.rerun()
