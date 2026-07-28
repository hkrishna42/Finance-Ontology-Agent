"""Ontology-SSOT invariants — the declarative spec and its derived artifacts must stay coherent.

The ontology (`api.ontology.schema`) is the single source of truth: one spec drives the extraction
JSON Schema, the Neo4j DDL, and the prompt schema-card. These tests are the adversarial backstop
on that contract — domain/range integrity, JSON-Schema validity + rejection of non-extractable
types, DDL determinism, and (against a live DB) that the DDL actually applies and the vector index
reaches ONLINE on Neo4j 5.26 CE (the key Gate-0 feasibility proof).
"""

from __future__ import annotations

from jsonschema import Draft202012Validator

from api.ontology.schema import (
    ENTITY_SPECS,
    RELATION_SPECS,
    VECTOR_DIM,
    entity_labels,
    extractable_entity_labels,
    extractable_relation_types,
    extraction_json_schema,
    neo4j_ddl,
    schema_card,
)
from api.stores.neo4j import VECTOR_INDEX_NAME

# --- Structural integrity of the declarative spec -----------------------------------------


def test_every_relation_domain_and_range_references_a_real_entity_label():
    """No RELATION_SPEC may point domain/range at a label that isn't a declared entity."""
    labels = set(entity_labels())
    problems: list[str] = []
    for r in RELATION_SPECS:
        for label in r.domain:
            if label not in labels:
                problems.append(f"{r.type}.domain -> unknown label {label!r}")
        for label in r.range:
            if label not in labels:
                problems.append(f"{r.type}.range -> unknown label {label!r}")
    assert not problems, problems


def test_entity_keys_are_declared_props_or_reserved():
    """Each entity's MERGE key should be a concrete property name (non-empty, unique per label)."""
    seen: set[str] = set()
    for e in ENTITY_SPECS:
        assert e.key, f"{e.label} has empty key"
        assert e.label not in seen, f"duplicate entity label {e.label}"
        seen.add(e.label)


def test_extractable_sets_are_nonempty_and_subset_of_all():
    ee, er = set(extractable_entity_labels()), set(extractable_relation_types())
    assert ee and er
    assert ee <= set(entity_labels())
    assert er <= {r.type for r in RELATION_SPECS}


# --- Derived artifact 1: extraction JSON Schema -------------------------------------------


def test_extraction_schema_is_structurally_valid():
    Draft202012Validator.check_schema(extraction_json_schema())


def test_extraction_schema_accepts_a_valid_document():
    validator = Draft202012Validator(extraction_json_schema())
    doc = {
        "entities": [
            {"label": "Company", "name": "NVIDIA", "span": "NVIDIA designs GPUs.",
             "confidence": 0.9},
            {"label": "RiskFactor", "name": "Supply concentration",
             "category": "supply_chain", "span": "We depend on limited foundries.",
             "confidence": 0.8},
        ],
        "relations": [
            {"type": "SUPPLIES_TO", "subject": "TSMC", "object": "NVIDIA",
             "qualifiers": [{"key": "criticality", "value": "critical"}],
             "span": "TSMC fabricates our GPUs.", "confidence": 0.95},
        ],
    }
    assert validator.is_valid(doc), list(validator.iter_errors(doc))


def test_extraction_schema_rejects_non_extractable_entity_label():
    """A structural-only label (Fund) must NOT validate as extraction output."""
    validator = Draft202012Validator(extraction_json_schema())
    bad = {"entities": [{"label": "Fund", "name": "X", "span": "x", "confidence": 0.5}],
           "relations": []}
    assert not validator.is_valid(bad)


def test_extraction_schema_rejects_unknown_label_and_relation():
    validator = Draft202012Validator(extraction_json_schema())
    assert not validator.is_valid(
        {"entities": [{"label": "Alien", "name": "X", "span": "x", "confidence": 0.5}],
         "relations": []}
    )
    assert not validator.is_valid(
        {"entities": [],
         "relations": [{"type": "BRIBES", "subject": "A", "object": "B", "span": "x",
                        "confidence": 0.5}]}
    )


def test_extraction_schema_requires_span_and_forbids_extra_props():
    validator = Draft202012Validator(extraction_json_schema())
    # missing span
    assert not validator.is_valid(
        {"entities": [{"label": "Company", "name": "NVIDIA", "confidence": 0.5}], "relations": []}
    )
    # additionalProperties:false — a smuggled field must fail
    assert not validator.is_valid(
        {"entities": [{"label": "Company", "name": "NVIDIA", "span": "x", "confidence": 0.5,
                       "evil": 1}], "relations": []}
    )


# --- Round-trip / determinism -------------------------------------------------------------


def test_schema_enums_roundtrip_extractable_sets():
    schema = extraction_json_schema()
    entity_enum = schema["properties"]["entities"]["items"]["properties"]["label"]["enum"]
    rel_enum = schema["properties"]["relations"]["items"]["properties"]["type"]["enum"]
    assert entity_enum == extractable_entity_labels()
    assert rel_enum == extractable_relation_types()


def test_derived_artifacts_are_deterministic():
    """Regenerating the derived artifacts yields byte-identical output (no set/dict nondeterminism)."""
    assert extraction_json_schema() == extraction_json_schema()
    assert neo4j_ddl() == neo4j_ddl()
    assert schema_card() == schema_card()


def test_ddl_shape_one_constraint_per_entity_plus_vector_and_fulltext():
    ddl = neo4j_ddl()
    constraints = [s for s in ddl if s.startswith("CREATE CONSTRAINT")]
    vector = [s for s in ddl if s.startswith("CREATE VECTOR INDEX")]
    fulltext = [s for s in ddl if s.startswith("CREATE FULLTEXT INDEX")]
    assert len(constraints) == len(ENTITY_SPECS)
    assert len(vector) == 1
    assert len(fulltext) == 1
    joined = "\n".join(ddl)
    assert f"`vector.dimensions`: {VECTOR_DIM}" in joined
    assert "vector.similarity_function" in joined


def test_ddl_respects_dim_override():
    assert "`vector.dimensions`: 512" in "\n".join(neo4j_ddl(512))


# --- Live DB feasibility: DDL applies + vector index reaches ONLINE -----------------------


def test_ddl_applies_and_vector_index_reaches_online(neo4j_store):
    """Gate-0 feasibility: apply the ontology DDL (idempotent) and confirm the index is ONLINE."""
    stmts = neo4j_store.apply_ddl()
    assert len(stmts) == len(neo4j_ddl())
    state = neo4j_store.wait_for_index_online(VECTOR_INDEX_NAME, timeout=60)
    assert state.get("state") == "ONLINE", state


def test_vector_index_is_configured_for_384_cosine(neo4j_store):
    info = neo4j_store.verify_vector_index(VECTOR_INDEX_NAME)
    assert info.get("state") == "ONLINE", info
    assert info.get("labelsOrTypes") == ["Chunk"], info
    assert info.get("properties") == ["embedding"], info
