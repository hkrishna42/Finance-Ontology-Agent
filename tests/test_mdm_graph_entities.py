"""MDM master data reflects the live graph — graph-derived Company entities join the seed.

Offline: a fake store (`.run` returns canned rows) is injected via `mdm.routes._GRAPH_STORE`, so no
Neo4j is required; the seed remains the fallback when the graph yields nothing.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.mdm import routes as mdm_routes
from api.mdm.graph import graph_master_entities
from api.mdm.routes import router as mdm_router


class FakeStore:
    def __init__(self, rows):
        self._rows = rows
        self.queries: list[tuple] = []

    def run(self, query, **params):
        self.queries.append((query, params))
        return list(self._rows)

    def close(self):  # pragma: no cover - lifecycle no-op
        pass


_GRAPH_ROWS = [
    {"name": "NVIDIA", "cik": "0001045810", "lei": None, "category": None,
     "fibo_class": "cmns-org:LegalEntity", "n_docs": 2, "n_mentions": 5, "n_props": 6},
    {"name": "Meridian Supplier Co", "cik": None, "lei": None, "category": None,
     "fibo_class": None, "n_docs": 3, "n_mentions": 4, "n_props": 3},
]


def test_graph_master_entities_shape():
    ents = graph_master_entities(FakeStore(_GRAPH_ROWS))
    nvidia = next(e for e in ents if e["master_key"] == "NVIDIA")
    assert nvidia["entity_id"] == "Company:NVIDIA"
    assert nvidia["entity_type"] == "Company"
    assert nvidia["n_sources"] == 2 and nvidia["n_mentions"] == 5
    assert nvidia["n_attributes"] == 6
    assert nvidia["fibo_curie"] == "cmns-org:LegalEntity"
    assert nvidia["fibo_class"]  # a resolved IRI
    assert nvidia["source"] == "graph"
    # a row with no stamped fibo_class still grounds to the default Company class
    other = next(e for e in ents if e["master_key"] == "Meridian Supplier Co")
    assert other["fibo_curie"] == "cmns-org:LegalEntity"


def test_list_entities_merges_seed_and_graph(tmp_path, monkeypatch):
    from api import config

    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "app.db"))
    config.get_settings.cache_clear()
    monkeypatch.setattr(mdm_routes, "_GRAPH_STORE", FakeStore(_GRAPH_ROWS))

    app = FastAPI()
    app.include_router(mdm_router)
    entities = TestClient(app).get("/mdm/entities").json()["entities"]
    by_id = {e["entity_id"]: e for e in entities}

    # seed entities still present + tagged lakehouse (the wizard flow is preserved)
    assert by_id["RealProperty:harborview_tower"]["source"] == "lakehouse"
    assert by_id["RealProperty:harborview_tower"]["n_sources"] == 4
    # graph entities appended + tagged graph
    assert by_id["Company:NVIDIA"]["source"] == "graph"
    assert "Company:Meridian Supplier Co" in by_id
    config.get_settings.cache_clear()


def test_list_entities_falls_back_to_seed_when_graph_empty(tmp_path, monkeypatch):
    from api import config

    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "app.db"))
    config.get_settings.cache_clear()
    monkeypatch.setattr(mdm_routes, "_GRAPH_STORE", FakeStore([]))  # graph yields nothing

    app = FastAPI()
    app.include_router(mdm_router)
    entities = TestClient(app).get("/mdm/entities").json()["entities"]
    assert entities and all(e.get("source") == "lakehouse" for e in entities)
    config.get_settings.cache_clear()
