"""Klient MCP obsługujący komunikację z lokalnym serwerem FastMCP i serwerem MCP_DOCKER (SSE)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from .config import get_settings

log = logging.getLogger(__name__)


@asynccontextmanager
async def get_docker_mcp_session() -> AsyncIterator[Any]:
    """Połączenie z serwerem MCP uruchomionym w kontenerze Docker przez transport SSE."""
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    settings = get_settings()
    headers: dict[str, str] = {}
    if settings.mcp_docker_bearer_token:
        headers["Authorization"] = f"Bearer {settings.mcp_docker_bearer_token}"
        log.info("Użycie autoryzacji Bearer Token dla sesji MCP Docker SSE")

    log.info("Inicjalizacja sesji MCP Docker SSE pod adresem: %s", settings.mcp_docker_sse_url)
    async with sse_client(settings.mcp_docker_sse_url, headers=headers if headers else None) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            log.info("Zainicjalizowano sesję MCP Docker SSE")
            yield session


@asynccontextmanager
async def get_internal_mcp_session() -> AsyncIterator[Any]:
    """Połączenie z lokalnym serwerem FastMCP przez proces stdio."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    settings = get_settings()
    log.info(
        "Inicjalizacja lokalnej sesji FastMCP (stdio) z komendą: %s %s",
        settings.mcp_local_command,
        " ".join(settings.mcp_local_args),
    )
    env = dict(os.environ)
    app_dir = str(Path(__file__).resolve().parent.parent)
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{app_dir}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else app_dir

    command = settings.mcp_local_command
    if command == "python" and sys.executable:
        command = sys.executable

    server_params = StdioServerParameters(
        command=command,
        args=settings.mcp_local_args,
        env=env,
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            log.info("Zainicjalizowano lokalną sesję FastMCP (stdio)")
            yield session


async def list_mcp_tools(provider: str | None = None) -> list[dict[str, Any]]:
    """Pobiera listę narzędzi udostępnianych przez serwer MCP."""
    settings = get_settings()
    mode = provider or settings.mcp_provider_mode

    log.info("Pobieranie listy narzędzi MCP (tryb: %s)", mode)
    if mode == "docker_sse":
        session_ctx = get_docker_mcp_session()
    elif mode == "internal":
        session_ctx = get_internal_mcp_session()
    else:
        err_msg = f"Nieobsługiwany lub wyłączony tryb MCP: {mode}"
        log.error(err_msg)
        raise ValueError(err_msg)

    async with session_ctx as session:
        result = await session.list_tools()
        tools_list = []
        for t in getattr(result, "tools", []):
            tools_list.append({
                "name": getattr(t, "name", ""),
                "description": getattr(t, "description", ""),
                "inputSchema": getattr(t, "inputSchema", {}),
            })
        log.info("Pobrano %d narzędzi z serwera MCP (%s)", len(tools_list), mode)
        return tools_list


def list_mcp_tools_sync(provider: str | None = None) -> list[dict[str, Any]]:
    """Synchroniczne pobranie listy narzędzi MCP."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(asyncio.run, list_mcp_tools(provider)).result()
    else:
        return asyncio.run(list_mcp_tools(provider))


async def call_mcp_tool(tool_name: str, arguments: dict[str, Any] | None = None, provider: str | None = None) -> Any:
    """Uniwersalne wykonanie narzędzia z wybranego źródła MCP."""
    settings = get_settings()
    mode = provider or settings.mcp_provider_mode
    args = arguments or {}

    log.info("Wywołanie narzędzia MCP '%s' (tryb: %s, argumenty: %s)", tool_name, mode, args)
    t0 = time.perf_counter()

    if mode == "docker_sse":
        session_ctx = get_docker_mcp_session()
    elif mode == "internal":
        session_ctx = get_internal_mcp_session()
    else:
        err_msg = f"Nieobsługiwany lub wyłączony tryb MCP: {mode}"
        log.error(err_msg)
        raise ValueError(err_msg)

    try:
        async with session_ctx as session:
            result = await session.call_tool(tool_name, arguments=args)
            latency_ms = (time.perf_counter() - t0) * 1000

            parsed_result: Any = result
            if hasattr(result, "content") and result.content:
                first_block = result.content[0]
                if hasattr(first_block, "text"):
                    try:
                        parsed_result = json.loads(first_block.text)
                    except json.JSONDecodeError:
                        parsed_result = first_block.text

            log.info(
                "Narzędzie MCP '%s' wykonane pomyślnie w %.1f ms",
                tool_name,
                latency_ms,
            )
            return parsed_result
    except Exception as e:
        latency_ms = (time.perf_counter() - t0) * 1000
        log.exception(
            "Błąd podczas wywołania narzędzia MCP '%s' (tryb: %s) po %.1f ms: %s",
            tool_name,
            mode,
            latency_ms,
            e,
        )
        raise


def call_mcp_tool_sync(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    provider: str | None = None,
) -> Any:
    """Synchroniczne wywołanie narzędzia MCP, bezpieczne dla dowolnego wątku."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(asyncio.run, call_mcp_tool(tool_name, arguments, provider)).result()
    else:
        return asyncio.run(call_mcp_tool(tool_name, arguments, provider))
