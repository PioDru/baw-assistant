"""Lokalny serwer FastMCP udostępniający narzędzia odczytu platformy IBM BAW."""
from __future__ import annotations

import logging
import sys
from typing import Any
try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    from mcp.server.mcpserver import MCPServer as FastMCP
from . import tools

log = logging.getLogger(__name__)

# Inicjalizacja serwera MCP o ustalonej nazwie
mcp = FastMCP("BAW-Assistant-Local-MCP-Server")


@mcp.tool()
def get_instance_details(instance_id: str) -> dict[str, Any]:
    """Zwraca kompleksowe szczegóły i stan instancji sprawy IBM BAW.

    Zwracane informacje obejmują: identyfikator, nazwę procesu, status,
    bieżący krok, osobę przypisaną, termin SLA, dni do przekroczenia SLA
    oraz listę brakujących dokumentów.
    """
    log.info("Odebrano żądanie get_instance_details dla sprawy: %s", instance_id)
    inst = tools.get_instance(instance_id)
    if not inst:
        log.warning("Instancja sprawy '%s' nie została znaleziona w bazie", instance_id)
        return {"error": f"Instancja sprawy o identyfikatorze '{instance_id}' nie została znaleziona."}

    # Wyliczenie relacji czasowych względem bieżącego czasu symulacji
    days_to_sla = tools.days_between(tools.now(), inst["sla_due"])
    idle_days = tools.days_between(inst["last_change_at"])

    log.info(
        "Pomyślnie pobrano szczegóły sprawy %s (status: %s, krok: %s, dni do SLA: %s)",
        instance_id,
        inst.get("status"),
        inst.get("current_step"),
        days_to_sla,
    )

    return {
        "id": inst["id"],
        "process_id": inst["process_id"],
        "process_name": inst["process_name"],
        "status": inst["status"],
        "current_step": inst["current_step"],
        "assignee_name": inst.get("assignee_name") or "Nieprzypisany",
        "sla_due": tools.fmt_date(inst["sla_due"]),
        "sla_days_remaining": days_to_sla,
        "is_sla_overdue": days_to_sla < 0,
        "days_since_last_change": idle_days,
        "missing_docs": inst.get("missing_docs", []),
        "last_change_at": inst.get("last_change_at"),
        "raw_sla_due": inst.get("sla_due"),
    }


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stderr,
    )
    log.info("Uruchamianie serwera BAW-Assistant-Local-MCP-Server...")
    mcp.run()
