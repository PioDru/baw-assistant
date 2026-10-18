"""Wspólne elementy UI: odpowiedź agentów, wykresy, ślad, zatwierdzanie akcji (human-in-the-loop)."""
from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from baw_assistant import actions
from baw_assistant.rbac import User

VERDICT_LABELS = {"ok": ("Zweryfikowana", "green"), "blocked": ("Zablokowana – same fakty", "orange"),
                  "refused": ("Odmowa", "red"), "info": ("Komunikat", "blue")}
BUCKET_COLORS = alt.Scale(domain=["w terminie", "zagrożone", "po terminie"], range=["#A9C1EE", "#E8A23B", "#9E1C14"])


def team_chart(rows: list[dict], height: int = 220) -> None:
    if not rows:
        return
    df = pd.DataFrame(rows).rename(columns={"ok": "w terminie", "risk": "zagrożone", "overdue": "po terminie"})
    long = df.melt(id_vars=["name"], value_vars=["w terminie", "zagrożone", "po terminie"], var_name="stan",
                   value_name="sprawy")
    chart = (alt.Chart(long).mark_bar().encode(
        x=alt.X("sum(sprawy):Q", title="Aktywne sprawy"),
        y=alt.Y("name:N", title=None, sort=df.sort_values("total", ascending=False)["name"].tolist()),
        color=alt.Color("stan:N", scale=BUCKET_COLORS, legend=alt.Legend(orient="top", title=None)),
        order=alt.Order("stan_order:Q"), tooltip=["name", "stan", "sprawy"])
        .transform_calculate(stan_order="indexof(['w terminie','zagrożone','po terminie'], datum.stan)")
        .properties(height=height))
    st.altair_chart(chart, width="stretch")


def weekly_chart(rows: list[dict], height: int = 220) -> None:
    if not rows:
        return
    df = pd.DataFrame(rows)
    st.altair_chart(alt.Chart(df).mark_bar(color="#7FA0DE").encode(
        x=alt.X("week_end:T", title="Tydzień (koniec)"), y=alt.Y("completed:Q", title="Zakończone instancje"),
        tooltip=["week_end:T", "completed:Q"]).properties(height=height), width="stretch")


def steps_chart(rows: list[dict], height: int = 220) -> None:
    if not rows:
        return
    df = pd.DataFrame(rows)
    top = df["days"].max()
    st.altair_chart(alt.Chart(df).mark_bar().encode(
        x=alt.X("days:Q", title="Średni czas w kroku [dni]"), y=alt.Y("step:N", title=None, sort=None),
        color=alt.condition(alt.datum.days == top, alt.value("#C2410C"), alt.value("#A9C1EE")),
        tooltip=["step", "days"]).properties(height=height), width="stretch")


def render_answer(result: dict, key: str, user: User, context_instance_id: str | None) -> None:
    label, color = VERDICT_LABELS.get(result.get("verdict", ""), ("", "gray"))
    if label:
        st.badge(label, color=color)
    st.markdown(result.get("answer", ""))
    data = result.get("data") or {}
    if data.get("team_breakdown"):
        team_chart(data["team_breakdown"])
    if data.get("steps"):
        steps_chart(data["steps"], height=180)
    acts = result.get("suggested_actions") or []
    if acts and result.get("verdict") in ("ok", "blocked"):
        st.caption("Akcje sugerowane – wymagają Twojego zatwierdzenia:")
        cols = st.columns(len(acts))
        for col, action in zip(cols, acts):
            if col.button(action, key=f"act_{key}_{action}", width="stretch"):
                instance_id = result.get("instance_id") or context_instance_id
                try:
                    st.session_state[f"pending_action_{user.id}"] = actions.start_action(action, user.id, instance_id)
                except PermissionError as exc:
                    st.error(str(exc))
                st.rerun()
    sources = result.get("sources") or []
    if sources:
        with st.expander(f"Źródła ({len(sources)})"):
            for f in sources:
                st.markdown(f"**[{f['id']}]** {f['text']}  \n<small>źródło: {f['source']} · {f.get('agent', '')}</small>",
                            unsafe_allow_html=True)


def render_trace(result: dict | None) -> None:
    st.markdown("#### Jak powstała odpowiedź")
    if not result:
        st.caption("Ślad pracy agentów pojawi się po pierwszym pytaniu.")
        return
    st.caption(f"Czas: {result.get('latency_ms', 0) / 1000:.1f} s")
    for n, step in enumerate(result.get("trace", []), 1):
        st.markdown(f"**{n}. {step['agent']}**  \n{step['summary']}")
    if result.get("decision") == "route":
        skipped = {"Agent Instancji", "Agent Analityk", "Agent Monitor"} - set(result.get("agents", []))
        if skipped:
            st.caption("Pominięci: " + ", ".join(sorted(skipped)) + " (niepotrzebni do tego pytania).")


def approval_panel(user: User) -> None:
    """Zatwierdzanie akcji: graf czeka w interrupt(), decyzja wznawia go przez Command(resume=…)."""
    key = f"pending_action_{user.id}"
    pending = st.session_state.get(key)
    done_key = f"done_action_{user.id}"
    if st.session_state.get(done_key):
        st.success(st.session_state.pop(done_key), icon=":material/check_circle:")
    if not pending:
        return
    with st.container(border=True):
        st.markdown(f"#### Do zatwierdzenia: {pending['action']}")
        st.caption(f"Kanał: {pending['channel']} · Adresat: {pending['recipient']} · Temat: {pending['subject']}")
        text = st.text_area("Treść (możesz ją poprawić przed zatwierdzeniem)", pending["draft"], height=180,
                            key=f"draft_{pending['thread_id']}")
        c1, c2, _ = st.columns([1, 1, 3])
        if c1.button("Zatwierdź", type="primary", key=f"approve_{pending['thread_id']}"):
            result = actions.resume_action(pending["thread_id"], True, text)
            st.session_state[done_key] = (f"{result['status']}: {pending['channel']} → {pending['recipient']}. "
                                          "W PoC agenci nie wykonują akcji w BAW.")
            st.session_state.pop(key)
            st.rerun()
        if c2.button("Odrzuć", key=f"reject_{pending['thread_id']}"):
            actions.resume_action(pending["thread_id"], False)
            st.session_state[done_key] = "Akcja odrzucona – nic nie zostało wysłane."
            st.session_state.pop(key)
            st.rerun()
