"""Warstwa modeli: jeden punkt tworzenia modelu przez LangChain init_chat_model.

LLM_MODEL=openai:gpt-6-luna  → model chmurowy przez API (domyślnie)
LLM_MODEL=mock               → bez LLM; agenci używają reguł i szablonów (testy, demo offline)
Zmiana dostawcy to zmiana napisu provider:model, bez zmian w kodzie agentów.
Gdy modelu nie da się utworzyć (np. brak klucza API), agenci przechodzą na reguły i szablony.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from .config import get_settings

log = logging.getLogger(__name__)
_init_error: str | None = None


@lru_cache(maxsize=1)
def get_llm() -> Any | None:
    global _init_error
    settings = get_settings()
    if settings.mock_llm:
        return None
    try:
        from langchain.chat_models import init_chat_model

        kwargs: dict[str, Any] = {}
        if settings.llm_temperature is not None:
            kwargs["temperature"] = settings.llm_temperature
        return init_chat_model(settings.llm_model, **kwargs)
    except Exception as exc:
        _init_error = str(exc)
        log.warning("Nie udało się utworzyć modelu %s, używam reguł i szablonów: %s", settings.llm_model, exc)
        return None


def llm_label() -> str:
    settings = get_settings()
    if settings.mock_llm:
        return "tryb mock (bez LLM)"
    if get_llm() is None:
        return f"{settings.llm_model} – niedostępny, tryb reguł"
    return settings.llm_model


def llm_error() -> str | None:
    get_llm()
    return _init_error
