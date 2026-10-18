"""Wspólna konfiguracja testów: osobna baza z danymi syntetycznymi i tryb mock (bez LLM)."""
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
    os.environ["DB_PATH"] = str(db)
    os.environ["LLM_MODEL"] = "mock"
    os.environ["LANGFUSE_ENABLED"] = "false"
    os.environ["SIM_NOW"] = "2026-10-18T10:45:00"
    os.environ["DATA_SEED"] = "42"
    from baw_assistant.data_generator import generate
    from baw_assistant.llm import get_llm

    get_llm.cache_clear()
    generate(force=True)
    return db
