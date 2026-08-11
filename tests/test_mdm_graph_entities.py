"""MDM master data reflects the live graph — graph-derived Company entities join the seed.

Offline: a fake store (`.run` returns canned rows) is injected via `mdm.routes._GRAPH_STORE`, so no
Neo4j is required; the seed remains the fallback when the graph yields nothing.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.firms.scope import DEMO_FIRM_NAME
from api.mdm import routes as mdm_routes
from api.mdm.graph import GRAPH_MASTER_CYPHER, GRAPH_MASTER_FIRM_CYPHER, graph_master_entities
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


def test_graph_master_entities_unscoped_uses_global_cypher():
    store = FakeStore(_GRAPH_ROWS)
    graph_master_entities(store)  # firm=None → whole-corpus recurring-Company listing
    query, params = store.queries[-1]
    assert query == GRAPH_MASTER_CYPHER
    assert "firm" not in params


def test_graph_master_entities_firm_scoped_uses_firm_cypher():
    store = FakeStore(_GRAPH_ROWS)
    ents = graph_master_entities(store, firm="Acme Global Real Estate Fund")
    # the firm-subgraph (held-issuers) Cypher was used, bound to the firm
    query, params = store.queries[-1]
    assert query == GRAPH_MASTER_FIRM_CYPHER
    assert params["firm"] == "Acme Global Real Estate Fund"
    # rows still map to the same MdmEntity shape, tagged source: graph
    assert {e["entity_id"] for e in ents} == {"Company:NVIDIA", "Company:Meridian Supplier Co"}
    assert all(e["source"] == "graph" for e in ents)


def test_list_entities_real_firm_scopes_to_graph_only(tmp_path, monkeypatch):
    # A real onboarded firm (?firm=<name>) → NO lakehouse seed; only its firm-scoped graph entities.
    from api import config

    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "app.db"))
    config.get_settings.cache_clear()
    store = FakeStore(_GRAPH_ROWS)
    monkeypatch.setattr(mdm_routes, "_GRAPH_STORE", store)

    app = FastAPI()
    app.include_router(mdm_router)
    entities = TestClient(app).get(
        "/mdm/entities", params={"firm": "Acme Global Real Estate Fund"}
    ).json()["entities"]

    # only graph-derived entities — the neutral lakehouse seed is suppressed for a real firm
    assert entities and all(e["source"] == "graph" for e in entities)
    assert not any(e["entity_id"].startswith("RealProperty:") for e in entities)
    # and the firm-subgraph Cypher was used, bound to the firm
    query, params = store.queries[-1]
    assert query == GRAPH_MASTER_FIRM_CYPHER and params["firm"] == "Acme Global Real Estate Fund"
    config.get_settings.cache_clear()


def test_list_entities_real_firm_with_no_holdings_is_empty(tmp_path, monkeypatch):
    # A real firm holding nothing → [] (a clean empty state, NEVER the seed's issuers).
    from api import config

    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "app.db"))
    config.get_settings.cache_clear()
    monkeypatch.setattr(mdm_routes, "_GRAPH_STORE", FakeStore([]))  # firm holds nothing

    app = FastAPI()
    app.include_router(mdm_router)
    entities = TestClient(app).get(
        "/mdm/entities", params={"firm": "Empty Holdings LLC"}
    ).json()["entities"]
    assert entities == []
    config.get_settings.cache_clear()


def test_list_entities_demo_firm_keeps_seed_and_graph(tmp_path, monkeypatch):
    # The fictional demo firm stays demo scope → lakehouse seed PLUS the global graph listing.
    from api import config

    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "app.db"))
    config.get_settings.cache_clear()
    monkeypatch.setattr(mdm_routes, "_GRAPH_STORE", FakeStore(_GRAPH_ROWS))

    app = FastAPI()
    app.include_router(mdm_router)
    entities = TestClient(app).get(
        "/mdm/entities", params={"firm": DEMO_FIRM_NAME}
    ).json()["entities"]
    by_id = {e["entity_id"]: e for e in entities}
    assert by_id["RealProperty:harborview_tower"]["source"] == "lakehouse"
    assert by_id["Company:NVIDIA"]["source"] == "graph"
    config.get_settings.cache_clear()


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
