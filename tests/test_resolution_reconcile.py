"""Resolution-queue reconcile — the graph-repoint merge + reject/promote routes (offline).

`repoint_entity` is exercised against a recording fake store (no Neo4j); the `/resolve/*` routes run
over an in-memory SQLite queue with the repoint store injected via the `_REPOINT_STORE` seam.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.resolution import routes as resolve_routes
from api.resolution import store as queue_store
from api.resolution.graph_reconcile import repoint_entity
from api.resolution.routes import get_conn, router


class RecordingStore:
    """Fake Neo4j store: records every query and returns canned rows by query shape."""

    def __init__(self, rel_types: list[str]):
        self.rel_types = rel_types
        self.queries: list[tuple[str, dict]] = []

    def run(self, query: str, **params):
        self.queries.append((query, params))
        if "collect(DISTINCT type(r))" in query:
            return [{"types": list(self.rel_types)}]
        if "RETURN properties(d) AS p" in query:
            return [{"p": {"name": params.get("dup"), "fibo_class": "cmns-org:LegalEntity"}}]
        if "DETACH DELETE" in query:
            return [{"n": 1}]
        if "RETURN count(*) AS n" in query:
            return [{"n": 1}]
        return []

    def ran(self, needle: str) -> bool:
        return any(needle in q for q, _ in self.queries)


# -- repoint_entity (unit) --------------------------------------------------------------------


def test_repoint_moves_edges_and_deletes_duplicate():
    store = RecordingStore(rel_types=["MENTIONS", "SUPPLIES_TO"])
    result = repoint_entity(
        store, label="Company", dup_key="Taiwan Semi", canonical_key="Taiwan Semiconductor"
    )
    assert result["rel_types"] == ["MENTIONS", "SUPPLIES_TO"]
    assert result["moved"] == 4  # 2 types × (1 outgoing + 1 incoming)
    assert result["dup_deleted"] is True
    # The canonical node is ensured, MENTIONS is repointed, and the duplicate is detached-deleted.
    assert store.ran("MERGE (n:Company {name: $k})")
    assert store.ran("MERGE (c)-[nr:MENTIONS]->(x)")
    assert store.ran("DETACH DELETE d")


def test_repoint_ignores_unknown_relationship_types():
    # A type not in the ontology vocabulary is never interpolated into a query (injection guard).
    store = RecordingStore(rel_types=["MENTIONS", "DROP_TABLE"])
    result = repoint_entity(store, label="Company", dup_key="Dup Co", canonical_key="Canon Co")
    assert result["rel_types"] == ["MENTIONS"]
    assert not store.ran("DROP_TABLE")


def test_repoint_is_noop_when_keys_match():
    store = RecordingStore(rel_types=["MENTIONS"])
    result = repoint_entity(store, label="Company", dup_key="Same", canonical_key="Same")
    assert result == {"moved": 0, "dup_deleted": False, "canonical_key": "Same", "rel_types": []}
    assert store.queries == []


def test_repoint_rejects_unknown_label():
    store = RecordingStore(rel_types=[])
    with pytest.raises(ValueError, match="unknown entity label"):
        repoint_entity(store, label="Sneaky`(){}", dup_key="a", canonical_key="b")


def test_repoint_stamps_extra_props_on_canonical():
    store = RecordingStore(rel_types=[])
    repoint_entity(
        store,
        label="Company",
        dup_key="Acme Corp",
        canonical_key="Acme Corporation",
        extra_props={"cik": "0000000123", "lei": "LEI123"},
    )
    assert any("SET n += $props" in q and p.get("props", {}).get("cik") == "0000000123"
               for q, p in store.queries)


# -- /resolve/merge · /promote · /reject (routes) ---------------------------------------------


@pytest.fixture()
def client():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    queue_store.init_resolution_db(conn)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_conn] = lambda: conn
    yield TestClient(app), conn
    conn.close()


def _enqueue(conn) -> int:
    return queue_store.enqueue(
        conn,
        mention="Taiwan Semi",
        normalized="taiwan semi",
        label="Company",
        candidates=[{"existing_id": "Taiwan Semiconductor", "title": "Taiwan Semiconductor",
                     "score": 0.93}],
    )


def test_merge_with_canonical_key_repoints_graph(client):
    tc, conn = client
    qid = _enqueue(conn)
    store = RecordingStore(rel_types=["MENTIONS", "SUPPLIES_TO"])
    resolve_routes._REPOINT_STORE = store
    try:
        body = tc.post(
            "/resolve/merge",
            json={"queue_id": qid, "canonical_key": "Taiwan Semiconductor",
                  "canonical_label": "Company"},
        ).json()
    finally:
        resolve_routes._REPOINT_STORE = None

    assert body["merged"]["status"] == "merged"
    assert body["graph"]["dup_deleted"] is True
    assert body["graph"]["moved"] == 4
    assert store.ran("DETACH DELETE d")  # the repoint Cypher actually ran
    # No longer provisional; kept in the full queue for audit.
    assert tc.get("/resolve/provisional").json()["count"] == 0
    assert tc.get("/resolve/queue").json()["count"] == 1


def test_merge_without_canonical_key_is_sqlite_only(client):
    tc, conn = client
    qid = _enqueue(conn)
    body = tc.post("/resolve/merge", json={"queue_id": qid, "cik": "0000000001"}).json()
    assert body["merged"]["status"] == "merged"
    assert body["merged"]["candidate_cik"] == "0000000001"
    assert body["graph"] is None  # no canonical_key → graph untouched


def test_promote_keeps_mention_as_new_node(client):
    tc, conn = client
    qid = _enqueue(conn)
    body = tc.post("/resolve/promote", json={"queue_id": qid}).json()
    assert body["promoted"]["status"] == "promoted"
    # UI maps 'promoted' → 'kept_new'.
    assert tc.get("/resolve").json()[0]["status"] in {"kept_new"} or \
        tc.get("/resolve/provisional").json()["count"] == 0


def test_reject_marks_rejected(client):
    tc, conn = client
    qid = _enqueue(conn)
    body = tc.post("/resolve/reject", json={"queue_id": qid}).json()
    assert body["rejected"]["status"] == "rejected"
    assert tc.get("/resolve/provisional").json()["count"] == 0


@pytest.mark.parametrize("path", ["/resolve/merge", "/resolve/promote", "/resolve/reject"])
def test_actions_on_unknown_id_404(client, path):
    tc, _conn = client
    assert tc.post(path, json={"queue_id": 999}).status_code == 404
