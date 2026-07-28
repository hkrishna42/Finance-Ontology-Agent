"""M2 / steward — domain/range validation, dedupe/corroboration, functional conflict, entailment.

Fully offline. The low-confidence entailment "approve" path uses the committed fixtures/fake result
(Role.STEWARD); the "reject" path uses FakeProvider's conservative default (no fixture).
"""

from __future__ import annotations

from api.providers.fake import FakeProvider
from api.steward.steward import (
    CandidateTriple,
    ExistingEdge,
    GraphWriter,
    Steward,
    decisions_as_dicts,
)


def _triple(sl, sk, rt, ol, ok, *, conf=1.0, span="span", doc="d1", **qual) -> CandidateTriple:
    return CandidateTriple(
        subject_label=sl,
        subject_key=sk,
        rel_type=rt,
        object_label=ol,
        object_key=ok,
        qualifiers=qual,
        provenance={
            "span": span,
            "doc_id": doc,
            "as_of": "2026-05-31",
            "confidence": conf,
            "sensitivity": "public",
            "extractor_model": "test",
        },
        confidence=conf,
    )


def test_bad_domain_and_range_are_rejected():
    steward = Steward(provider=FakeProvider())
    triples = [
        _triple("Fund", "F", "SUPPLIES_TO", "Company", "X"),  # domain must be Company
        _triple("Company", "NVIDIA", "EXPOSED_TO", "Company", "Y"),  # range must be RiskFactor
        _triple("Company", "A", "NOT_A_REL", "Company", "B"),  # unknown relation
        _triple("Company", "NVIDIA", "SUPPLIES_TO", "Company", "TSMC", criticality="critical"),  # ok
    ]
    result = steward.review(triples)
    actions = [d.action for d in result.decisions]
    assert actions == ["reject", "reject", "reject", "insert"]
    reasons = [d.reason for d in result.rejected]
    assert any(r.startswith("bad_domain") for r in reasons)
    assert any(r.startswith("bad_range") for r in reasons)
    assert "unknown_relation" in reasons


def test_dedupe_corroborates_and_merges_provenance():
    steward = Steward(provider=FakeProvider())
    triples = [
        _triple("Company", "NVIDIA", "SUPPLIES_TO", "Company", "TSMC", doc="d1", criticality="critical"),
        _triple("Company", "NVIDIA", "SUPPLIES_TO", "Company", "TSMC", doc="d2", criticality="critical"),
        _triple("Company", "NVIDIA", "SUPPLIES_TO", "Company", "TSMC", doc="d3", criticality="critical"),
    ]
    result = steward.review(triples)
    assert [d.action for d in result.decisions] == ["insert", "corroborate", "corroborate"]
    assert result.decisions[-1].corroboration == 3
    # only the accepted set flows to the impact worker
    assert len(result.facts_changed) == 3


def test_dedupe_against_existing_graph_edges():
    steward = Steward(provider=FakeProvider())
    existing = {
        ("Company", "NVIDIA", "SUPPLIES_TO"): [ExistingEdge("TSMC", corroboration=1, sources=["d0"])]
    }
    result = steward.review(
        [_triple("Company", "NVIDIA", "SUPPLIES_TO", "Company", "TSMC", doc="d1", criticality="critical")],
        existing=existing,
    )
    assert result.decisions[0].action == "corroborate"
    assert result.decisions[0].corroboration == 2


def test_functional_conflict_is_flagged_not_overwritten():
    steward = Steward(provider=FakeProvider())
    existing = {("Fund", "F1", "MANAGED_BY"): [ExistingEdge("FirmA", 1, ["d0"])]}
    result = steward.review(
        [_triple("Fund", "F1", "MANAGED_BY", "Company", "FirmB")], existing=existing
    )
    d = result.decisions[0]
    assert d.action == "conflict"
    assert d.conflict is True
    assert result.facts_changed[0]["conflict"] is True


def test_low_confidence_triggers_entailment_and_can_reject():
    # No fixture matches this span → FakeProvider minimal instance → entailed:false → reject.
    steward = Steward(provider=FakeProvider())
    result = steward.review(
        [_triple("Company", "Acme", "COMPETES_WITH", "Company", "Beta", conf=0.3, span="unrelated")]
    )
    d = result.decisions[0]
    assert d.action == "reject"
    assert d.reason == "not_entailed"
    assert d.entailment_checked is True


def test_low_confidence_entailment_approved_via_fixture():
    # Matches the committed fixtures/fake steward entailment result → entailed:true → insert.
    steward = Steward(provider=FakeProvider())
    triple = CandidateTriple(
        subject_label="Company",
        subject_key="Contoso Foundry",
        rel_type="SUPPLIES_TO",
        object_label="Company",
        object_key="Fabrikam Systems",
        qualifiers={"criticality": "critical"},
        provenance={
            "span": "Contoso Foundry is the sole supplier of critical wafers to Fabrikam Systems.",
            "doc_id": "doc_entail",
            "as_of": "2026-05-31",
            "sensitivity": "public",
        },
        confidence=0.4,
    )
    result = steward.review([triple])
    d = result.decisions[0]
    assert d.action == "insert"
    assert d.entailment_checked is True


def test_high_confidence_skips_entailment():
    steward = Steward(provider=FakeProvider())
    result = steward.review(
        [_triple("Company", "NVIDIA", "SUPPLIES_TO", "Company", "TSMC", conf=0.95, criticality="critical")]
    )
    assert result.decisions[0].entailment_checked is False


def test_facts_changed_callback_fires():
    steward = Steward(provider=FakeProvider())
    captured: list[dict] = []
    steward.review(
        [_triple("Company", "NVIDIA", "SUPPLIES_TO", "Company", "TSMC", criticality="critical")],
        on_facts_changed=captured.extend,
    )
    assert len(captured) == 1
    assert captured[0]["op"] == "insert"
    assert captured[0]["rel_type"] == "SUPPLIES_TO"


def test_edge_props_carry_provenance_and_bitemporal_tags():
    steward = Steward(provider=FakeProvider())
    result = steward.review(
        [_triple("Company", "NVIDIA", "SUPPLIES_TO", "Company", "TSMC", criticality="critical")]
    )
    writer = GraphWriter(store=None)
    props = writer._edge_props(result.decisions[0], now="2026-07-27T00:00:00+00:00")
    # PROVENANCE_FIELDS present on the write
    for f in ("doc_id", "span", "confidence", "as_of", "sensitivity", "extractor_model"):
        assert f in props
    # qualifiers + reconciliation + bitemporal
    assert props["criticality"] == "critical"
    assert props["corroboration"] == 1
    assert props["conflict"] is False
    assert props["valid_from"] == "2026-05-31"
    assert props["recorded_at"] == "2026-07-27T00:00:00+00:00"


def test_rejected_triples_are_kept_and_serializable():
    steward = Steward(provider=FakeProvider())
    result = steward.review([_triple("Fund", "F", "SUPPLIES_TO", "Company", "X")])
    assert len(result.rejected) == 1
    rows = decisions_as_dicts(result)
    assert rows[0]["action"] == "reject"
    assert rows[0]["accepted"] is False
    assert rows[0]["triple"]["subject_label"] == "Fund"
