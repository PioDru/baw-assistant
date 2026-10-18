"""Wspólna konfiguracja testów: osobna baza z danymi syntetycznymi, Qdrant w pamięci, tryb mock (bez LLM)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session", autouse=True)
def synthetic_db(tmp_path_factory):
    db = tmp_path_factory.mktemp("data") / "baw-test.db"
    os.environ.update({"DB_PATH": str(db), "LLM_MODEL": "mock", "LANGFUSE_ENABLED": "false",
                       "SIM_NOW": "2026-10-18T10:45:00", "DATA_SEED": "42", "QDRANT_URL": ":memory:",
                       "EMBEDDING_MODEL": "hash"})
    from baw_assistant import kb
    from baw_assistant.data_generator import generate
    from baw_assistant.llm import get_llm

    get_llm.cache_clear()
    generate(force=True)
    kb.ensure_index()
    return db


@pytest.fixture()
def fresh_db(synthetic_db):
    """Dla testów, które zmieniają dane (zdarzenia, akcje): świeże dane przed testem."""
    from baw_assistant.data_generator import generate

    generate(force=True)
    yield synthetic_db
    generate(force=True)
