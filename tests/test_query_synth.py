"""M3 — Synthesizer: entitlement wall (Cypher-side filter + withheld_count) and grounded citations."""

from __future__ import annotations

from api.providers.fake import FakeProvider
from api.query.synth import Synthesizer, gather_evidence_chunks
from api.tools.analytics import AnalyticsResult


class _FakeStore:
    def __init__(self, rows):
        self._rows = rows

    def run(self, query, **params):
        return self._rows


def test_gather_evidence_chunks_splits_visible_and_withheld():
    rows = [
        {"chunk_id": "a", "doc_id": "nvda_10k", "text": "public one", "sensitivity": "public", "visible": True},
        {"chunk_id": "b", "doc_id": "nvda_10k", "text": "public two", "sensitivity": "public", "visible": True},
        {"chunk_id": "c", "doc_id": "internal_note", "text": "secret", "sensitivity": "internal", "visible": False},
    ]
    visible, withheld = gather_evidence_chunks(_FakeStore(rows), ["NVIDIA"], ["public"])
    assert len(visible) == 2
    assert withheld == 1
    assert all(v["sensitivity"] == "public" for v in visible)


def test_gather_no_names_returns_empty():
    visible, withheld = gather_evidence_chunks(_FakeStore([]), [], ["public"])
    assert visible == [] and withheld == 0


def test_synthesize_shared_suppliers_answer_and_citations():
    analytics = AnalyticsResult(
        tool="shared_suppliers",
        cypher="MATCH ...",
        params={},
        rows=[{"supplier": "Taiwan Semiconductor Manufacturing",
               "holdings": ["NVIDIA", "Advanced Micro Devices", "Apple", "Broadcom"], "n": 4}],
        graph_paths=[],
    )
    chunks = [{"chunk_id": "nvda_10k_c1", "doc_id": "nvda_10k", "text": "We depend on TSMC.",
               "sensitivity": "public"}]
    synth = Synthesizer(provider=FakeProvider())
    out = synth.synthesize(
        "shared suppliers?",
        plan_fund="Demo Growth Fund",
        analytics=analytics,
        chunks=chunks,
        withheld_count=1,
    )
    assert "Taiwan Semiconductor Manufacturing" in out["answer"]
    assert "NVIDIA" in out["answer"] and "Advanced Micro Devices" in out["answer"]
    assert "1 source(s) withheld" in out["answer"]
    assert out["withheld_count"] == 1
    sources = {c["source"] for c in out["citations"]}
    assert {"analytics", "document"} <= sources
    doc_cite = next(c for c in out["citations"] if c["source"] == "document")
    assert doc_cite["doc_id"] == "nvda_10k"
    assert doc_cite["snippet"]


def test_synthesize_grounded_when_no_evidence():
    synth = Synthesizer(provider=FakeProvider())
    out = synth.synthesize("something obscure", chunks=[], withheld_count=0)
    assert "No matching graph or document evidence" in out["answer"]
    assert out["citations"] == []
