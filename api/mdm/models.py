"""MDM data model — match results, per-attribute survivorship decisions, and the golden record."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MatchResult:
    """The outcome of ontology-driven entity matching across a cluster of source records."""

    confidence: float  # 0..1
    threshold: float
    resolved: bool  # confidence >= threshold
    matched_record_ids: list[str]
    blocking_keys: list[str]
    method: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "confidence": self.confidence,
            "confidence_pct": round(self.confidence * 100, 1),
            "threshold": self.threshold,
            "resolved": self.resolved,
            "matched_record_ids": self.matched_record_ids,
            "blocking_keys": self.blocking_keys,
            "method": self.method,
        }


@dataclass
class SurvivorshipDecision:
    """Why one source value won a single attribute — the auditable golden-record trail."""

    attribute: str
    rule: str  # display label, e.g. "Curated Gold System Wins"
    winning_source: str  # source system id (or "ontology rule engine")
    winning_value: Any
    rationale: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "attribute": self.attribute,
            "rule": self.rule,
            "winning_source": self.winning_source,
            "winning_value": self.winning_value,
            "rationale": self.rationale,
        }


@dataclass
class GoldenRecord:
    """A single, canonical, auditable master record reconciled from many source systems."""

    entity_type: str
    dim_table: str
    pk: str
    canonical_attributes: dict[str, Any]
    retained_alternate_ids: dict[str, list[str]] = field(default_factory=dict)
    fibo_class: str | None = None
    fibo_curie: str | None = None
    decisions: list[SurvivorshipDecision] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "dim_table": self.dim_table,
            "pk": self.pk,
            "lakehouse_provenance": f"Lakehouse.{self.dim_table} (PK: {self.pk})",
            "canonical_attributes": self.canonical_attributes,
            "retained_alternate_ids": self.retained_alternate_ids,
            "fibo_class": self.fibo_class,
            "fibo_curie": self.fibo_curie,
            "per_attribute": [d.as_dict() for d in self.decisions],
        }
