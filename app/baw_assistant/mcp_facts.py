"""Adapter konwertujący wyniki narzędzi MCP na ustrukturyzowane obiekty Fact dla Redaktora-Weryfikatora."""
from __future__ import annotations

from typing import Any
from .state import Fact


def mcp_result_to_facts(tool_name: str, data: Any, source_label: str = "MCP") -> list[Fact]:
    """Przekształca surowy wynik narzędzia MCP w listę faktów gotowych do weryfikacji."""
    facts: list[Fact] = []

    if tool_name == "get_instance_details" and isinstance(data, dict):
        if "error" not in data:
            facts.append({
                "id": "",
                "text": (
                    f"Sprawa {data['id']} ({data.get('process_name', '')}) ma status „{data.get('status')}” "
                    f"i jest w kroku „{data.get('current_step')}”. Przypisana osoba: {data.get('assignee_name', 'Nieprzypisany')}."
                ),
                "source": f"MCP [{source_label}]",
                "agent": "Agent Instancji",
            })
            if data.get("sla_due"):
                sla_rem = data.get("sla_days_remaining")
                if sla_rem is not None and sla_rem < 0:
                    facts.append({
                        "id": "",
                        "text": f"Termin SLA: {data['sla_due']} – przekroczony o {-sla_rem} dni.",
                        "source": f"MCP [{source_label}]",
                        "agent": "Agent Instancji",
                    })
                else:
                    facts.append({
                        "id": "",
                        "text": f"Termin SLA: {data['sla_due']}.",
                        "source": f"MCP [{source_label}]",
                        "agent": "Agent Instancji",
                    })
            if data.get("missing_docs"):
                facts.append({
                    "id": "",
                    "text": f"Brakujące dokumenty: {', '.join(data['missing_docs'])}.",
                    "source": f"MCP [{source_label}]",
                    "agent": "Agent Instancji",
                })

    return facts
