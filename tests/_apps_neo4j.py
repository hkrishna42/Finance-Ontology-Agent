"""Shared Neo4j availability probe for the Apps workstream's integration tests.

`neo4j_store_or_skip()` returns a connected `Neo4jStore` (reading NEO4J_URI/creds from the
environment via Settings) or calls `pytest.skip` when no seeded database is reachable. This keeps
the offline CI gate green (no Neo4j) while the same tests validate the real Cypher when a seeded
graph is up (e.g. NEO4J_URI=bolt://localhost:7688). Not named `test_*`, so not collected.
"""

from __future__ import annotations

import pytest

from api.config import Settings
from api.stores.neo4j import Neo4jStore


def neo4j_store_or_skip() -> Neo4jStore:
    settings = Settings()  # fresh read of env (not the lru_cached get_settings)
    store = Neo4jStore(settings)
    try:
        store.verify_connectivity()
    except Exception as exc:  # noqa: BLE001 - any driver/connection error => skip offline
        store.close()
        pytest.skip(f"Neo4j not reachable at {settings.neo4j_uri}: {exc}")
    rows = store.run("MATCH (f:Fund) RETURN count(f) AS n")
    if not rows or rows[0]["n"] == 0:
        store.close()
        pytest.skip("Neo4j reachable but not seeded (no :Fund nodes); run `make seed`")
    return store
