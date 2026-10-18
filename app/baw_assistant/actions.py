"""Akcje sugerowane z zatwierdzeniem przez człowieka (human-in-the-loop) – LangGraph interrupt().

Przebieg: przygotowanie treści → interrupt() (graf czeka, stan zapisany w checkpointerze SQLite) →
decyzja użytkownika (Zatwierdź / Odrzuć, z możliwością edycji treści) → finalizacja.
W PoC zatwierdzona akcja kończy się tylko symulowanym powiadomieniem – agenci nie wykonują akcji w BAW.
"""
from __future__ import annotations

import sqlite3
import uuid
from functools import lru_cache
from typing import Any, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from . import analytics, notifications, tools
from .agents.common import (ACTION_ESCALATE_SLA, ACTION_INCIDENT, ACTION_PING_DEPT, ACTION_REBALANCE, ACTION_REMIND)
from .db import db_path, execute, now_iso, query_one
from .rbac import can_view_instance, get_user

INSTANCE_ACTIONS = {ACTION_REMIND, ACTION_ESCALATE_SLA, ACTION_INCIDENT, ACTION_PING_DEPT}


class ActionState(TypedDict, total=False):
    action: str
    instance_id: str | None
    user_id: str
    channel: str
    recipient: str
    subject: str
    draft: str
    approved: bool
    final_text: str
    status: str
    notification_id: int | None


def _manager_of(team_id: str) -> str:
    row = query_one("SELECT u.name FROM teams t JOIN users u ON u.id = t.manager_id WHERE t.id = ?", (team_id,))
    return row["name"] if row else "Kierownik zespołu"


def _draft(state: ActionState) -> dict:
    user = get_user(state["user_id"])
    action, iid = state["action"], state.get("instance_id")
    inst = tools.get_instance(iid) if iid else None
    sign = f"\n\nZ poważaniem\n{user.name}"
    if action == ACTION_REMIND:
        missing = ", ".join(inst["missing_docs"]) or "brak"
        return {"channel": "E-mail", "recipient": f"Klient: {inst['customer']}",
                "subject": f"Przypomnienie: brakujące dokumenty – sprawa {iid}",
                "draft": f"Dzień dobry,\n\nw sprawie {iid} ({inst['process_name']}) nadal brakuje dokumentu: {missing}. "
                         "Prosimy o przesłanie go w odpowiedzi na tę wiadomość lub przez portal klienta. "
                         f"Planowany termin obsługi sprawy to {tools.fmt_date(inst['sla_due'])}.{sign}"}
    if action == ACTION_ESCALATE_SLA:
        external = [t for t in tools.get_tasks(iid) if t["status"] == "Otwarte" and t["assignee_type"] != "user"]
        reason = f"czeka na: {external[-1]['assignee_label']}" if external else f"krok „{inst['current_step']}”"
        return {"channel": "Zadanie BAW", "recipient": f"{_manager_of(inst['team_id'])} (kierownik zespołu)",
                "subject": f"Ryzyko SLA – sprawa {iid}",
                "draft": f"Sprawa {iid} ({inst['process_name']}) jest zagrożona przekroczeniem terminu SLA "
                         f"{tools.fmt_date(inst['sla_due'])}. Obecnie {reason}. Proszę o decyzję co do dalszych "
                         f"kroków.{sign}"}
    if action == ACTION_INCIDENT:
        logs = tools.get_instance_logs(iid)
        last = next((lg for lg in reversed(logs) if lg["level"] == "ERROR"), logs[-1] if logs else None)
        detail = f"{tools.fmt_datetime(last['ts'])} {last['component']}: {last['message']}" if last else "brak wpisu"
        return {"channel": "Teams", "recipient": "#baw-incydenty", "subject": f"Incydent techniczny – sprawa {iid}",
                "draft": f"Sprawa {iid} jest zatrzymana przez błąd techniczny. Ostatni błąd: {detail}. "
                         f"Proszę o sprawdzenie i wznowienie instancji.{sign}"}
    if action == ACTION_PING_DEPT:
        dept = next((t for t in tools.get_tasks(iid) if t["status"] == "Otwarte" and t["assignee_type"] == "department"),
                    None)
        name = dept["assignee_label"] if dept else "Dział realizujący"
        due = tools.fmt_date(dept["due_at"]) if dept else "—"
        return {"channel": "E-mail", "recipient": name, "subject": f"Monit: zadanie w sprawie {iid}",
                "draft": f"Dzień dobry,\n\nprzypominam o zadaniu „{dept['name'] if dept else ''}” w sprawie {iid}, "
                         f"którego termin minął {due}. Proszę o informację, kiedy możemy spodziewać się "
                         f"odpowiedzi.{sign}"}
    if action == ACTION_REBALANCE:
        t = analytics.team_overview(user)
        top = t["per_assignee"][0]
        target = t["least_loaded"]["name"]
        cases = ", ".join(t.get("overdue_instances", [])[:2]) or "—"
        return {"channel": "Zadanie BAW", "recipient": t["team_name"],
                "subject": "Propozycja przesunięcia spraw w zespole",
                "draft": f"Proponuję przekazać sprawy {cases} od: {top['name']} (aktywne: {top['total']}, po terminie: "
                         f"{top['overdue']}) do: {target} (aktywne: {t['least_loaded']['total']}). Zmiana przypisania "
                         f"wymaga wykonania w BAW przez kierownika.{sign}"}
    raise ValueError(f"Nieznana akcja: {action}")


def prepare_node(state: ActionState) -> ActionState:
    return _draft(state)


def approval_node(state: ActionState) -> ActionState:
    decision = interrupt({"action": state["action"], "channel": state["channel"], "recipient": state["recipient"],
                          "subject": state["subject"], "draft": state["draft"]})
    return {"approved": bool(decision.get("approved")), "final_text": decision.get("text") or state["draft"]}


def finalize_node(state: ActionState) -> ActionState:
    user = get_user(state["user_id"])
    nid = None
    if state["approved"]:
        nid = notifications.send(state["channel"], state["recipient"], state["subject"], state["final_text"],
                                 f"akcja zatwierdzona przez: {user.name}")
        status = "Zatwierdzona – wysłano (symulacja)"
    else:
        status = "Odrzucona"
    return {"status": status, "notification_id": nid}


def _log(thread_id: str, state: dict) -> None:
    execute("INSERT INTO action_log (ts, thread_id, user_id, action, instance_id, status, channel, recipient, text)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (now_iso(), thread_id, state.get("user_id"), state.get("action"), state.get("instance_id"),
             state.get("status"), state.get("channel"), state.get("recipient"),
             state.get("final_text") or state.get("draft")))


@lru_cache(maxsize=4)
def _graph(path: str):
    conn = sqlite3.connect(path, check_same_thread=False)
    g = StateGraph(ActionState)
    g.add_node("prepare", prepare_node)
    g.add_node("approval", approval_node)
    g.add_node("finalize", finalize_node)
    g.add_edge(START, "prepare")
    g.add_edge("prepare", "approval")
    g.add_edge("approval", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=SqliteSaver(conn))


def action_graph():
    return _graph(str(db_path().with_name("checkpoints.db")))


def start_action(action: str, user_id: str, instance_id: str | None = None) -> dict[str, Any]:
    """Przygotowuje akcję i zatrzymuje graf na zatwierdzeniu. Zwraca identyfikator wątku i treść do akceptacji."""
    user = get_user(user_id)
    if action in INSTANCE_ACTIONS and (not instance_id or not can_view_instance(user, instance_id)):
        raise PermissionError("Brak uprawnień do sprawy lub brak wybranej sprawy.")
    thread_id = str(uuid.uuid4())
    result = action_graph().invoke({"action": action, "instance_id": instance_id, "user_id": user_id},
                                   config={"configurable": {"thread_id": thread_id}})
    payload = result["__interrupt__"][0].value
    _log(thread_id, {**payload, "user_id": user_id, "instance_id": instance_id, "status": "Oczekuje na zatwierdzenie"})
    return {"thread_id": thread_id, **payload}


def resume_action(thread_id: str, approved: bool, text: str | None = None) -> dict[str, Any]:
    """Wznawia wstrzymany graf decyzją użytkownika (Command(resume=…))."""
    result = action_graph().invoke(Command(resume={"approved": approved, "text": text}),
                                   config={"configurable": {"thread_id": thread_id}})
    _log(thread_id, result)
    return result
