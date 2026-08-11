"""M2 / resolution — the /resolve API (provisional queue + steward merge).

Offline: an in-memory SQLite connection is injected via dependency override; the resolve endpoint
runs the full pipeline in stub mode (FakeProvider + HashEmbedder).
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.firms.scope import DEMO_FIRM_NAME
from api.resolution import routes as resolution_routes
from api.resolution import store as queue_store
from api.resolution.routes import get_conn, router


@pytest.fixture()
def client():
    # Sync endpoints run in Starlette's threadpool, so the shared test connection must allow
    # cross-thread use (production opens one connection per request, per thread).
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    queue_store.init_resolution_db(conn)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_conn] = lambda: conn
    yield TestClient(app), conn
    conn.close()


_PROVISIONAL_KEYS = {
    "id", "label", "name", "aliases", "span", "doc_id", "chunk_id", "confidence",
    "candidates", "status",
}


def test_get_resolve_returns_provisional_entity_array_demo_when_empty(client):
    # Bug #6: bare GET /resolve → ProvisionalEntity[] (not 307/405, not {count,items}).
    tc, _conn = client
    resp = tc.get("/resolve")
    assert resp.status_code == 200
    items = resp.json()
    assert isinstance(items, list) and len(items) >= 2  # demo fixture (queue is empty)
    e = items[0]
    assert _PROVISIONAL_KEYS <= set(e)
    assert e["status"] in {"pending", "merged", "kept_new"}
    cand = e["candidates"][0]
    assert {"existing_id", "name", "label", "score"} <= set(cand)


def test_get_resolve_maps_live_queue_rows_to_provisional_entities(client):
    tc, conn = client
    queue_store.enqueue(
        conn,
        mention="Nvidia Corp.",
        normalized="nvidia",
        label="Company",
        aliases=["Nvidia Corporation"],
        span="Nvidia Corp. reported record data center revenue for the quarter",
        doc_id="nvda_8k_live",
        chunk_id="nvda_8k_live_c1",
        confidence=0.91,
        candidates=[{"cik": "0001045810", "title": "NVIDIA", "ticker": "NVDA", "score": 0.97}],
    )
    items = tc.get("/resolve").json()
    assert len(items) == 1  # live queue non-empty → mapped rows, not the demo fixture
    e = items[0]
    assert _PROVISIONAL_KEYS <= set(e)
    assert e["name"] == "Nvidia Corp."
    assert e["label"] == "Company"
    assert e["aliases"] == ["Nvidia Corporation"]
    assert e["doc_id"] == "nvda_8k_live"
    assert e["status"] == "pending"
    cand = e["candidates"][0]
    assert cand["existing_id"] == "NVIDIA"
    assert cand["score"] == 0.97


def test_get_resolve_demo_firm_keeps_demo_fixture(client):
    # The fictional demo firm stays demo scope: an empty queue still yields the committed demo fixture.
    tc, _conn = client
    items = tc.get("/resolve", params={"firm": DEMO_FIRM_NAME}).json()
    assert isinstance(items, list) and len(items) >= 2  # demo fixture (queue is empty)


def test_get_resolve_real_firm_never_returns_demo_fixture(client, monkeypatch):
    # A real firm with an EMPTY live queue must get [] — never the committed NVIDIA/TSMC demo fixture.
    tc, _conn = client
    monkeypatch.setattr(resolution_routes, "_firm_doc_ids", lambda firm: set())
    items = tc.get("/resolve", params={"firm": "Acme Global Real Estate Fund"}).json()
    assert items == []


def test_get_resolve_real_firm_filters_rows_by_firm_docs(client, monkeypatch):
    # A real firm returns only live rows whose doc_id belongs to the firm's documents; rows for other
    # docs (or with no doc_id, which can't be attributed) are excluded — a clean, correct scope.
    tc, conn = client
    queue_store.enqueue(conn, mention="Held Issuer Inc.", normalized="held", label="Company",
                        doc_id="firm_doc_1",
                        candidates=[{"cik": "0000000001", "title": "HELD", "score": 0.5}])
    queue_store.enqueue(conn, mention="Unrelated Corp.", normalized="unrelated", label="Company",
                        doc_id="other_doc_9", candidates=[])
    queue_store.enqueue(conn, mention="No Doc Mention", normalized="nodoc", label="Company",
                        doc_id=None, candidates=[])
    # the firm owns only firm_doc_1
    monkeypatch.setattr(resolution_routes, "_firm_doc_ids", lambda firm: {"firm_doc_1"})

    items = tc.get("/resolve", params={"firm": "Acme Global Real Estate Fund"}).json()
    assert [e["name"] for e in items] == ["Held Issuer Inc."]


def test_resolve_known_ticker_is_resolved(client):
    tc, _conn = client
    resp = tc.post("/resolve/", json={"name": "NVIDIA Corporation", "ticker": "NVDA"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "resolved"
    assert body["cik"] == "0001045810"
    assert body["method"] == "ticker"


def test_provisional_queue_and_merge_flow(client):
    tc, _conn = client
    # An unknown mention → provisional + queued
    resp = tc.post("/resolve/", json={"name": "Totally Unknown Widgets LLC"})
    body = resp.json()
    assert body["status"] == "provisional"
    queue_id = body["queue_id"]
    assert queue_id is not None

    listing = tc.get("/resolve/provisional").json()
    assert listing["count"] == 1
    assert listing["items"][0]["mention"] == "Totally Unknown Widgets LLC"

    # Steward merges it onto a CIK/LEI
    merged = tc.post(
        "/resolve/merge",
        json={"queue_id": queue_id, "cik": "0000000001", "lei": "TESTLEI0000000000000"},
    ).json()
    assert merged["merged"]["status"] == "merged"
    assert merged["merged"]["candidate_cik"] == "0000000001"

    # No longer provisional
    assert tc.get("/resolve/provisional").json()["count"] == 0
    # But still in the full queue (kept for audit)
    assert tc.get("/resolve/queue").json()["count"] == 1


def test_merge_unknown_id_404(client):
    tc, _conn = client
    resp = tc.post("/resolve/merge", json={"queue_id": 999, "cik": "0000000001"})
    assert resp.status_code == 404
