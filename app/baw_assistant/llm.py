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


def create_llm(
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> Any | None:
    """Tworzy instancję modelu LLM dla zadanych parametrów lub domyślnych z konfiguracji."""
    global _init_error
    settings = get_settings()
    target_model = model or settings.llm_model
    if target_model.lower() in ("mock", "mock:rules"):
        return None
    try:
        from langchain.chat_models import init_chat_model

        kwargs: dict[str, Any] = {}
        target_base_url = base_url if base_url is not None else settings.llm_base_url
        if target_base_url:
            kwargs["base_url"] = target_base_url
        target_api_key = api_key if api_key is not None else settings.llm_api_key
        if target_api_key:
            kwargs["api_key"] = target_api_key
        target_temp = temperature if temperature is not None else settings.llm_temperature
        if target_temp is not None:
            kwargs["temperature"] = target_temp
        target_tokens = max_tokens if max_tokens is not None else settings.llm_max_tokens
        if target_tokens is not None:
            kwargs["max_tokens"] = target_tokens
        return init_chat_model(target_model, **kwargs)
    except Exception as exc:
        _init_error = str(exc)
        log.warning("Nie udało się utworzyć modelu %s: %s", target_model, exc)
        return None


@lru_cache(maxsize=1)
def get_llm() -> Any | None:
    return create_llm()


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
