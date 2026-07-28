"""Committed graph snapshot — offline invariants (no Neo4j needed).

Guards the two properties the containerized stub boot depends on:
  1. every committed :Chunk vector has dim == VECTOR_DIM (384), matching the Neo4j vector index;
  2. the *current* HashEmbedder still reproduces the committed vectors — the embedder/snapshot
     consistency drift alarm. Stub restore loads these frozen vectors as the chunk embeddings while
     queries embed with HashEmbedder, so if HashEmbedder drifts without `make snapshot` being
     re-run, stub vector search would silently desync. This test fails first.
"""

from __future__ import annotations

import math

import pytest

from api import snapshot
from api.ontology.schema import VECTOR_DIM
from api.providers.embeddings import HashEmbedder

pytestmark = pytest.mark.skipif(
    not snapshot.EMBEDDINGS_JSON.exists(),
    reason="snapshot not generated — run `make snapshot`",
)


def test_manifest_declares_hash_backend_and_dim_384():
    m = snapshot.load_manifest()
    assert m["embed_backend"] == "hash"
    assert m["vector_dim"] == VECTOR_DIM == 384


def test_every_committed_vector_has_dim_384():
    emb = snapshot.load_embeddings()
    assert emb, "snapshot embeddings.json is empty"
    for chunk_id, vec in emb.items():
        assert len(vec) == VECTOR_DIM == 384, f"{chunk_id}: dim {len(vec)} != {VECTOR_DIM}"


def test_snapshot_covers_every_seed_chunk():
    chunks = snapshot.parse_chunk_texts(snapshot.SNAPSHOT_CYPHER.read_text())
    emb = snapshot.load_embeddings()
    assert set(emb) == set(chunks), "embeddings.json and seed :Chunk ids disagree"


def test_current_hash_embedder_reproduces_committed_vectors():
    """The stub embedder matches the snapshot (drift alarm): HashEmbedder(text) == frozen vector."""
    chunks = snapshot.parse_chunk_texts(snapshot.SNAPSHOT_CYPHER.read_text())
    committed = snapshot.load_embeddings()
    embedder = HashEmbedder(dim=VECTOR_DIM)
    for chunk_id, text in chunks.items():
        live = embedder.embed([text])[0]
        frozen = committed[chunk_id]
        assert len(live) == len(frozen) == VECTOR_DIM
        for a, b in zip(live, frozen, strict=True):
            assert math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12), (
                f"{chunk_id}: HashEmbedder drifted from the committed snapshot — re-run "
                f"`make snapshot` and commit corpus/snapshot/."
            )


def test_snapshot_cypher_matches_manifest_checksum():
    import hashlib

    m = snapshot.load_manifest()
    actual = hashlib.sha256(snapshot.SNAPSHOT_CYPHER.read_bytes()).hexdigest()
    assert actual == m["cypher_sha256"], "snapshot cypher changed without a manifest refresh"
