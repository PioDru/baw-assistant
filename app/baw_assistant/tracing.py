"""Obserwowalność: Langfuse (opcjonalnie) – callback handler dla LangGraph/LangChain.

Gdy klucze Langfuse nie są ustawione albo serwer jest niedostępny, aplikacja działa dalej
bez śledzenia. Własny ślad agentów (panel w UI) działa niezależnie od Langfuse.
"""
from __future__ import annotations

import contextlib
import logging
import os
from typing import Any, Iterator

from .config import get_settings

log = logging.getLogger(__name__)


def _prepare_env() -> None:
    settings = get_settings()
    os.environ.setdefault("LANGFUSE_BASE_URL", settings.langfuse_base_url)


def get_callbacks() -> list[Any]:
    settings = get_settings()
    if not settings.langfuse_enabled:
        return []
    try:
        _prepare_env()
        from langfuse.langchain import CallbackHandler

        return [CallbackHandler()]
    except Exception as exc:  # brak pakietu lub błędna konfiguracja nie może blokować aplikacji
        log.warning("Langfuse niedostępny: %s", exc)
        return []


@contextlib.contextmanager
def trace_context(user_id: str, session_id: str, role: str) -> Iterator[None]:
    settings = get_settings()
    if not settings.langfuse_enabled:
        yield
        return
    try:
        _prepare_env()
        from langfuse import propagate_attributes

        ctx = propagate_attributes(trace_name="asystent-baw", user_id=user_id, session_id=session_id,
                                   tags=["poc-v1", role])
    except Exception as exc:
        log.warning("Langfuse niedostępny: %s", exc)
        yield
        return
    with ctx:
        yield


def flush() -> None:
    if not get_settings().langfuse_enabled:
        return
    with contextlib.suppress(Exception):
        from langfuse import get_client

        get_client().flush()
