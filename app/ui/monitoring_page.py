"""Strona „Centrum monitoringu” – kolejka zdarzeń (UCA, EDA, harmonogram), alerty, progi i powiadomienia."""
from __future__ import annotations

import io
import json

import pandas as pd
import streamlit as st

from baw_assistant import db, events, notifications
from baw_assistant.db import query

PRIORITY_COLORS = {"Krytyczny": "red", "Ostrzeżenie": "orange", "Informacja": "blue"}


def _process_queue() -> None:
    with st.spinner("Agenci przetwarzają kolejkę…"):
        done = events.process_pending()
    st.session_state["monitor_msg"] = f"Przetworzone zdarzenia: {len(done)}."


def _upload_logs(file) -> int:
    raw = file.getvalue().decode("utf-8")
    df = (pd.read_json(io.StringIO(raw), lines=True) if file.name.endswith((".jsonl", ".json"))
          else pd.read_csv(io.StringIO(raw)))
    missing = {"ts", "level", "component", "message"} - set(df.columns)
    if missing:
        raise ValueError("Brak kolumn: " + ", ".join(sorted(missing)))
    df["instance_id"] = df["instance_id"] if "instance_id" in df else None
    rows = df[["ts", "level", "component", "instance_id", "message"]].astype(object).where(df.notna(), None)
    with db.connect() as conn:
        conn.executemany("INSERT INTO logs (ts, level, component, instance_id, message) VALUES (?,?,?,?,?)",
                         rows.itertuples(index=False, name=None))
    return len(rows)


def render() -> None:
    st.title("Centrum monitoringu")
    top_l, top_r = st.columns([4, 1], vertical_alignment="bottom")
    top_l.caption("Agent Monitor wykrywa alerty w logach, kolejkach i danych; zdarzenia UCA i EDA przechodzą przez "
                  "Dyspozytora do właściwych agentów. Worker harmonogramu przetwarza kolejkę co 15 s.")
    if top_r.button("Uruchom skan teraz", type="primary", width="stretch"):
        events.inject_test_event("scan")
        _process_queue()
    if msg := st.session_state.pop("monitor_msg", None):
        st.success(msg, icon=":material/check_circle:")

    alerts = pd.DataFrame(query("SELECT * FROM alerts ORDER BY id DESC"))
    c1, c2, c3, c4 = st.columns(4)
    count = lambda col, val: int((alerts[col] == val).sum()) if not alerts.empty else 0  # noqa: E731
    c1.metric("Alerty krytyczne", count("priority", "Krytyczny"))
    c2.metric("Ostrzeżenia", count("priority", "Ostrzeżenie"))
    c3.metric("Błędy danych", count("category", "Błąd danych"))
    c4.metric("Zdarzenia w kolejce", len(events.pending(100)))

    st.subheader("Kolejka zdarzeń wyzwalających agentów")
    f1, f2, f3 = st.columns([3, 1, 1], vertical_alignment="bottom")
    kind = f1.selectbox("Zdarzenie testowe (PoC)", list(events.TEST_EVENTS), format_func=events.TEST_EVENTS.get)
    if f2.button("Wstrzyknij do kolejki", width="stretch"):
        eid = events.inject_test_event(kind)
        st.session_state["monitor_msg"] = f"Dodano zdarzenie #{eid}. Przetworzy je worker albo przycisk obok."
        st.rerun()
    if f3.button("Przetwórz kolejkę", width="stretch"):
        _process_queue()
        st.rerun()
    queue = pd.DataFrame(events.recent(30))
    if not queue.empty:
        queue["źródło"] = queue["source"].map(events.SOURCE_LABELS)
        queue["dane"] = queue["payload_json"].map(lambda p: ", ".join(f"{k}: {v}" for k, v in json.loads(p).items()))
        st.dataframe(queue.rename(columns={"id": "#", "created_at": "Czas", "type": "Typ", "status": "Status",
                                           "routed_to": "Ścieżka agentów"})
                     [["#", "Czas", "źródło", "Typ", "dane", "Status", "Ścieżka agentów"]],
                     hide_index=True, width="stretch")
        with st.expander("Wynik ostatniego przetworzonego zdarzenia"):
            last = next((e for e in events.recent(30) if e["result"]), None)
            st.markdown(last["result"] if last else "—")

    st.subheader("Alerty")
    if alerts.empty:
        st.caption("Brak alertów – uruchom skan.")
    else:
        st.dataframe(alerts.rename(columns={"event_ts": "Czas zdarzenia", "priority": "Priorytet",
                                            "category": "Kategoria", "description": "Opis", "status": "Status"})
                     [["Czas zdarzenia", "Priorytet", "Kategoria", "Opis", "Status"]], hide_index=True, width="stretch")
        for a in alerts[alerts["priority"] != "Informacja"].head(5).to_dict("records"):
            with st.expander(f"{a['priority']} · {a['category']}: {a['description'][:90]}"):
                st.badge(a["priority"], color=PRIORITY_COLORS.get(a["priority"], "gray"))
                if a["diagnosis"]:
                    st.markdown(f"**Diagnoza (baza wiedzy):** {a['diagnosis']}")
                if a["suggested_action"]:
                    st.markdown(f"**Sugerowane działanie:** {a['suggested_action']}")
                st.markdown("**Kanały:** " + (", ".join(json.loads(a["channels_json"])) or "brak (poniżej progu)"))
                if a["evidence"]:
                    st.code(a["evidence"], language="log")

    left, right = st.columns(2, gap="large")
    with left:
        st.subheader("Progi alertów")
        with st.form("thresholds"):
            labels = {"integration_errors_15min": "Błędy integracji / 15 min", "queue_delay_s": "Opóźnienie kolejki (s)",
                      "step_growth_pct": "Wzrost liczby spraw w kroku (%)",
                      "slow_response_warnings_24h": "Ostrzeżenia o wolnych odpowiedziach / 24 h",
                      "scan_interval_min": "Częstotliwość skanu (min, po restarcie workera)"}
            values = {k: st.number_input(v, min_value=1, value=int(db.get_setting(k)), step=1) for k, v in labels.items()}
            if st.form_submit_button("Zapisz progi"):
                for k, v in values.items():
                    db.set_setting(k, str(v))
                st.session_state["monitor_msg"] = "Zapisano progi – obowiązują od następnego skanu."
                st.rerun()
        upload = st.file_uploader("Wgraj próbkę logów (CSV lub JSONL: ts, level, component, instance_id, message)",
                                  type=["csv", "jsonl", "json"])
        if upload and st.button("Dodaj logi"):
            try:
                st.session_state["monitor_msg"] = f"Dodano wpisy logów: {_upload_logs(upload)}. Uruchom skan."
                st.rerun()
            except Exception as exc:
                st.error(f"Nie udało się wczytać pliku: {exc}")
    with right:
        st.subheader("Historia powiadomień")
        notes = pd.DataFrame(notifications.recent(50))
        if notes.empty:
            st.caption("Brak powiadomień.")
        else:
            st.dataframe(notes.rename(columns={"ts": "Czas", "channel": "Kanał", "recipient": "Adresat",
                                               "subject": "Temat", "status": "Status"})
                         [["Czas", "Kanał", "Adresat", "Temat", "Status"]], hide_index=True, width="stretch")
        st.caption("W PoC wysyłka jest symulowana: wpisy trafiają tylko do tej listy.")
