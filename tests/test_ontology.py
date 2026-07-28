"""Ontology SSOT tests: the three derived artifacts stay valid and self-consistent."""

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator

from api.ontology.models import ExtractionResult
from api.ontology.schema import (
    ENTITY_SPECS,
    RELATION_SPECS,
    OntologyInfo,
    extractable_entity_labels,
    extractable_relation_types,
    extraction_json_schema,
    neo4j_ddl,
    schema_card,
)


def test_extraction_schema_is_valid_json_schema():
    schema = extraction_json_schema()
    # Raises SchemaError if the schema itself is malformed.
    Draft202012Validator.check_schema(schema)


def test_valid_extraction_dict_validates():
    schema = extraction_json_schema()
    validator = Draft202012Validator(schema)
    sample = {
        "entities": [
            {
                "label": "Company",
                "name": "NVIDIA",
                "span": "NVIDIA designs accelerated computing platforms.",
                "confidence": 0.95,
            },
            {
                "label": "RiskFactor",
                "name": "Supply concentration",
                "category": "supply_chain",
                "span": "We depend on a limited number of foundry partners.",
                "confidence": 0.8,
            },
        ],
        "relations": [
            {
                "type": "SUPPLIES_TO",
                "subject": "TSMC",
                "object": "NVIDIA",
                "qualifiers": [{"key": "criticality", "value": "critical"}],
                "span": "TSMC manufactures substantially all of our chips.",
                "confidence": 0.9,
            }
        ],
    }
    assert validator.is_valid(sample), list(validator.iter_errors(sample))


def test_invalid_label_fails():
    schema = extraction_json_schema()
    validator = Draft202012Validator(schema)
    bad = {
        "entities": [
            {"label": "Alien", "name": "X", "span": "x", "confidence": 0.5}
        ],
        "relations": [],
    }
    assert not validator.is_valid(bad)


def test_invalid_relation_type_fails():
    schema = extraction_json_schema()
    validator = Draft202012Validator(schema)
    bad = {
        "entities": [],
        "relations": [
            {
                "type": "BRIBES",
                "subject": "A",
                "object": "B",
                "span": "x",
                "confidence": 0.5,
            }
        ],
    }
    assert not validator.is_valid(bad)


def test_missing_required_field_fails():
    schema = extraction_json_schema()
    validator = Draft202012Validator(schema)
    # entity missing `span`
    bad = {"entities": [{"label": "Company", "name": "NVIDIA", "confidence": 0.5}], "relations": []}
    assert not validator.is_valid(bad)


def test_neo4j_ddl_has_vector_index_dim_384():
    ddl = neo4j_ddl()
    joined = "\n".join(ddl)
    assert "CREATE VECTOR INDEX chunk_embedding" in joined
    assert "`vector.dimensions`: 384" in joined
    assert "vector.similarity_function" in joined
    # one uniqueness constraint per entity + a vector index + a fulltext index
    assert sum(1 for s in ddl if s.startswith("CREATE CONSTRAINT")) == len(ENTITY_SPECS)


def test_neo4j_ddl_respects_dim_argument():
    assert "`vector.dimensions`: 512" in "\n".join(neo4j_ddl(512))


def test_schema_card_mentions_supplies_to_and_criticality():
    card = schema_card()
    assert "SUPPLIES_TO" in card
    assert "criticality" in card
    # controlled vocab surfaces in the card
    assert "critical" in card


def test_extraction_result_parses_sample():
    result = ExtractionResult.model_validate(
        {
            "entities": [
                {"label": "Company", "name": "AMD", "span": "AMD competes with NVIDIA.",
                 "confidence": 1.0}
            ],
            "relations": [
                {"type": "COMPETES_WITH", "subject": "AMD", "object": "NVIDIA",
                 "span": "AMD competes with NVIDIA.", "confidence": 1.0}
            ],
        }
    )
    assert result.entities[0].label == "Company"
    assert result.relations[0].type == "COMPETES_WITH"


def test_extraction_result_rejects_non_extractable_label():
    with pytest.raises(ValueError):
        ExtractionResult.model_validate(
            {"entities": [{"label": "Fund", "name": "X", "span": "x"}], "relations": []}
        )


def test_schema_enum_matches_extractable_sets():
    """The JSON Schema enums must be exactly the extractable label/type sets (round-trip)."""
    schema = extraction_json_schema()
    entity_enum = schema["properties"]["entities"]["items"]["properties"]["label"]["enum"]
    rel_enum = schema["properties"]["relations"]["items"]["properties"]["type"]["enum"]
    assert entity_enum == extractable_entity_labels()
    assert rel_enum == extractable_relation_types()


def test_ontology_info_counts_match_specs():
    info = OntologyInfo()
    assert info.entity_count == len(ENTITY_SPECS)
    assert info.relation_count == len(RELATION_SPECS)
    assert info.extractable_entities == len(extractable_entity_labels())
    assert info.extractable_relations == len(extractable_relation_types())
    assert info.vector_dim == 384
