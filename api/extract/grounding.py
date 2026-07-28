"""The grounding gate — deterministic defense against hallucinated edges.

Every extracted entity/relation must carry a `span`: the verbatim sentence from the chunk that
supports it. The gate verifies that span actually appears in the chunk (fuzzy ≥ threshold). Any
triple whose span cannot be grounded is DROPPED before it can reach the graph. This is
provider-agnostic and runs the same whether the extractor was Claude, a fake, or a cassette — it
is the first line of the reliability story and needs no network or key.
"""

from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz

from api.ontology.models import ExtractedEntity, ExtractedRelation, ExtractionResult

DEFAULT_THRESHOLD = 0.9


def _norm(s: str) -> str:
    return " ".join(s.split()).lower()


def span_grounded(span: str, chunk_text: str, threshold: float = DEFAULT_THRESHOLD) -> bool:
    """True if `span` appears in `chunk_text` (exact or fuzzy ≥ threshold)."""
    if not span or not span.strip():
        return False
    s = _norm(span)
    c = _norm(chunk_text)
    if not c:
        return False
    if s in c:
        return True
    # partial_ratio aligns the span against the best-matching substring window of the chunk.
    return (fuzz.partial_ratio(s, c) / 100.0) >= threshold


@dataclass
class DroppedTriple:
    kind: str  # "entity" | "relation"
    label_or_type: str
    detail: str
    span: str
    reason: str = "span_not_grounded"


def filter_extraction(
    result: ExtractionResult, chunk_text: str, threshold: float = DEFAULT_THRESHOLD
) -> tuple[ExtractionResult, list[DroppedTriple]]:
    """Return (grounded result, dropped triples). Dropped triples are kept for the trace drawer
    ("what the system refused to believe")."""
    kept_entities: list[ExtractedEntity] = []
    kept_relations: list[ExtractedRelation] = []
    dropped: list[DroppedTriple] = []

    for e in result.entities:
        if span_grounded(e.span, chunk_text, threshold):
            kept_entities.append(e)
        else:
            dropped.append(DroppedTriple("entity", e.label, e.name, e.span))

    for r in result.relations:
        if span_grounded(r.span, chunk_text, threshold):
            kept_relations.append(r)
        else:
            dropped.append(
                DroppedTriple("relation", r.type, f"{r.subject} -> {r.object}", r.span)
            )

    return ExtractionResult(entities=kept_entities, relations=kept_relations), dropped
