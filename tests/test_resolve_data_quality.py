"""`/resolve` data quality — queue rows never render as confidence 0.00 or span "".

confidence is the resolver's best-candidate score (or null when there is genuinely no candidate);
span is the source snippet (or null when absent). Offline, via an injected in-memory SQLite conn.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.resolution import store as queue_store
from api.resolution.routes import get_conn, router


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


def _by_name(items):
    return {e["name"]: e for e in items}


def test_confidence_is_best_candidate_score_and_span_present(client):
    tc, conn = client
    queue_store.enqueue(
        conn,
        mention="Acme Robotics",
        normalized="acme robotics",
        label="Company",
        span="Acme Robotics supplies actuators to the fund's largest holding.",
        doc_id="doc1",
        chunk_id="doc1_c1",
        confidence=0.0,  # the resolver stores 0.0 for a provisional mention
        candidates=[
            {"cik": "1", "title": "Acme Robotics Inc", "ticker": "ACME", "score": 0.72},
            {"cik": "2", "title": "Acme Holdings", "ticker": "", "score": 0.66},
        ],
    )
    e = _by_name(tc.get("/resolve").json())["Acme Robotics"]
    assert e["confidence"] == 0.72  # best candidate score, NOT the stored 0.0
    assert e["span"] == "Acme Robotics supplies actuators to the fund's largest holding."
    assert e["candidates"][0]["existing_id"]  # candidates normalized to the UI shape


def test_no_candidate_yields_null_confidence_and_null_span(client):
    tc, conn = client
    queue_store.enqueue(
        conn,
        mention="Totally Unknown Widgets LLC",
        normalized="totally unknown widgets",
        confidence=0.0,
        candidates=[],  # genuinely nothing to match against
    )
    e = _by_name(tc.get("/resolve").json())["Totally Unknown Widgets LLC"]
    assert e["confidence"] is None  # NOT 0.0 -> UI can show "no candidate match"
    assert e["span"] is None  # NOT "" -> UI shows no snippet cleanly


def test_set_provenance_backfills_span_on_a_queued_row(client):
    # The ingest pipeline enriches a just-queued row with the source span it holds.
    tc, conn = client
    qid = queue_store.enqueue(conn, mention="Beacon Metals", confidence=0.0, candidates=[])
    assert tc.get("/resolve").json()[0]["span"] is None
    queue_store.set_provenance(
        conn, qid, span="Beacon Metals is a key supplier.", doc_id="d2", chunk_id="d2_c3"
    )
    e = tc.get("/resolve").json()[0]
    assert e["span"] == "Beacon Metals is a key supplier."
    assert e["doc_id"] == "d2" and e["chunk_id"] == "d2_c3"
