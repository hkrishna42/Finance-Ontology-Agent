"""Embedding tests: HashEmbedder shape/determinism and the offline-by-default factory."""

from __future__ import annotations

import math

from api.config import Settings
from api.providers.base import EmbeddingProvider
from api.providers.embeddings import HashEmbedder, get_embedder


def test_hash_embedder_dim_and_unit_norm():
    emb = HashEmbedder()
    vecs = emb.embed(["NVIDIA depends on TSMC for advanced packaging."])
    assert emb.dim == 384
    assert len(vecs) == 1
    assert len(vecs[0]) == 384
    norm = math.sqrt(sum(x * x for x in vecs[0]))
    assert math.isclose(norm, 1.0, rel_tol=1e-6)


def test_hash_embedder_is_deterministic():
    emb = HashEmbedder()
    a = emb.embed(["supply chain risk"])[0]
    b = emb.embed(["supply chain risk"])[0]
    assert a == b
    c = emb.embed(["a different sentence entirely"])[0]
    assert a != c


def test_hash_embedder_handles_empty_text():
    emb = HashEmbedder()
    v = emb.embed([""])[0]
    assert len(v) == 384
    assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, rel_tol=1e-6)


def test_hash_embedder_is_embedding_provider():
    assert isinstance(HashEmbedder(), EmbeddingProvider)


def test_get_embedder_defaults_to_hash():
    emb = get_embedder(Settings())
    assert isinstance(emb, HashEmbedder)
    assert emb.dim == 384
