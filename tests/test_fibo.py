"""Offline FIBO service tests — real OWL loading, OWL-RL reasoning, grounding, SPARQL, routes.

Pure rdflib + owlrl: no Neo4j, no LLM, no network. Proves the curated FIBO slice loads with genuine
class IRIs, that the reasoner catches a disjointness violation, and that grounding + SPARQL + the HTTP
surface behave.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.fibo import grounding, reasoner, sparql, tbox
from api.fibo.routes import router as fibo_router
from api.ontology.schema import entity_spec

LEGAL_ENTITY = tbox.iri_for("cmns-org:LegalEntity")
REAL_PROPERTY = tbox.iri_for("fibo-fnd-plc-rp:RealProperty")
LOAN = tbox.iri_for("fibo-loan-ln-ln:Loan")
CIV = tbox.iri_for("fibo-sec-sec-pls:CollectiveInvestmentVehicle")


# --- TBox load + reasoning ------------------------------------------------------------------


def test_tbox_loads_real_fibo_classes():
    classes = tbox.all_classes()
    curies = {c["curie"] for c in classes}
    # the flagship classes we ground to, with their real IRIs
    assert "fibo-fnd-plc-rp:RealProperty" in curies
    assert "cmns-org:LegalEntity" in curies
    assert "fibo-loan-ln-ln:Loan" in curies
    assert "fibo-sec-sec-pls:CollectiveInvestmentVehicle" in curies
    assert REAL_PROPERTY == "https://spec.edmcouncil.org/fibo/ontology/FND/Places/RealProperty/RealProperty"


def test_owlrl_materializes_transitive_subclass():
    # RealProperty -> RealEstate -> Asset (transitive closure) should be materialized.
    rp = next(c for c in tbox.all_classes() if c["curie"] == "fibo-fnd-plc-rp:RealProperty")
    assert "fibo-fnd-oac-own:Asset" in rp["parents"]  # transitive via RealEstate
    assert "fibo-fnd-oac-own:PhysicalAsset" in rp["parents"]  # direct


def test_iri_curie_roundtrip():
    assert tbox.curie_for(tbox.iri_for("fibo-loan-ln-ln:Loan")) == "fibo-loan-ln-ln:Loan"
    assert tbox.iri_for("unknown:Thing") == "unknown:Thing"  # unknown prefix passthrough


# --- grounding (Agent B) --------------------------------------------------------------------


def test_ground_company_and_refinements():
    assert grounding.ground("Company").curie == "cmns-org:LegalEntity"
    # a debt/bond-issuer role refines Company -> CorporateDebtIssuer
    issuer = grounding.ground("Company", attributes={"role": "bond issuer"})
    assert issuer.curie == "fibo-sec-dbt-dbt:CorporateDebtIssuer" and issuer.refined
    # real-property type variants all map to the one canonical RealProperty class
    assert grounding.ground("RealProperty", attributes={"property_type": "Class A Office"}).curie \
        == "fibo-fnd-plc-rp:RealProperty"
    # an unmapped label is a valid ungrounded state
    assert grounding.ground("RiskFactor").grounded is False


def test_entity_spec_carries_fibo_class():
    assert entity_spec("Company").fibo_class == "cmns-org:LegalEntity"
    assert entity_spec("Person").fibo_class is None  # additive default, unset elsewhere


# --- reasoner (Agent C) ---------------------------------------------------------------------


def test_reasoner_passes_consistent_grounding():
    r = reasoner.validate([
        {"id": "fund1", "class_iri": CIV},
        {"id": "prop1", "class_iri": REAL_PROPERTY},
        {"id": "acme", "class_iri": LEGAL_ENTITY},
    ])
    assert r.valid is True and r.checked == 3 and r.violations == []


def test_reasoner_detects_disjointness_violation():
    # one individual typed as BOTH a LegalEntity and RealProperty (disjoint) is inconsistent
    r = reasoner.validate([
        {"id": "x", "class_iri": LEGAL_ENTITY},
        {"id": "x", "class_iri": REAL_PROPERTY},
    ])
    assert r.valid is False and len(r.violations) == 1
    classes = {r.violations[0]["class1"], r.violations[0]["class2"]}
    assert classes == {"cmns-org:LegalEntity", "fibo-fnd-plc-rp:RealProperty"}


# --- SPARQL ---------------------------------------------------------------------------------


def test_sparql_over_reasoned_tbox():
    out = sparql.query(
        "SELECT ?c WHERE { ?c <http://www.w3.org/2000/01/rdf-schema#subClassOf> "
        "<https://spec.edmcouncil.org/fibo/ontology/FBC/FinancialInstruments/FinancialInstruments/DebtInstrument> }"
    )
    flat = {row[0] for row in out["rows"]}
    assert LOAN in flat  # Loan is a DebtInstrument


def test_sparql_rejects_service():
    import pytest

    with pytest.raises(sparql.SparqlError):
        sparql.query("SELECT ?x WHERE { SERVICE <http://evil/> { ?x ?p ?o } }")


# --- routes ---------------------------------------------------------------------------------


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(fibo_router)
    return TestClient(app)


def test_fibo_routes():
    c = _client()
    assert c.get("/fibo/classes").json()["count"] > 0
    g = c.get("/fibo/ground", params={"label": "Company"}).json()
    assert g["curie"] == "cmns-org:LegalEntity" and g["grounded"] is True
    v = c.post("/fibo/validate", json={"instances": [
        {"id": "x", "class_iri": LEGAL_ENTITY}, {"id": "x", "class_iri": LOAN}]}).json()
    assert v["valid"] is False
    s = c.post("/fibo/sparql", json={"query": "ASK { ?c a <http://www.w3.org/2002/07/owl#Class> }"})
    assert s.status_code == 200
    bad = c.post("/fibo/sparql", json={"query": "DELETE { ?x ?p ?o } WHERE { ?x ?p ?o }"})
    assert bad.status_code == 400


# --- pipeline grounding (Agent B stamps FIBO on ingested nodes) ------------------------------


def test_pipeline_stamps_fibo_grounding_on_nodes():
    from api.ingest.pipeline import _entity_node_props
    from api.ontology.models import ExtractedEntity

    props = _entity_node_props(ExtractedEntity(label="Company", name="Acme Corp", span="Acme Corp"))
    assert props["fibo_class"] == "cmns-org:LegalEntity"
    assert props["fibo_grounded"] is True and props["reasoning_valid"] is True

    issuer = ExtractedEntity(label="Company", name="Atlantic Credit", span="Atlantic Credit",
                             attributes={"role": "bond issuer"})
    assert _entity_node_props(issuer)["fibo_class"] == "fibo-sec-dbt-dbt:CorporateDebtIssuer"

    # an ungrounded (but valid) label leaves no FIBO props
    assert "fibo_class" not in _entity_node_props(
        ExtractedEntity(label="RiskFactor", name="Supply risk", span="Supply risk"))

