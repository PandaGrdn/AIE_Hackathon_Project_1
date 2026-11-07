"""Embeddings for reasoning steps, subtask specs and lessons. Owner: C.

Default: Gemini embeddings. Fallback: sentence-transformers (pip install .[local-embeddings]).
Tests use `fake_embed` so they never hit the network.
"""

from __future__ import annotations

import hashlib

import numpy as np

from core.config import settings

_local_model = None


def fake_embed(texts: list[str], dim: int = 64) -> list[list[float]]:
    """Deterministic pseudo-embedding for tests: similar strings do NOT embed similarly."""
    out = []
    for t in texts:
        seed = int(hashlib.sha256(t.encode()).hexdigest()[:8], 16)
        rng = np.random.default_rng(seed)
        v = rng.standard_normal(dim)
        out.append((v / np.linalg.norm(v)).tolist())
    return out


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    if settings.gemini_api_key:
        from google import genai

        client = genai.Client(api_key=settings.gemini_api_key)
        resp = client.models.embed_content(model=settings.gemini_embed_model, contents=texts)
        return [e.values for e in resp.embeddings]

    global _local_model
    try:
        if _local_model is None:
            from sentence_transformers import SentenceTransformer

            _local_model = SentenceTransformer("all-MiniLM-L6-v2")
        return _local_model.encode(texts, normalize_embeddings=True).tolist()
    except ImportError:
        return fake_embed(texts)


def cosine(a: list[float], b: list[float]) -> float:
    va, vb = np.asarray(a), np.asarray(b)
    return float(va @ vb / (np.linalg.norm(va) * np.linalg.norm(vb) + 1e-12))
