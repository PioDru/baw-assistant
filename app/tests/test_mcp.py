"""Testy integracji z serwerami MCP (FastMCP lokalny, konfiguracja, adapter faktów)."""
from __future__ import annotations

from baw_assistant.agents.common import AGENT_INSTANCE, Findings
from baw_assistant.agents.instance_agent import diagnosis, details
from baw_assistant.config import get_settings
from baw_assistant.graph import ask
from baw_assistant.mcp_client import call_mcp_tool_sync, list_mcp_tools_sync
from baw_assistant.mcp_facts import mcp_result_to_facts
from baw_assistant.mcp_server import get_instance_details, mcp


def test_mcp_server_metadata():
    assert mcp.name == "BAW-Assistant-Local-MCP-Server"


def test_mcp_get_instance_details_existing():
    result = get_instance_details("REK-2026-0412")
    assert result["id"] == "REK-2026-0412"
    assert result["current_step"] == "Weryfikacja dokumentów"
    assert result["missing_docs"] == ["Protokół szkody"]
    assert result["sla_days_remaining"] == 3
    assert result["is_sla_overdue"] is False
    assert isinstance(result["days_since_last_change"], int)


def test_mcp_get_instance_details_not_found():
    result = get_instance_details("NIE-ISTNIEJE-0001")
    assert "error" in result
    assert "nie została znaleziona" in result["error"]


def test_mcp_facts_adapter():
    data = get_instance_details("REK-2026-0412")
    facts = mcp_result_to_facts("get_instance_details", data, source_label="local")
    assert len(facts) >= 2
    texts = " ".join(f["text"] for f in facts)
    assert "REK-2026-0412" in texts
    assert "Weryfikacja dokumentów" in texts
    assert "Protokół szkody" in texts
    assert all(f["source"] == "MCP [local]" for f in facts)
    assert all(f["agent"] == "Agent Instancji" for f in facts)


def test_mcp_settings_defaults(monkeypatch):
    monkeypatch.delenv("MCP_DOCKER_SSE_URL", raising=False)
    monkeypatch.delenv("MCP_DOCKER_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("MCP_PROVIDER_MODE", raising=False)
    monkeypatch.delenv("MCP_LOCAL_COMMAND", raising=False)
    monkeypatch.delenv("MCP_LOCAL_ARGS", raising=False)
    settings = get_settings()
    assert settings.mcp_provider_mode in ("internal", "docker_sse", "disabled")
    assert settings.mcp_docker_sse_url == "http://localhost:8080/sse"
    assert settings.mcp_docker_bearer_token == ""
    assert settings.mcp_local_command == "python"
    assert settings.mcp_local_args == ["-m", "baw_assistant.mcp_server"]


def test_mcp_docker_session_headers(monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager
    from baw_assistant.mcp_client import get_docker_mcp_session

    captured_kwargs = {}

    @asynccontextmanager
    async def mock_sse_client(url, headers=None, **kwargs):
        captured_kwargs["url"] = url
        captured_kwargs["headers"] = headers
        yield ("read_stream", "write_stream")

    class MockSession:
        def __init__(self, read, write):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass
        async def initialize(self):
            pass

    monkeypatch.setenv("MCP_DOCKER_BEARER_TOKEN", "secret-test-token-123")
    monkeypatch.setenv("MCP_DOCKER_SSE_URL", "http://localhost:8199/sse")
    monkeypatch.setattr("mcp.client.sse.sse_client", mock_sse_client)
    monkeypatch.setattr("mcp.ClientSession", MockSession)

    async def run_test():
        async with get_docker_mcp_session() as s:
            assert s is not None

    asyncio.run(run_test())
    assert captured_kwargs["url"] == "http://localhost:8199/sse"
    assert captured_kwargs["headers"] == {"Authorization": "Bearer secret-test-token-123"}


def test_mcp_client_sync_call():
    result = call_mcp_tool_sync("get_instance_details", {"instance_id": "REK-2026-0412"}, provider="internal")
    assert isinstance(result, dict)
    assert result["id"] == "REK-2026-0412"
    assert result["status"] == "W toku"
    assert result["current_step"] == "Weryfikacja dokumentów"


def test_mcp_list_tools():
    tools = list_mcp_tools_sync(provider="internal")
    assert isinstance(tools, list)
    assert len(tools) >= 1
    tool_names = [t["name"] for t in tools]
    assert "get_instance_details" in tool_names
    assert "description" in tools[0]
    assert "inputSchema" in tools[0]


def test_instance_agent_uses_mcp_source():
    f = Findings(AGENT_INSTANCE)
    diagnosis(f, "REK-2026-0412", "pracownik")
    sources = [fact["source"] for fact in f.facts]
    assert any("MCP [internal]" in src for src in sources)


def test_ask_graph_integration_with_mcp():
    res = ask("Dlaczego ta sprawa stoi?", "u_anna", "pracownik", "REK-2026-0412")
    assert res["verdict"] == "ok"
    assert any("MCP [internal]" in s.get("source", "") for s in res["numbered_facts"])
