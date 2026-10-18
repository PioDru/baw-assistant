"""Graf LangGraph PoC v1: Dyspozytor → Agent Instancji ⇄ Redaktor-Weryfikator."""
from __future__ import annotations

import time
import uuid
from functools import lru_cache
from typing import Any

from langgraph.graph import END, START, StateGraph

from . import db, tracing
from .agents.dispatcher import dispatcher_node
from .agents.editor_verifier import editor_verifier_node
from .agents.instance_agent import instance_agent_node
from .state import AssistantState


def _after_dispatcher(state: AssistantState) -> str:
    return "instance_agent" if state["decision"] == "route" else "editor_verifier"


def _after_verifier(state: AssistantState) -> str:
    return "instance_agent" if state.get("verdict") == "retry" else END


@lru_cache(maxsize=1)
def build_graph():
    g = StateGraph(AssistantState)
    g.add_node("dispatcher", dispatcher_node)
    g.add_node("instance_agent", instance_agent_node)
    g.add_node("editor_verifier", editor_verifier_node)
    g.add_edge(START, "dispatcher")
    g.add_conditional_edges("dispatcher", _after_dispatcher, ["instance_agent", "editor_verifier"])
    g.add_edge("instance_agent", "editor_verifier")
    g.add_conditional_edges("editor_verifier", _after_verifier, ["instance_agent", END])
    return g.compile()


def ask(question: str, user_id: str, role: str, context_instance_id: str | None = None,
        session_id: str | None = None) -> dict[str, Any]:
    """Uruchamia graf dla jednego pytania i zapisuje przebieg w tabeli agent_runs."""
    started = time.perf_counter()
    session_id = session_id or str(uuid.uuid4())
    initial: AssistantState = {"question": question, "user_id": user_id, "role": role,
                               "context_instance_id": context_instance_id, "trace": []}
    config = {"callbacks": tracing.get_callbacks(), "run_name": "asystent-baw",
              "metadata": {"user_id": user_id, "role": role}}
    with tracing.trace_context(user_id=user_id, session_id=session_id, role=role):
        result = build_graph().invoke(initial, config=config)
    # Langfuse wysyła ślady w tle – bez synchronicznego flush, żeby niedostępny serwer nie spowalniał odpowiedzi.
    latency = int((time.perf_counter() - started) * 1000)
    result["latency_ms"] = latency
    path = [step["agent"] for step in result.get("trace", [])]
    db.log_run(user_id, role, question, result.get("intent", ""), path, result.get("verdict", ""),
               latency, result.get("answer", ""))
    return result
