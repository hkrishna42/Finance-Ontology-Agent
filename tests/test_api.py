"""API smoke tests: health, ontology introspection, and the demo SSE stream."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from api.main import app
from api.ontology.schema import ENTITY_SPECS, RELATION_SPECS

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["mode"] == "stub"


def test_ontology_info():
    r = client.get("/ontology/info")
    assert r.status_code == 200
    body = r.json()
    assert body["entity_count"] == len(ENTITY_SPECS)
    assert body["relation_count"] == len(RELATION_SPECS)
    assert body["vector_dim"] == 384


def test_ontology_schema_endpoint():
    r = client.get("/ontology/schema")
    assert r.status_code == 200
    assert r.json()["required"] == ["entities", "relations"]


def test_ontology_ddl_endpoint():
    r = client.get("/ontology/ddl")
    assert r.status_code == 200
    stmts = r.json()["statements"]
    assert any("VECTOR INDEX chunk_embedding" in s for s in stmts)


def test_ontology_card_endpoint():
    r = client.get("/ontology/card")
    assert r.status_code == 200
    assert "SUPPLIES_TO" in r.json()["card"]


def test_demo_stream_emits_sequence():
    with client.stream("GET", "/ingest/demo/stream") as r:
        assert r.status_code == 200
        events = []
        for line in r.iter_lines():
            if line.startswith("event:"):
                events.append(line.split(":", 1)[1].strip())
            elif line.startswith("data:"):
                json.loads(line.split(":", 1)[1].strip())  # each data payload is valid JSON
    assert events[0] == "job.started"
    assert events[-1] == "job.completed"
    assert "extracted" in events
