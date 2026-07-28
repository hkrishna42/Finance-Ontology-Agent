#!/usr/bin/env python
"""Apply the ontology-derived Neo4j DDL and verify the vector index reaches ONLINE.

Usage:  uv run python scripts/apply_ddl.py

Reads connection settings from the environment / .env (see api.config.Settings). Idempotent — all
statements use IF NOT EXISTS. Exits non-zero if the `chunk_embedding` vector index never comes
ONLINE (the key Neo4j-5.26-CE feasibility check).
"""

from __future__ import annotations

import sys

from api.config import get_settings
from api.stores.neo4j import VECTOR_INDEX_NAME, Neo4jStore


def main() -> int:
    settings = get_settings()
    store = Neo4jStore(settings)
    try:
        store.verify_connectivity()
        stmts = store.apply_ddl()
        print(f"Applied {len(stmts)} DDL statement(s) to {settings.neo4j_uri}:")
        for s in stmts:
            print(f"  - {s.split(' IF ')[0]}")
        state = store.wait_for_index_online(VECTOR_INDEX_NAME, timeout=60)
        print(f"\nVector index '{VECTOR_INDEX_NAME}' state: {state.get('state')}")
        if state.get("state") != "ONLINE":
            print("ERROR: vector index did not reach ONLINE", file=sys.stderr)
            return 1
        print("OK: graph schema applied and vector index ONLINE.")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
