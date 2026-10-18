"""Strona „Asystent” – czat w kontekście instancji, odpowiedź ze źródłami i śladem agentów."""
from __future__ import annotations

import uuid

import streamlit as st

from baw_assistant import tools
from baw_assistant.graph import ask
from baw_assistant.rbac import User, visible_active_instances

EXAMPLES = {
    "pracownik": ["Dlaczego ta sprawa stoi? Co mam zrobić dalej?", "Jakie dokumenty są już w sprawie?",
                  "Co mam pilnego?"],
    "kierownik": ["Co jest pilne w moim zespole?", "Dlaczego ta sprawa stoi?",
                  "Kto ma najwięcej przeterminowanych zadań?"],
    "wlasciciel": ["Dlaczego ta sprawa stoi?", "Jakie są wąskie gardła procesu?", "Co jest po terminie SLA?"],
    "admin": ["Dlaczego ta sprawa stoi?", "Kto zajmował się tą sprawą?", "Jaki jest stan platformy?"],
}

VERDICT_LABELS = {"ok": ("Zweryfikowana", "green"), "blocked": ("Zablokowana – same fakty", "orange"),
                  "refused": ("Odmowa", "red"), "info": ("Komunikat", "blue")}


def _chat_key(user: User) -> str:
    return f"chat_{user.id}"


def _context_selector(user: User) -> str | None:
    instances = visible_active_instances(user)
    options = [None] + [i["id"] for i in instances]
    labels = {i["id"]: f"{i['id']} · {i['process']} · {i['current_step']}" for i in instances}
    default = 1 if user.role == "pracownik" and instances else 0
    if user.id == "u_anna" and "REK-2026-0412" in labels:  # scenariusz demo
        default = options.index("REK-2026-0412")
    return st.selectbox("Kontekst: instancja procesu", options, index=default,
                        format_func=lambda x: "— bez kontekstu —" if x is None else labels[x],
                        key=f"ctx_{user.id}")


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


def _render_answer(result: dict, idx: int) -> None:
    label, color = VERDICT_LABELS.get(result.get("verdict", ""), ("", "gray"))
    if label:
        st.badge(label, color=color)
    st.markdown(result.get("answer", ""))
    actions = result.get("suggested_actions") or []
    if actions and result.get("verdict") in ("ok", "blocked"):
        cols = st.columns(len(actions))
        for col, action in zip(cols, actions):
            col.button(action, key=f"act_{idx}_{action}", disabled=True,
                       help="Akcje sugerowane wymagają zatwierdzenia (człowiek w pętli) – etap 6 PoC.")
    sources = result.get("sources") or []
    if sources:
        with st.expander(f"Źródła ({len(sources)})"):
            for f in sources:
                st.markdown(f"**[{f['id']}]** {f['text']}  \n<small>źródło: {f['source']}</small>",
                            unsafe_allow_html=True)


def _render_trace(result: dict | None) -> None:
    st.markdown("#### Jak powstała odpowiedź")
    if not result:
        st.caption("Ślad pracy agentów pojawi się po pierwszym pytaniu.")
        return
    st.caption(f"Czas: {result.get('latency_ms', 0) / 1000:.1f} s")
    steps = result.get("trace", [])
    for n, step in enumerate(steps, 1):
        st.markdown(f"**{n}. {step['agent']}**  \n{step['summary']}")
    used = {s["agent"] for s in steps}
    if result.get("decision") == "route" and "Agent Analityk" not in used:
        st.caption("Agent Analityk i Agent Monitor: PoC v2.")


def render() -> None:
    user: User = st.session_state["current_user"]
    st.title("Asystent")
    st.caption("Zadaj pytanie o sprawę, nad którą pracujesz. Odpowiedź uwzględnia kontekst wybranej instancji "
               "i Twoje uprawnienia.")

    context_id = _context_selector(user)
    if context_id:
        _context_summary(context_id)

    key = _chat_key(user)
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
                    _render_answer(msg["result"], idx)
        st.caption("Przykładowe pytania:")
        cols = st.columns(len(EXAMPLES[user.role]))
        clicked = None
        for col, example in zip(cols, EXAMPLES[user.role]):
            if col.button(example, key=f"ex_{user.id}_{example}", width="stretch"):
                clicked = example

    with trace_col:
        last = next((m["result"] for m in reversed(history) if m["role"] == "assistant"), None)
        with st.container(border=True):
            _render_trace(last)
        if history and st.button("Wyczyść rozmowę", key=f"clear_{user.id}"):
            st.session_state[key] = []
            st.rerun()

    prompt = st.chat_input("Napisz pytanie o tę sprawę…", key=f"input_{user.id}") or clicked
    if prompt:
        history.append({"role": "user", "content": prompt})
        with st.spinner("Agenci pracują…"):
            result = ask(prompt, user.id, user.role, context_id, session_id=session_id)
        history.append({"role": "assistant", "result": result})
        st.rerun()
