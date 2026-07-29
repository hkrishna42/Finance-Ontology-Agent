"""Offline MDM tests — matching, attribute survivorship, golden-record publish, and the wizard routes.

Pure SQLite (in-memory / temp) + the lakehouse seed; no Neo4j, no LLM, no network.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.lakehouse import store as lakehouse
from api.mdm import survivorship as sv
from api.mdm.routes import router as mdm_router
from api.stores.sqlite import connect


def _conn():
    conn = connect(":memory:")
    lakehouse.bootstrap(conn)
    return conn


def _decisions(golden):
    return {d.attribute: d for d in golden.decisions}


# --- matching + survivorship engine ---------------------------------------------------------


def test_flagship_property_matches_above_threshold():
    conn = _conn()
    m = sv.match_only(conn, "RealProperty", "harborview_tower")
    assert m["resolved"] is True and m["confidence"] >= 0.88
    assert "fuzzy name" in m["method"] and "property_id" in m["blocking_keys"]


def test_survivorship_rules_pick_expected_winners():
    conn = _conn()
    policy = sv.load_policy()
    records = lakehouse.bronze_for_master(conn, "RealProperty", "harborview_tower")
    trust = lakehouse.trust_by_system(conn)
    golden = sv.build_golden(records, "RealProperty", "harborview_tower", policy, trust)
    d = _decisions(golden)
    # curated Gold governs the canonical name; loan-servicing has the most complete (postcode) address
    assert d["property_name"].winning_source == "curated_gold"
    assert golden.canonical_attributes["property_name"] == "Harborview Tower"
    assert d["address"].winning_source == "loan_servicing"
    assert "SE1 2AA" in golden.canonical_attributes["address"]
    # id crosswalk keeps the alternates
    assert golden.pk == "PROP-1001"
    assert set(golden.retained_alternate_ids["property_id"]) == {"HVT-001", "PROP1001"}
    # numeric within-tolerance -> curated value survives; ontology classification collapses the type
    assert d["rentable_area"].winning_source == "curated_gold"
    assert "tolerance" in d["rentable_area"].rationale.lower()
    assert d["property_type"].winning_source == "ontology rule engine"
    assert golden.canonical_attributes["property_type"] == "Commercial Office"


def test_golden_fibo_grounding_incl_issuer_refinement():
    conn = _conn()
    policy = sv.load_policy()
    trust = lakehouse.trust_by_system(conn)
    prop = sv.build_golden(lakehouse.bronze_for_master(conn, "RealProperty", "harborview_tower"),
                           "RealProperty", "harborview_tower", policy, trust)
    assert prop.fibo_curie == "fibo-fnd-plc-rp:RealProperty"
    # the bond-issuer role refines the golden legal entity to CorporateDebtIssuer
    issuer = sv.build_golden(lakehouse.bronze_for_master(conn, "Company", "atlantic_credit"),
                             "Company", "atlantic_credit", policy, trust)
    assert issuer.fibo_curie == "fibo-sec-dbt-dbt:CorporateDebtIssuer"


def test_match_and_merge_publishes_gold_and_lineage():
    conn = _conn()
    out = sv.run_match_and_merge(conn, "RealProperty", "harborview_tower")
    assert out["published"] is True and out["match"]["resolved"] is True
    # the Gold dim row was written with its FIBO class + lakehouse PK
    row = lakehouse.get_gold_dim(conn, "dim_property", "PROP-1001")
    assert row["attributes"]["property_name"] == "Harborview Tower"
    assert row["fibo_class"] == "fibo-fnd-plc-rp:RealProperty"
    # per-attribute lineage records which source won each attribute
    winners = {ln["attribute"]: ln["bronze_record_id"] for ln in row["lineage"] if ln["contributed"]}
    assert winners["property_name"] == "curated_gold:harborview"
    assert winners["address"] == "loan_servicing:harborview"


# --- wizard routes --------------------------------------------------------------------------


def test_mdm_wizard_routes(tmp_path, monkeypatch):
    from api import config

    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "app.db"))
    config.get_settings.cache_clear()
    app = FastAPI()
    app.include_router(mdm_router)
    client = TestClient(app)

    # 1. list master entities
    entities = client.get("/mdm/entities").json()["entities"]
    by_id = {e["entity_id"]: e for e in entities}
    assert "RealProperty:harborview_tower" in by_id
    assert by_id["RealProperty:harborview_tower"]["n_sources"] == 4
    assert by_id["Company:atlantic_credit"]["fibo_curie"] == "fibo-sec-dbt-dbt:CorporateDebtIssuer"

    # 2. source records (conflicting), most-trusted first
    src = client.get("/mdm/entities/RealProperty:harborview_tower/sources").json()["sources"]
    assert len(src) == 4 and src[0]["system"] == "curated_gold" and src[0]["trust"] == 95

    # 3. match
    m = client.post("/mdm/match", json={"entity_id": "RealProperty:harborview_tower"}).json()
    assert m["resolved"] is True and m["confidence_pct"] >= 88

    # 4. policy
    pol = client.get("/mdm/survivorship-policy").json()
    assert "RealProperty" in pol["entity_types"]

    # 5. merge -> golden record + re-fetch
    g = client.post("/mdm/merge", json={"entity_id": "RealProperty:harborview_tower"}).json()
    assert g["published"] is True
    assert g["golden_record"]["lakehouse_provenance"] == "Lakehouse.dim_property (PK: PROP-1001)"
    refetch = client.get("/mdm/golden/dim_property/PROP-1001")
    assert refetch.status_code == 200
    config.get_settings.cache_clear()
