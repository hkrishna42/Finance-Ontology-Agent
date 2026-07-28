"""Pydantic models for parsing/validating LLM extraction output.

These mirror `extraction_json_schema()` and are the typed surface the ingest pipeline works
with after the agent returns. `confidence` is clamped here (Anthropic json_schema can't enforce
numeric ranges); the grounding gate additionally verifies each `span` against the source chunk.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from .schema import extractable_entity_labels, extractable_relation_types


def _pairs_to_dict(v: object) -> dict:
    """Normalize a [{key,value}] list (the Anthropic-structured-output shape) into a dict;
    pass an existing dict (FakeProvider / fixtures) through unchanged."""
    if isinstance(v, list):
        return {
            str(p["key"]): str(p.get("value", ""))
            for p in v
            if isinstance(p, dict) and "key" in p
        }
    return v if isinstance(v, dict) else {}


class ExtractedEntity(BaseModel):
    label: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    category: str | None = None
    attributes: dict[str, str] = Field(default_factory=dict)
    span: str
    confidence: float = 1.0

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, v: float) -> float:
        return max(0.0, min(1.0, v))

    @field_validator("label")
    @classmethod
    def _known_label(cls, v: str) -> str:
        if v not in extractable_entity_labels():
            raise ValueError(f"non-extractable entity label: {v}")
        return v

    @field_validator("attributes", mode="before")
    @classmethod
    def _norm_attributes(cls, v: object) -> dict:
        return _pairs_to_dict(v)


class ExtractedRelation(BaseModel):
    type: str
    subject: str
    object: str
    qualifiers: dict[str, str] = Field(default_factory=dict)
    span: str
    confidence: float = 1.0

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, v: float) -> float:
        return max(0.0, min(1.0, v))

    @field_validator("type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        if v not in extractable_relation_types():
            raise ValueError(f"non-extractable relation type: {v}")
        return v

    @field_validator("qualifiers", mode="before")
    @classmethod
    def _norm_qualifiers(cls, v: object) -> dict:
        return _pairs_to_dict(v)


class ExtractionResult(BaseModel):
    entities: list[ExtractedEntity] = Field(default_factory=list)
    relations: list[ExtractedRelation] = Field(default_factory=list)
