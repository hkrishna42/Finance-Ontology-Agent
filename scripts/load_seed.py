"""Load fixtures/seed_graph.cypher into Neo4j, then compute + set :Chunk embeddings.

Usage:
    uv run python scripts/load_seed.py            # uses NEO4J_* + EMBED_BACKEND from env/.env
    uv run python scripts/load_seed.py --stats    # also print node/edge counts

The seed graph is the shared, ontology-consistent demo subgraph every workstream develops
against. Embeddings use get_embedder(settings) (HashEmbedder by default — offline/deterministic).
"""

from __future__ import annotations

import sys
from pathlib import Path

from neo4j import GraphDatabase

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.config import get_settings  # noqa: E402
from api.providers.embeddings import get_embedder  # noqa: E402

SEED = Path(__file__).resolve().parents[1] / "fixtures" / "seed_graph.cypher"


def _statements(text: str) -> list[str]:
    # Strip full-line // comments FIRST (they may contain ';' or quotes), THEN split on ';'.
    # No ';' appears inside cypher string literals in the fixture, so the split is then safe.
    code = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("//"))
    return [s.strip() for s in code.split(";") if s.strip()]


def main() -> int:
    settings = get_settings()
    stmts = _statements(SEED.read_text())
    driver = GraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    with driver.session() as sess:
        for i, stmt in enumerate(stmts):
            try:
                sess.run(stmt)
            except Exception as exc:  # fail loudly with context (don't leave a half-loaded graph)
                raise RuntimeError(f"seed statement #{i} failed:\n{stmt}\n-> {exc}") from exc
        # Embed chunks
        rows = sess.run("MATCH (c:Chunk) WHERE c.text IS NOT NULL RETURN c.chunk_id AS id, c.text AS text").data()
        if rows:
            embedder = get_embedder(settings)
            vectors = embedder.embed([r["text"] for r in rows])
            for r, vec in zip(rows, vectors, strict=True):
                sess.run(
                    "MATCH (c:Chunk {chunk_id: $id}) SET c.embedding = $v",
                    id=r["id"], v=[float(x) for x in vec],
                )
        counts = sess.run(
            "MATCH (n) WITH count(n) AS nodes "
            "MATCH ()-[r]->() RETURN nodes, count(r) AS rels"
        ).single()
    driver.close()
    print(f"Seed loaded: {counts['nodes']} nodes, {counts['rels']} relationships, "
          f"{len(stmts)} statements, {len(rows)} chunks embedded ({settings.embed_backend}).")
    if "--stats" in sys.argv:
        print("  funds:", 2, "| holdings edges: Growth 9 / Focused 8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
