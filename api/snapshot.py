"""Committed graph snapshot — generate offline + restore-on-empty.

The stub demo graph is the seed subgraph (``fixtures/seed_graph.cypher``) plus deterministic
``HashEmbedder`` ``:Chunk`` embeddings. ``make snapshot`` freezes both into ``corpus/snapshot/``
(committed to git); the ``api`` container restores them on first boot when the graph is empty —
instant, no extraction, no API key.

Snapshot layout (``corpus/snapshot/``)::

    seed_graph.cypher   verbatim copy of fixtures/seed_graph.cypher (the graph structure)
    embeddings.json     {chunk_id: [384 floats]} produced by HashEmbedder (the vectors)
    manifest.json       {embed_backend, vector_dim, chunk_count, statement_count, cypher_sha256}

**Embedder / snapshot consistency** (the query-time gotcha): vector search compares a query
embedding against the ``:Chunk`` embeddings, so both must come from the *same* embedder.

  * ``stub``: restore loads the committed HashEmbedder vectors verbatim **and** queries embed with
    HashEmbedder (``EMBED_BACKEND=hash``) — self-consistent, offline, no download.
  * ``full``: the frozen hash vectors are ignored; the same chunk text is re-embedded with
    fastembed (``EMBED_BACKEND=fastembed``) so chunk and query vectors both come from fastembed.

``tests/test_snapshot.py`` asserts every committed vector has dim == ``VECTOR_DIM`` (384) and that
the *current* ``HashEmbedder`` still reproduces the committed vectors — a drift alarm that fires if
``HashEmbedder`` changes without ``make snapshot`` being re-run (which would silently desync stub
query embeddings from the restored chunk vectors).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .ontology.schema import VECTOR_DIM, neo4j_ddl

if TYPE_CHECKING:  # pragma: no cover
    from .config import Settings

# ---- paths -------------------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_CYPHER = REPO_ROOT / "fixtures" / "seed_graph.cypher"
SNAPSHOT_DIR = REPO_ROOT / "corpus" / "snapshot"
SNAPSHOT_CYPHER = SNAPSHOT_DIR / "seed_graph.cypher"
EMBEDDINGS_JSON = SNAPSHOT_DIR / "embeddings.json"
MANIFEST_JSON = SNAPSHOT_DIR / "manifest.json"

_CHUNK_ID_RE = re.compile(r"chunk_id:\s*'([^']+)'")
_CHUNK_TEXT_RE = re.compile(r"\.text\s*=\s*'([^']*)'")


# ---- pure helpers (no DB) ----------------------------------------------------------------------
def split_statements(text: str) -> list[str]:
    """Split a Cypher script into executable statements.

    Strips full-line ``//`` comments first (they may contain ``;`` or quotes), then splits on
    ``;``. No ``;`` appears inside string literals in the seed fixture, so the split is safe.
    Mirrors ``scripts/load_seed.py`` so the two never diverge.
    """
    code = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("//"))
    return [s.strip() for s in code.split(";") if s.strip()]


def parse_chunk_texts(cypher: str) -> dict[str, str]:
    """Return ``{chunk_id: text}`` for every ``:Chunk`` statement that sets ``.text``."""
    out: dict[str, str] = {}
    for stmt in split_statements(cypher):
        if ":Chunk" not in stmt or ".text" not in stmt:
            continue
        cid = _CHUNK_ID_RE.search(stmt)
        txt = _CHUNK_TEXT_RE.search(stmt)
        if cid and txt:
            out[cid.group(1)] = txt.group(1)
    return out


def load_embeddings() -> dict[str, list[float]]:
    """Load the committed ``{chunk_id: vector}`` map (raises if the snapshot is absent)."""
    return json.loads(EMBEDDINGS_JSON.read_text())


def load_manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_JSON.read_text())


# ---- generate (offline; `make snapshot`) -------------------------------------------------------
def write_snapshot() -> dict[str, Any]:
    """Freeze the seed graph + deterministic HashEmbedder vectors into ``corpus/snapshot/``.

    Fully offline — no Neo4j, no network, no model download. Deterministic, so re-running produces
    a byte-identical snapshot (empty ``git diff``) unless the seed or the embedder changed.
    """
    from .providers.embeddings import HashEmbedder  # local import keeps module import cheap

    cypher = SEED_CYPHER.read_text()
    chunks = parse_chunk_texts(cypher)
    if not chunks:
        raise RuntimeError(f"no :Chunk texts parsed from {SEED_CYPHER}")

    embedder = HashEmbedder(dim=VECTOR_DIM)
    ids = sorted(chunks)  # stable order → stable JSON
    vectors = embedder.embed([chunks[c] for c in ids])
    embeddings = {cid: [float(x) for x in vec] for cid, vec in zip(ids, vectors, strict=True)}

    bad = {cid: len(v) for cid, v in embeddings.items() if len(v) != VECTOR_DIM}
    if bad:
        raise RuntimeError(f"embedding dim mismatch (expected {VECTOR_DIM}): {bad}")

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_CYPHER.write_text(cypher)
    EMBEDDINGS_JSON.write_text(json.dumps(embeddings, indent=1, sort_keys=True) + "\n")

    manifest = {
        "source": "fixtures/seed_graph.cypher",
        "embed_backend": "hash",
        "embed_model_note": "HashEmbedder (deterministic offline stub); full mode re-embeds with fastembed",  # noqa: E501
        "vector_dim": VECTOR_DIM,
        "chunk_count": len(embeddings),
        "statement_count": len(split_statements(cypher)),
        "cypher_sha256": hashlib.sha256(cypher.encode("utf-8")).hexdigest(),
    }
    MANIFEST_JSON.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


# ---- restore-on-empty (in-container boot; `python -m api.snapshot --restore`) -------------------
def graph_is_empty(driver: Any) -> bool:
    with driver.session() as sess:
        return (sess.run("MATCH (n) RETURN count(n) AS c").single()["c"]) == 0


def restore_if_empty(settings: Settings, *, force: bool = False) -> dict[str, Any]:
    """Apply the DDL and, if the graph is empty (or ``force``), load the committed snapshot.

    Idempotent: a populated graph is left untouched (returns ``{"loaded": False}``) so re-running
    ``bootstrap`` never double-loads or clobbers a real full-mode extraction graph.

    Embeddings: ``stub``/``hash`` loads the frozen vectors verbatim; any other backend (``full`` →
    ``fastembed``) re-embeds the chunk text with ``get_embedder(settings)`` so chunk and query
    vectors share one embedder.
    """
    from neo4j import GraphDatabase

    if not SNAPSHOT_CYPHER.exists():
        raise FileNotFoundError(
            f"{SNAPSHOT_CYPHER} missing — run `make snapshot` (or `python -m api.snapshot --write`)"
        )

    driver = GraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    try:
        # DDL first (constraints + the dim-384 :Chunk vector index). Idempotent (IF NOT EXISTS).
        with driver.session() as sess:
            for stmt in neo4j_ddl(settings.vector_dim):
                sess.run(stmt)

        if not force and not graph_is_empty(driver):
            counts = _counts(driver)
            return {"loaded": False, "reason": "graph not empty", **counts}

        cypher = SNAPSHOT_CYPHER.read_text()
        stmts = split_statements(cypher)
        with driver.session() as sess:
            for i, stmt in enumerate(stmts):
                try:
                    sess.run(stmt)
                except Exception as exc:  # fail loudly rather than leave a half-loaded graph
                    raise RuntimeError(f"snapshot statement #{i} failed:\n{stmt}\n-> {exc}") from exc

            n_embedded = _set_chunk_embeddings(sess, settings)

        counts = _counts(driver)
        return {"loaded": True, "chunks_embedded": n_embedded, "backend": settings.embed_backend, **counts}
    finally:
        driver.close()


def _set_chunk_embeddings(sess: Any, settings: Settings) -> int:
    """Populate ``:Chunk.embedding``. Hash backend → committed vectors; else recompute."""
    rows = sess.run(
        "MATCH (c:Chunk) WHERE c.text IS NOT NULL RETURN c.chunk_id AS id, c.text AS text"
    ).data()
    if not rows:
        return 0

    if settings.embed_backend == "hash" and EMBEDDINGS_JSON.exists():
        committed = load_embeddings()
        vectors = {r["id"]: committed[r["id"]] for r in rows if r["id"] in committed}
        # Any chunk missing from the frozen map (e.g. seed drift) falls back to a live hash embed.
        missing = [r for r in rows if r["id"] not in committed]
        if missing:
            from .providers.embeddings import HashEmbedder

            for r, vec in zip(missing, HashEmbedder(dim=settings.vector_dim).embed(
                [r["text"] for r in missing]), strict=True):
                vectors[r["id"]] = [float(x) for x in vec]
    else:
        from .providers.embeddings import get_embedder

        embedder = get_embedder(settings)
        vectors = {
            r["id"]: [float(x) for x in vec]
            for r, vec in zip(rows, embedder.embed([r["text"] for r in rows]), strict=True)
        }

    for cid, vec in vectors.items():
        sess.run("MATCH (c:Chunk {chunk_id: $id}) SET c.embedding = $v", id=cid, v=vec)
    return len(vectors)


def _counts(driver: Any) -> dict[str, int]:
    with driver.session() as sess:
        nodes = sess.run("MATCH (n) RETURN count(n) AS c").single()["c"]
        rels = sess.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
    return {"nodes": nodes, "relationships": rels}


# ---- CLI ---------------------------------------------------------------------------------------
def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Graph snapshot: generate (offline) / restore.")
    parser.add_argument("--write", action="store_true", help="(re)generate corpus/snapshot/ offline")
    parser.add_argument("--restore", action="store_true", help="restore into Neo4j if graph empty")
    parser.add_argument("--force", action="store_true", help="restore even if the graph is non-empty")
    args = parser.parse_args(argv)

    if args.write:
        m = write_snapshot()
        print(
            f"snapshot written -> {SNAPSHOT_DIR.relative_to(REPO_ROOT)}/ "
            f"({m['chunk_count']} chunks, dim {m['vector_dim']}, {m['statement_count']} statements)"
        )
    if args.restore:
        from .config import get_settings

        res = restore_if_empty(get_settings(), force=args.force)
        if res["loaded"]:
            print(
                f"snapshot restored: {res['nodes']} nodes, {res['relationships']} relationships, "
                f"{res['chunks_embedded']} chunks embedded ({res['backend']})."
            )
        else:
            print(
                f"snapshot skipped ({res.get('reason')}): graph already has "
                f"{res['nodes']} nodes, {res['relationships']} relationships."
            )
    if not (args.write or args.restore):
        parser.error("nothing to do: pass --write and/or --restore")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
