"""Baza wiedzy w Qdrant: opisy procesów i instrukcje rozwiązywania incydentów.

- Dokumenty Markdown z katalogu knowledge/ są dzielone na sekcje (## …) i indeksowane w Qdrant.
- Każda sekcja ma metadane (rodzaj, proces, odbiorcy), a wyszukiwanie filtruje po nich w trakcie
  wyszukiwania wektorowego – np. instrukcje dla administratora nie trafią do odpowiedzi dla pracownika.
- Embeddingi: model OpenAI przez LangChain (gdy dostępny) albo lokalny embedding haszujący
  (tryb offline / testy). Każdy tryb ma własną kolekcję, bo wektory mają różne wymiary.
- QDRANT_URL=":memory:" uruchamia Qdrant w procesie (bez serwera) – do testów i pracy lokalnej.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import unicodedata
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .config import get_settings

log = logging.getLogger(__name__)

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"
HASH_DIM = 512
NAMESPACE = uuid.UUID("6f1c2b4e-8f7a-4d1e-9b9e-2a6c7d8e9f10")


# --- dokumenty ---------------------------------------------------------------------

def load_chunks() -> list[dict[str, Any]]:
    chunks = []
    for path in sorted(KNOWLEDGE_DIR.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        _, front, body = raw.split("---", 2)
        meta = yaml.safe_load(front)
        for section in re.split(r"^## ", body, flags=re.MULTILINE)[1:]:
            heading, _, text = section.partition("\n")
            text = " ".join(text.split())
            chunks.append({"doc": path.stem, "title": meta["title"], "section": heading.strip(), "text": text,
                           "kind": meta["kind"], "process_id": meta["process_id"],
                           "audience": meta["audience"]})
    return chunks


# --- embeddingi ----------------------------------------------------------------------

def embedding_mode() -> str:
    mode = os.getenv("EMBEDDING_MODEL", "auto")
    if mode == "auto":
        return "hash" if get_settings().mock_llm or not os.getenv("OPENAI_API_KEY") else "openai"
    return "hash" if mode == "hash" else "openai"


def _tokens(text: str) -> list[str]:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c)).replace("ł", "l")
    words = re.findall(r"[a-z0-9]+", text)
    # Prosty „stemming”: pierwsze 5 znaków słowa łagodzi odmianę przez przypadki.
    return [w[:5] for w in words if len(w) > 2]


def _hash_embed(text: str) -> list[float]:
    vec = [0.0] * HASH_DIM
    toks = _tokens(text)
    for tok in toks + [a + "_" + b for a, b in zip(toks, toks[1:])]:
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
        vec[h % HASH_DIM] += 1.0 if (h >> 8) % 2 else -1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


@lru_cache(maxsize=1)
def _openai_embedder():
    from langchain_openai import OpenAIEmbeddings

    return OpenAIEmbeddings(model=os.getenv("EMBEDDING_OPENAI_MODEL", "text-embedding-3-small"))


def embed(texts: list[str]) -> list[list[float]]:
    if embedding_mode() == "openai":
        return _openai_embedder().embed_documents(texts)
    return [_hash_embed(t) for t in texts]


def collection_name() -> str:
    return f"baw_knowledge_{embedding_mode()}"


# --- Qdrant --------------------------------------------------------------------------

@lru_cache(maxsize=1)
def client():
    from qdrant_client import QdrantClient

    url = get_settings().qdrant_url
    if url in (":memory:", "memory"):
        return QdrantClient(":memory:")
    if url.startswith("local:"):
        return QdrantClient(path=url.removeprefix("local:"))
    return QdrantClient(url=url, timeout=5)


def ensure_index() -> int:
    """Tworzy kolekcję (jeśli brak) i wgrywa sekcje dokumentów. Idempotentne: stałe identyfikatory punktów."""
    from qdrant_client import models

    chunks = load_chunks()
    qc = client()
    name = collection_name()
    vectors = embed([f"{c['title']} – {c['section']}: {c['text']}" for c in chunks])
    if not qc.collection_exists(name):
        qc.create_collection(name, vectors_config=models.VectorParams(size=len(vectors[0]),
                                                                       distance=models.Distance.COSINE))
        if not str(get_settings().qdrant_url).startswith((":memory:", "memory", "local:")):
            for field in ("kind", "process_id", "audience"):  # indeksy pól filtrów (tylko serwer Qdrant)
                qc.create_payload_index(name, field, models.PayloadSchemaType.KEYWORD)
    points = [models.PointStruct(id=str(uuid.uuid5(NAMESPACE, f"{c['doc']}#{c['section']}")), vector=v, payload=c)
              for c, v in zip(chunks, vectors)]
    qc.upsert(name, points=points)
    return len(points)


def search(query: str, *, kind: str | None = None, process_id: str | None = None, role: str | None = None,
           limit: int = 2, min_score: float | None = None) -> list[dict[str, Any]]:
    """Wyszukiwanie semantyczne z filtrem metadanych. Błąd Qdrant = pusta lista (agent działa dalej)."""
    from qdrant_client import models

    try:
        must = []
        if kind:
            must.append(models.FieldCondition(key="kind", match=models.MatchValue(value=kind)))
        if process_id:
            must.append(models.FieldCondition(key="process_id", match=models.MatchAny(any=[process_id, "ALL"])))
        if role:
            must.append(models.FieldCondition(key="audience", match=models.MatchValue(value=role)))
        threshold = min_score if min_score is not None else (0.12 if embedding_mode() == "hash" else 0.3)
        result = client().query_points(collection_name(), query=embed([query])[0],
                                       query_filter=models.Filter(must=must) if must else None,
                                       limit=limit, with_payload=True, score_threshold=threshold)
        return [{**p.payload, "score": round(p.score, 3)} for p in result.points]
    except Exception as exc:
        log.warning("Baza wiedzy niedostępna: %s", exc)
        return []


def doc_section(doc: str, section: str) -> str | None:
    """Treść konkretnej sekcji dokumentu (np. „Działania” z instrukcji znalezionej przez wyszukiwanie)."""
    return next((c["text"] for c in load_chunks() if c["doc"] == doc and c["section"] == section), None)


def short(text: str, limit: int = 240) -> str:
    """Pierwsze zdania sekcji, żeby fakt był krótki."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    return cut[: cut.rfind(".") + 1] if "." in cut else cut + "…"
