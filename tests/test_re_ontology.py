"""Real-estate ontology extension + FIBO-grounding consistency (offline; no Neo4j / LLM)."""

from __future__ import annotations

from api.fibo.grounding import LABEL_FIBO_CURIE
from api.ontology.schema import (
    entity_spec,
    extractable_entity_labels,
    extraction_json_schema,
    neo4j_ddl,
    relation_types,
)

RE_LABELS = ("RealProperty", "Portfolio", "Lease", "Loan", "Valuation")
RE_RELATIONS = (
    "HOLDS_PORTFOLIO", "CONTAINS_PROPERTY", "LEASES_SPACE_AT",
    "COLLATERALIZED_BY", "HAS_VALUATION", "SERVICED_BY",
)


def test_re_entities_present_grounded_and_structural():
    for label in RE_LABELS:
        spec = entity_spec(label)
        assert spec is not None, label
        assert spec.extractable is False  # structural: MDM/seed-written, never LLM-extracted
        assert spec.fibo_class  # every RE type carries a FIBO grounding


def test_re_relations_present():
    types = relation_types()
    for r in RE_RELATIONS:
        assert r in types


def test_schema_fibo_class_agrees_with_grounding_map():
    # the EntitySpec.fibo_class default must match the grounding module's default map (one truth)
    for label, curie in LABEL_FIBO_CURIE.items():
        spec = entity_spec(label)
        if spec is not None and spec.fibo_class is not None:
            assert spec.fibo_class == curie, label


def test_structural_re_types_absent_from_extraction_schema():
    # RE types are extractable=False -> the LLM extraction contract must NOT include them,
    # so existing extraction/steward behaviour is unchanged.
    extractable = set(extractable_entity_labels())
    for label in RE_LABELS:
        assert label not in extractable
    schema_text = str(extraction_json_schema())
    assert "RealProperty" not in schema_text and "Valuation" not in schema_text


def test_neo4j_ddl_covers_new_labels():
    ddl = "\n".join(neo4j_ddl())
    for label in RE_LABELS:
        assert label in ddl  # a uniqueness constraint was emitted for each
