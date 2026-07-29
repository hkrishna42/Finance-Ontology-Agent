"""Offline lakehouse tests — medallion schema, neutral seed, gold + per-attribute lineage.

Pure SQLite (`:memory:` / temp file); no Neo4j, no network.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.lakehouse import store
from api.stores.sqlite import connect


def _seeded():
    conn = connect(":memory:")
    store.bootstrap(conn)
    return conn


def test_seed_loads_four_source_systems_with_trust():
    conn = _seeded()
    systems = store.list_source_systems(conn)
    assert len(systems) == 4
    trust = store.trust_by_system(conn)
    # gold layer is most trusted; loan servicing least (the mockup's 95..80 spread)
    assert trust["curated_gold"] == 95 and trust["loan_servicing"] == 80
    assert systems[0]["system_id"] == "curated_gold"  # ordered by trust desc


def test_bronze_records_conflict_across_sources():
    conn = _seeded()
    recs = store.bronze_for_master(conn, "RealProperty", "harborview_tower")
    assert len(recs) == 4
    names = {r["attributes"]["property_name"] for r in recs}
    # deliberately conflicting representations of the same property
    assert names == {"Harborview Tower", "Harborview Twr", "Harborview Office TWR"}
    ids = {r["attributes"]["property_id"] for r in recs}
    assert "PROP-1001" in ids and "HVT-001" in ids and "PROP1001" in ids


def test_master_entities_lists_clusters():
    conn = _seeded()
    masters = {(m["entity_type"], m["master_key"]): m["n_sources"] for m in store.master_entities(conn)}
    assert masters[("RealProperty", "harborview_tower")] == 4
    assert masters[("Company", "meridian_investments")] == 3
    assert masters[("Fund", "meridian_core_re_fund")] == 2


def test_gold_dim_and_lineage_roundtrip():
    conn = _seeded()
    store.upsert_gold_dim(
        conn, dim_table="dim_property", pk="PROP-1001", entity_type="RealProperty",
        fibo_class="fibo-fnd-plc-rp:RealProperty", golden_record_id="GR-1",
        attributes={"property_name": "Harborview Tower", "property_id": "PROP-1001"},
    )
    store.replace_lineage(conn, "dim_property", "PROP-1001", [
        {"bronze_record_id": "curated_gold:harborview", "attribute": "property_name", "contributed": 1},
        {"bronze_record_id": "pms:harborview", "attribute": "rentable_area", "contributed": 1},
    ])
    row = store.get_gold_dim(conn, "dim_property", "PROP-1001")
    assert row["attributes"]["property_name"] == "Harborview Tower"
    assert row["fibo_class"] == "fibo-fnd-plc-rp:RealProperty"
    winners = {ln["attribute"]: ln["bronze_record_id"] for ln in row["lineage"] if ln["contributed"]}
    assert winners["property_name"] == "curated_gold:harborview"
    assert winners["rentable_area"] == "pms:harborview"


def test_lakehouse_routes(tmp_path, monkeypatch):
    from api import config
    from api.lakehouse.routes import router

    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "app.db"))
    config.get_settings.cache_clear()
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    systems = client.get("/lakehouse/source-systems").json()["source_systems"]
    assert len(systems) == 4
    assert client.get("/lakehouse/dim/dim_property/DOES-NOT-EXIST").status_code == 404
    config.get_settings.cache_clear()
