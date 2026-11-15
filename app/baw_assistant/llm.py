"""Warstwa modeli: jeden punkt tworzenia modelu przez LangChain init_chat_model.

LLM_MODEL=openai:gpt-6-luna  → model chmurowy przez API (domyślnie)
LLM_MODEL=mock               → bez LLM; agenci używają reguł i szablonów (testy, demo offline)
Zmiana dostawcy to zmiana napisu provider:model, bez zmian w kodzie agentów.
Gdy modelu nie da się utworzyć (np. brak klucza API), agenci przechodzą na reguły i szablony.
"""
from __future__ import annotations

import logging
import os
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


@lru_cache(maxsize=1)
def get_fast_llm() -> Any | None:
    """Zwraca lekki model (np. openai:baw-fast) dedykowany dla zadań triage i klasyfikacji."""
    settings = get_settings()
    if settings.mock_llm:
        return None
    return create_llm(model="openai:baw-fast") or get_llm()


def resolve_configured_model(model_name: str | None = None) -> str:
    """Zwraca czytelną nazwę skonfigurowanego modelu z rozwinięciem logicznych aliasów bramki LiteLLM."""
    settings = get_settings()
    target = model_name or settings.llm_model
    if target.lower() in ("mock", "mock:rules"):
        return "tryb mock (bez LLM)"

    local_moe = (settings.local_moe_model or os.getenv("LOCAL_MOE_MODEL", "")).replace("openai/", "").replace("openai:", "")
    local_fast = (settings.local_fast_model or os.getenv("LOCAL_FAST_MODEL", "")).replace("openai/", "").replace("openai:", "")
    fallback = (settings.fallback_cloud_model or os.getenv("FALLBACK_CLOUD_MODEL", "")).replace("openai/", "").replace("openai:", "")

    if "baw-moe-main" in target:
        details = local_moe or fallback
        return f"{target} ({details})" if details else target
    if "baw-fast" in target:
        details = local_fast or fallback
        return f"{target} ({details})" if details else target
    if "baw-cloud-fallback" in target:
        return f"{target} ({fallback})" if fallback else target

    return target


def extract_model_name(obj: Any, llm_instance: Any = None) -> str | None:
    """Wydobywa rzeczywistą nazwę modelu z metadanych odpowiedzi LangChain / LiteLLM."""
    if obj is not None:
        response_meta = getattr(obj, "response_metadata", None)
        if isinstance(response_meta, dict):
            model = response_meta.get("model_name") or response_meta.get("model")
            if model:
                return str(model).replace("openai/", "").replace("openai:", "")
        usage_meta = getattr(obj, "usage_metadata", None)
        if isinstance(usage_meta, dict):
            model = usage_meta.get("model_name") or usage_meta.get("model")
            if model:
                return str(model).replace("openai/", "").replace("openai:", "")
        add_kwargs = getattr(obj, "additional_kwargs", None)
        if isinstance(add_kwargs, dict):
            model = add_kwargs.get("model") or add_kwargs.get("model_name")
            if model:
                return str(model).replace("openai/", "").replace("openai:", "")

    if llm_instance is not None:
        model = getattr(llm_instance, "model_name", None) or getattr(llm_instance, "model", None)
        if model:
            return str(model).replace("openai/", "").replace("openai:", "")

    return None


def llm_label() -> str:
    settings = get_settings()
    if settings.mock_llm:
        return "tryb mock (bez LLM)"
    resolved = resolve_configured_model(settings.llm_model)
    if get_llm() is None:
        return f"{resolved} – niedostępny, tryb reguł"
    return resolved


def llm_error() -> str | None:
    get_llm()
    return _init_error
