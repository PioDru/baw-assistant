"""Konfiguracja aplikacji z zmiennych środowiskowych."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    db_path: Path
    llm_model: str
    llm_base_url: str
    llm_api_key: str
    llm_temperature: float | None
    llm_max_tokens: int | None
    sim_now: datetime
    seed: int
    langfuse_flag: bool
    langfuse_public_key: str
    langfuse_secret_key: str
    langfuse_base_url: str
    qdrant_url: str
    mcp_provider_mode: str
    mcp_docker_sse_url: str
    mcp_docker_bearer_token: str
    mcp_local_command: str
    mcp_local_args: list[str]

    @property
    def mock_llm(self) -> bool:
        """Tryb bez LLM: odpowiedzi składane z szablonów (testy, demo offline)."""
        return self.llm_model.lower() in ("mock", "mock:rules")

    @property
    def langfuse_enabled(self) -> bool:
        return self.langfuse_flag and bool(self.langfuse_public_key and self.langfuse_secret_key)


def get_settings() -> Settings:
    default_db = Path(__file__).resolve().parent.parent / "data" / "baw.db"
    return Settings(
        db_path=Path(_env("DB_PATH", str(default_db))),
        llm_model=_env("LLM_MODEL", "openai:gpt-6-luna"),
        llm_base_url=_env("LLM_BASE_URL", _env("OPENAI_BASE_URL", "")),
        llm_api_key=_env("LLM_API_KEY", _env("OPENAI_API_KEY", "")),
        # Puste = domyślna temperatura modelu (część modeli nie przyjmuje tego parametru).
        llm_temperature=float(_env("LLM_TEMPERATURE")) if _env("LLM_TEMPERATURE") else None,
        llm_max_tokens=int(_env("LLM_MAX_TOKENS", "1024")) if _env("LLM_MAX_TOKENS", "1024") else 1024,
        # Symulowany „teraz” – dane syntetyczne są ułożone wokół tej chwili,
        # dzięki czemu scenariusze testowe dają zawsze te same odpowiedzi.
        sim_now=datetime.fromisoformat(_env("SIM_NOW", "2026-10-18T10:45:00")),
        seed=int(_env("DATA_SEED", "42")),
        langfuse_flag=_env("LANGFUSE_ENABLED", "true").lower() in ("1", "true", "yes"),
        langfuse_public_key=_env("LANGFUSE_PUBLIC_KEY"),
        langfuse_secret_key=_env("LANGFUSE_SECRET_KEY"),
        langfuse_base_url=_env("LANGFUSE_BASE_URL", "http://localhost:3000"),
        qdrant_url=_env("QDRANT_URL", "http://localhost:6333"),
        mcp_provider_mode=_env("MCP_PROVIDER_MODE", "internal"),
        mcp_docker_sse_url=_env("MCP_DOCKER_SSE_URL", "http://localhost:8080/sse"),
        mcp_docker_bearer_token=_env("MCP_DOCKER_BEARER_TOKEN"),
        mcp_local_command=_env("MCP_LOCAL_COMMAND", "python"),
        mcp_local_args=[arg.strip() for arg in _env("MCP_LOCAL_ARGS", "-m,baw_assistant.mcp_server").split(",") if arg.strip()],
    )
