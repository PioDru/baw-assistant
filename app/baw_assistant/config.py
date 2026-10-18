"""Konfiguracja aplikacji z zmiennych środowiskowych."""
from __future__ import annotations

import os
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
    llm_temperature: float | None
    sim_now: datetime
    seed: int
    langfuse_flag: bool
    langfuse_public_key: str
    langfuse_secret_key: str
    langfuse_base_url: str
    qdrant_url: str

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
        # Puste = domyślna temperatura modelu (część modeli nie przyjmuje tego parametru).
        llm_temperature=float(_env("LLM_TEMPERATURE")) if _env("LLM_TEMPERATURE") else None,
        # Symulowany „teraz” – dane syntetyczne są ułożone wokół tej chwili,
        # dzięki czemu scenariusze testowe dają zawsze te same odpowiedzi.
        sim_now=datetime.fromisoformat(_env("SIM_NOW", "2026-10-18T10:45:00")),
        seed=int(_env("DATA_SEED", "42")),
        langfuse_flag=_env("LANGFUSE_ENABLED", "true").lower() in ("1", "true", "yes"),
        langfuse_public_key=_env("LANGFUSE_PUBLIC_KEY"),
        langfuse_secret_key=_env("LANGFUSE_SECRET_KEY"),
        langfuse_base_url=_env("LANGFUSE_BASE_URL", "http://localhost:3000"),
        qdrant_url=_env("QDRANT_URL", "http://localhost:6333"),
    )
