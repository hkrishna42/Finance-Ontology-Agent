"""FIBO alignment glossary (placeholder).

A thin mapping from our ontology labels/relations to FIBO (Financial Industry Business Ontology)
IRIs, kept so downstream export / interop has a single seam to grow into. Intentionally sparse in
M0 — populated as alignment work lands. Not on any Gate-0 critical path.
"""

from __future__ import annotations

# label / relation-type  ->  FIBO IRI (or "" when not yet aligned).
FIBO_IRI: dict[str, str] = {
    # e.g. "Company": "https://spec.edmcouncil.org/fibo/ontology/BE/LegalEntities/LegalPersons/",
}


def fibo_iri(name: str) -> str | None:
    """Return the aligned FIBO IRI for a label/relation, or None if unmapped."""
    iri = FIBO_IRI.get(name)
    return iri or None
