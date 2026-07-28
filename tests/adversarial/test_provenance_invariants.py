"""Provenance invariants — every semantic edge must be traceable back to a source.

The platform's reliability story is that no semantic (LLM-derived) fact exists without provenance.
The mission's target for the current surface: every SUPPLIES_TO / EXPOSED_TO edge carries
`doc_id + span + confidence + sensitivity`.

History: an earlier pass found EXPOSED_TO missing the canonical `span` field (it carried the
verbatim text only under `severity_language`) — filed as bugs/exposed-to-missing-span-provenance.md
and asserted here as a strict-xfail. The graph workstream fixed the seed (EXPOSED_TO now sets
`e.span = x.sev` alongside `severity_language`), so that xfail has been flipped to a normal passing
assertion and the full four-field invariant is now enforced on both edge types.
"""

from __future__ import annotations

import pytest

# Fields the mission requires on every SUPPLIES_TO / EXPOSED_TO edge (now that the seed carries
# `span` on both, the full four-field invariant holds).
CORE_PROVENANCE = ("doc_id", "span", "confidence", "sensitivity")


def _edges_missing(graph, rel_type: str, field: str) -> list[str]:
    """Return a per-edge marker for every `rel_type` edge whose `field` is null/absent."""
    rows = graph.run(
        f"MATCH (a)-[r:{rel_type}]->(b) "
        f"WHERE r.{field} IS NULL "
        "RETURN a.name AS a, b.name AS b, type(r) AS t"
    )
    return [f"{r['a']} -{r['t']}-> {r['b']}" for r in rows]


def _edge_count(graph, rel_type: str) -> int:
    return graph.value(f"MATCH ()-[r:{rel_type}]->() RETURN count(r) AS n")


@pytest.mark.parametrize("rel_type", ["SUPPLIES_TO", "EXPOSED_TO"])
@pytest.mark.parametrize("field", CORE_PROVENANCE)
def test_core_provenance_present_on_every_semantic_edge(graph, rel_type, field):
    """doc_id + span + confidence + sensitivity must be present on every SUPPLIES_TO / EXPOSED_TO edge."""
    assert _edge_count(graph, rel_type) > 0, f"no {rel_type} edges in seed"
    missing = _edges_missing(graph, rel_type, field)
    assert not missing, f"{rel_type} edges missing {field!r}: {missing}"


def test_exposed_to_carries_canonical_span_field(graph):
    """Regression for bugs/exposed-to-missing-span-provenance.md (FIXED): EXPOSED_TO now carries
    the canonical `span` provenance field on every edge, alongside `severity_language`."""
    assert _edge_count(graph, "EXPOSED_TO") > 0
    missing = _edges_missing(graph, "EXPOSED_TO", "span")
    assert not missing, f"EXPOSED_TO edges missing span: {len(missing)} of {_edge_count(graph, 'EXPOSED_TO')}"


def test_exposed_to_span_equals_severity_language(graph):
    """The fix set span == severity_language (the verbatim hedge). Assert they agree on every edge."""
    mismatched = graph.run(
        "MATCH ()-[r:EXPOSED_TO]->() WHERE r.span <> r.severity_language "
        "RETURN count(r) AS n"
    )
    assert mismatched[0]["n"] == 0, "EXPOSED_TO span diverges from severity_language"


def test_provenance_sensitivity_values_are_in_vocabulary(graph):
    """sensitivity on semantic edges must be a declared Sensitivity value (public|internal)."""
    from api.ontology.schema import Sensitivity

    allowed = {s.value for s in Sensitivity}
    for rel_type in ("SUPPLIES_TO", "EXPOSED_TO"):
        vals = {
            r["v"]
            for r in graph.run(
                f"MATCH ()-[r:{rel_type}]->() WHERE r.sensitivity IS NOT NULL "
                "RETURN DISTINCT r.sensitivity AS v"
            )
        }
        assert vals <= allowed, f"{rel_type} has out-of-vocab sensitivity: {vals - allowed}"
