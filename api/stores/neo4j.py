"""Thin Neo4j driver wrapper.

Owns exactly three responsibilities for M0:
  * connect from `Settings`,
  * `apply_ddl()` — run the ontology-derived DDL (`schema.neo4j_ddl(VECTOR_DIM)`): uniqueness
    constraints + the `:Chunk` vector index (dim 384) + the fulltext index,
  * `verify_vector_index()` / `wait_for_index_online()` — confirm the vector index reaches ONLINE
    on Neo4j 5.26 CE (the key feasibility check Gate 0 must prove).

The `neo4j` driver is imported lazily so importing this module never requires a running database
(CI stays offline). `python -m api.stores.neo4j --apply-ddl` applies the DDL and verifies.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import TYPE_CHECKING, Any

from ..ontology.schema import VECTOR_DIM, neo4j_ddl

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings

VECTOR_INDEX_NAME = "chunk_embedding"


class Neo4jStore:
    """Minimal connection + DDL + index-verification helper."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        uri: str | None = None,
        user: str | None = None,
        password: str | None = None,
    ) -> None:
        if settings is not None:
            self.uri = uri or settings.neo4j_uri
            self.user = user or settings.neo4j_user
            self.password = password or settings.neo4j_password
        else:
            if not (uri and user and password):
                raise ValueError("Neo4jStore needs either settings or uri/user/password")
            self.uri, self.user, self.password = uri, user, password
        self._driver: Any = None

    @property
    def driver(self) -> Any:
        if self._driver is None:
            from neo4j import GraphDatabase  # lazy: keep module import offline

            self._driver = GraphDatabase.driver(self.uri, auth=(self.user, self.password))
        return self._driver

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def __enter__(self) -> Neo4jStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def verify_connectivity(self) -> None:
        self.driver.verify_connectivity()

    def run(self, query: str, **params: Any) -> list[dict]:
        """Execute a Cypher statement and return rows as dicts (read helper / DDL runner)."""
        with self.driver.session() as session:
            result = session.run(query, **params)
            return [record.data() for record in result]

    def apply_ddl(self, dim: int = VECTOR_DIM) -> list[str]:
        """Apply every ontology-derived DDL statement. Idempotent (all use IF NOT EXISTS)."""
        stmts = neo4j_ddl(dim)
        for stmt in stmts:
            self.run(stmt)
        return stmts

    def verify_vector_index(self, name: str = VECTOR_INDEX_NAME) -> dict[str, Any]:
        """Return `{name, type, state, ...}` for the vector index (or `{state: MISSING}`)."""
        rows = self.run(
            "SHOW INDEXES YIELD name, type, state, entityType, labelsOrTypes, properties "
            "WHERE name = $name RETURN name, type, state, entityType, labelsOrTypes, properties",
            name=name,
        )
        if not rows:
            return {"name": name, "state": "MISSING"}
        return rows[0]

    def wait_for_index_online(
        self, name: str = VECTOR_INDEX_NAME, timeout: float = 30.0, interval: float = 1.0
    ) -> dict[str, Any]:
        """Poll until the index is ONLINE or the timeout elapses; returns the last state."""
        deadline = time.time() + timeout
        state = self.verify_vector_index(name)
        while state.get("state") != "ONLINE" and time.time() < deadline:
            time.sleep(interval)
            state = self.verify_vector_index(name)
        return state


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Neo4j DDL / vector-index utility")
    parser.add_argument("--apply-ddl", action="store_true", help="apply the ontology DDL")
    parser.add_argument("--verify", action="store_true", help="verify the vector index is ONLINE")
    args = parser.parse_args(argv)

    from ..config import get_settings

    store = Neo4jStore(get_settings())
    try:
        if args.apply_ddl:
            stmts = store.apply_ddl()
            print(f"applied {len(stmts)} DDL statement(s):")
            for s in stmts:
                print(f"  - {s[:80]}")
        if args.apply_ddl or args.verify:
            state = store.wait_for_index_online()
            print(f"vector index '{VECTOR_INDEX_NAME}' state: {state.get('state')}")
            if state.get("state") != "ONLINE":
                print("ERROR: vector index did not reach ONLINE", file=sys.stderr)
                return 1
    finally:
        store.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
