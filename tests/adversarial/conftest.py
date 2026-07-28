"""Shared fixtures for the QA adversarial suite.

The DB-backed invariants (hero queries, provenance, live vector index) need a *seeded* Neo4j.
`scripts/qa_check.sh` brings that up, applies the DDL, seeds, and exports the connection env.

For an ad-hoc `uv run pytest` with no database (the offline dev / CI path — the same contract as
`make ci`), these tests must not fail or hang: the `graph` fixture connects with a short timeout
and `pytest.skip`s when Neo4j is unreachable, and skips again if the DB is reachable but the seed
anchor (both demo funds) is absent. This keeps the offline suite green while still executing the
live invariants for real under `qa_check.sh`.

Scope note: this conftest lives under tests/adversarial/ so it only affects the QA suite and never
the frozen offline tests in tests/.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

# Anchors that prove the shared demo seed is present (not just an empty schema).
_SEED_ANCHOR_FUNDS = (
    "Demo Growth Fund",
    "Demo Focused Growth Fund",
)


class GraphProbe:
    """Tiny read helper around a live neo4j session (QA never writes to the graph)."""

    def __init__(self, driver: Any) -> None:
        self._driver = driver

    def run(self, cypher: str, **params: Any) -> list[dict]:
        with self._driver.session() as sess:
            return [r.data() for r in sess.run(cypher, **params)]

    def value(self, cypher: str, **params: Any) -> Any:
        """Return the single scalar of the single row (fails loudly if shape is wrong)."""
        rows = self.run(cypher, **params)
        assert len(rows) == 1, f"expected exactly 1 row, got {len(rows)}: {cypher}"
        row = rows[0]
        assert len(row) == 1, f"expected exactly 1 column, got {list(row)}: {cypher}"
        return next(iter(row.values()))


@pytest.fixture(scope="session")
def _driver() -> Iterator[Any]:
    """Session-scoped neo4j driver, or skip the whole DB-backed suite if unreachable."""
    try:
        from neo4j import GraphDatabase
        from neo4j.exceptions import Neo4jError, ServiceUnavailable
    except Exception as exc:  # pragma: no cover - neo4j is a hard dep, defensive only
        pytest.skip(f"neo4j driver unavailable: {exc}")

    from api.config import get_settings

    settings = get_settings()
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
        connection_timeout=3.0,
    )
    try:
        driver.verify_connectivity()
    except (ServiceUnavailable, Neo4jError, OSError) as exc:
        driver.close()
        pytest.skip(
            f"Neo4j unreachable at {settings.neo4j_uri} ({exc}); "
            "run scripts/qa_check.sh (or `make up apply-ddl seed`) to exercise DB invariants."
        )
    yield driver
    driver.close()


@pytest.fixture()
def neo4j_store(_driver: Any) -> Any:
    """A connected `Neo4jStore` (reachable DB only; no seed required).

    Used by the ontology feasibility check (DDL applies + vector index ONLINE). Reuses the
    session driver's connectivity gate, so it skips exactly when the DB is down.
    """
    from api.config import get_settings
    from api.stores.neo4j import Neo4jStore

    return Neo4jStore(get_settings())


@pytest.fixture()
def graph(_driver: Any) -> GraphProbe:
    """A GraphProbe against the seeded demo graph, or skip if the seed is absent."""
    probe = GraphProbe(_driver)
    present = probe.value(
        "MATCH (f:Fund) WHERE f.name IN $names RETURN count(f) AS n",
        names=list(_SEED_ANCHOR_FUNDS),
    )
    if present != len(_SEED_ANCHOR_FUNDS):
        pytest.skip(
            "Neo4j reachable but demo seed absent "
            f"(found {present}/{len(_SEED_ANCHOR_FUNDS)} anchor funds); run `make seed`."
        )
    return probe
