"""Graf LangGraph PoC v2.

Pytanie lub zdarzenie → Dyspozytor → agenci (równolegle) → Redaktor-Weryfikator → [powiadomienia] → koniec.
"""
from __future__ import annotations

import json
import time
import uuid
from functools import lru_cache
from typing import Any

from langgraph.graph import END, START, StateGraph

from . import db, tracing
from .agents.analyst_agent import analyst_agent_node
from .agents.common import AGENT_ANALYST, AGENT_INSTANCE, AGENT_MONITOR
from .agents.dispatcher import dispatcher_node
from .agents.editor_verifier import editor_verifier_node
from .agents.instance_agent import instance_agent_node
from .agents.monitor_agent import monitor_agent_node
from .agents.notifier import notifier_node
from .state import AssistantState

NODE_BY_AGENT = {AGENT_INSTANCE: "instance_agent", AGENT_ANALYST: "analyst_agent", AGENT_MONITOR: "monitor_agent"}


def _after_dispatcher(state: AssistantState) -> list[str]:
    if state["decision"] != "route" or not state.get("agents"):
        return ["editor_verifier"]
    return [NODE_BY_AGENT[a] for a in state["agents"]]  # kilka węzłów = praca równoległa


def _after_editor(state: AssistantState) -> str:
    if state.get("mode") == "event" and state.get("verdict") in ("ok", "blocked"):
        return "notifier"
    return END


@lru_cache(maxsize=1)
def build_graph():
    g = StateGraph(AssistantState)
    g.add_node("dispatcher", dispatcher_node)
    g.add_node("instance_agent", instance_agent_node)
    g.add_node("analyst_agent", analyst_agent_node)
    g.add_node("monitor_agent", monitor_agent_node)
    g.add_node("editor_verifier", editor_verifier_node)
    g.add_node("notifier", notifier_node)
    g.add_edge(START, "dispatcher")
    g.add_conditional_edges("dispatcher", _after_dispatcher,
                            ["instance_agent", "analyst_agent", "monitor_agent", "editor_verifier"])
    for node in ("instance_agent", "analyst_agent", "monitor_agent"):
        g.add_edge(node, "editor_verifier")
    g.add_conditional_edges("editor_verifier", _after_editor, ["notifier", END])
    g.add_edge("notifier", END)
    return g.compile()


def _run(initial: AssistantState, user_id: str, role: str, session_id: str) -> dict[str, Any]:
    config = {"callbacks": tracing.get_callbacks(), "run_name": "asystent-baw",
              "metadata": {"user_id": user_id, "role": role, "mode": initial.get("mode")}}
    with tracing.trace_context(user_id=user_id, session_id=session_id, role=role):
        return build_graph().invoke(initial, config=config)


def ask(question: str, user_id: str, role: str, context_instance_id: str | None = None,
        session_id: str | None = None, params: dict | None = None,
        on_token: Any | None = None) -> dict[str, Any]:
    """Pytanie użytkownika (tryb interaktywny). Przebieg trafia do tabeli agent_runs."""
    started = time.perf_counter()
    initial: AssistantState = {"mode": "chat", "question": question, "user_id": user_id, "role": role,
                               "context_instance_id": context_instance_id, "params": params or {}, "trace": [],
                               "facts": [], "leads": [], "suggested_actions": [], "recommendations": [],
                               "alerts": [], "data": {}, "on_token": on_token}
    result = _run(initial, user_id, role, session_id or str(uuid.uuid4()))
    result["latency_ms"] = int((time.perf_counter() - started) * 1000)
    result["suggested_actions"] = list(dict.fromkeys(result.get("suggested_actions") or []))
    db.log_run(user_id, role, question, result.get("intent", ""), [s["agent"] for s in result.get("trace", [])],
               result.get("verdict", ""), result["latency_ms"], result.get("answer", ""))
    return result


def handle_event(event: dict) -> dict[str, Any]:
    """Zdarzenie z kolejki (tryb systemowy: UCA, EDA, harmonogram) – konto techniczne System BAW."""
    started = time.perf_counter()
    payload = event.get("payload") or json.loads(event.get("payload_json") or "{}")
    question = f"Zdarzenie {event['source']} {event['type']}: {json.dumps(payload, ensure_ascii=False)}"
    initial: AssistantState = {"mode": "event", "event": {**event, "payload": payload}, "question": question,
                               "user_id": "u_system", "role": "system", "trace": [], "facts": [], "leads": [],
                               "suggested_actions": [], "recommendations": [], "alerts": [], "data": {}}
    result = _run(initial, "u_system", "system", f"event-{event.get('id', uuid.uuid4())}")
    result["latency_ms"] = int((time.perf_counter() - started) * 1000)
    db.log_run("u_system", "system", question, result.get("intent", ""),
               [s["agent"] for s in result.get("trace", [])], result.get("verdict", ""), result["latency_ms"],
               result.get("answer", ""))
    return result
