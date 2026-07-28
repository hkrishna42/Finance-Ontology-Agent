"""Offline ingest-pipeline tests (FakeProvider / fake store, $0, no Neo4j).

Proves the M1 acceptance surface without a database:
  * the pipeline emits the full SSE sequence in contract order with monotonic seq;
  * the real grounding gate drops an ungrounded entity + relation;
  * a Document node + Chunk nodes (embedded, 384-dim) + MENTIONS provenance are written;
  * extracted semantic edges are steward-written carrying PROVENANCE_FIELDS + qualifiers;
  * resolution enrichment (CIK) lands on the canonical Company node.
"""

from __future__ import annotations

from api.contracts.events import EventType
from api.ingest.pipeline import ingest_document
from api.ingest.sources import source_from_text
from api.providers.base import StructuredResult, Usage

# One-chunk fixture. The grounded spans below are substrings of this text; the ungrounded ones
# (Ghost Corporation) appear nowhere, so the grounding gate must drop them.
TEXT = (
    "NVIDIA depends on a limited number of foundries, principally Taiwan Semiconductor "
    "Manufacturing (TSMC), for the fabrication of its GPUs. Advanced packaging capacity "
    "remains constrained."
)

_GROUNDED_SPAN = (
    "NVIDIA depends on a limited number of foundries, principally Taiwan Semiconductor "
    "Manufacturing (TSMC), for the fabrication of its GPUs."
)
_UNGROUNDED_SPAN = "Ghost Corporation is a fictional supplier mentioned nowhere in this document."

_CANNED_EXTRACTION = {
    "entities": [
        {"label": "Company", "name": "NVIDIA", "span": _GROUNDED_SPAN,
         "confidence": 0.96, "attributes": {"ticker": "NVDA"}},
        {"label": "Company", "name": "Taiwan Semiconductor Manufacturing",
         "span": _GROUNDED_SPAN, "confidence": 0.93},
        {"label": "RiskFactor", "name": "Supply concentration", "category": "supply_chain",
         "span": "NVIDIA depends on a limited number of foundries", "confidence": 0.88},
        # Ungrounded entity → dropped by the grounding gate.
        {"label": "Company", "name": "Ghost Corporation", "span": _UNGROUNDED_SPAN,
         "confidence": 0.5},
    ],
    "relations": [
        {"type": "SUPPLIES_TO", "subject": "NVIDIA",
         "object": "Taiwan Semiconductor Manufacturing",
         "qualifiers": {"criticality": "critical", "component": "GPU fabrication"},
         "span": _GROUNDED_SPAN, "confidence": 0.95},
        # Ungrounded relation → dropped by the grounding gate.
        {"type": "COMPETES_WITH", "subject": "NVIDIA", "object": "Ghost Corporation",
         "span": _UNGROUNDED_SPAN, "confidence": 0.9},
    ],
}


class CannedExtractor:
    """An `LLMProvider` that returns a fixed extraction for the EXTRACTION role."""

    def __init__(self, data: dict) -> None:
        self.data = data
        self.calls = 0

    def complete_structured(self, *, role, schema, system, messages, max_tokens=2048,
                            cache_system=False) -> StructuredResult:
        self.calls += 1
        return StructuredResult(
            data=self.data,
            raw="{}",
            model="fake-canned",
            usage=Usage(input_tokens=120, output_tokens=40),
            stop_reason="end_turn",
        )

    def complete(self, *, role, system, messages, max_tokens=1024):  # pragma: no cover
        raise NotImplementedError


class _StubResolution:
    def __init__(self, resolved, *, cik=None, lei=None, ticker=None):
        self.resolved = resolved
        self.cik = cik
        self.lei = lei
        self.ticker = ticker


class StubResolver:
    """Deterministic resolver: names in `resolved` pin to a CIK; everything else is provisional."""

    def __init__(self, resolved: set[str] | None = None) -> None:
        self.resolved = resolved or set()

    def resolve(self, name, *, ticker=None, enrich_lei=False, conn=None) -> _StubResolution:
        if name in self.resolved:
            return _StubResolution(True, cik="0001045810", ticker=ticker or "NVDA")
        return _StubResolution(False)


class FakeStore:
    """Records every Cypher call; returns no rows (so the steward treats all triples as new)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def run(self, query: str, **params) -> list:
        self.calls.append((query, params))
        return []

    def close(self) -> None:  # pragma: no cover - lifecycle no-op
        pass

    def queries(self, needle: str) -> list[tuple[str, dict]]:
        return [(q, p) for q, p in self.calls if needle in q]


def _run(store, provider, resolver):
    return list(
        ingest_document(
            source_from_text(TEXT, doc_id="nvda_test", doc_type="10-K", sensitivity="public"),
            store=store,
            provider=provider,
            resolver=resolver,
            queue=False,
        )
    )


def test_pipeline_emits_full_sequence_in_order():
    events = _run(FakeStore(), CannedExtractor(_CANNED_EXTRACTION), StubResolver())
    assert [e.event for e in events] == [
        EventType.JOB_STARTED,
        EventType.CLASSIFIED,
        EventType.PARSED,
        EventType.CHUNKED,
        EventType.EXTRACTED,
        EventType.RESOLVED,
        EventType.WRITTEN,
        EventType.JOB_COMPLETED,
    ]
    # seq is monotonic 0..n and every event carries the same job_id
    assert [e.seq for e in events] == list(range(len(events)))
    assert len({e.job_id for e in events}) == 1


def test_classified_and_chunked_payloads_match_ui_contract():
    events = _run(FakeStore(), CannedExtractor(_CANNED_EXTRACTION), StubResolver())
    by_type = {e.event: e for e in events}
    assert by_type[EventType.CLASSIFIED].data == {"doc_type": "10-K", "sensitivity": "public"}
    assert by_type[EventType.CHUNKED].data["chunks"] == 1
    assert by_type[EventType.PARSED].data["chars"] == len(source_from_text(TEXT).text)


def test_grounding_gate_drops_ungrounded_triples():
    events = _run(FakeStore(), CannedExtractor(_CANNED_EXTRACTION), StubResolver())
    extracted = next(e for e in events if e.event == EventType.EXTRACTED).data
    # 3 grounded entities kept (NVIDIA, TSMC, RiskFactor); Ghost Corporation dropped.
    assert extracted["entities"] == 3
    # 1 grounded relation kept (SUPPLIES_TO); COMPETES_WITH dropped.
    assert extracted["relations"] == 1
    # 2 dropped (one entity + one relation) — surfaced for the trace drawer.
    assert extracted["dropped"] == 2


def test_writes_document_chunks_embeddings_and_mentions():
    store = FakeStore()
    _run(store, CannedExtractor(_CANNED_EXTRACTION), StubResolver())

    assert store.queries("MERGE (d:Document {doc_id: $id})"), "Document node not written"

    chunk_writes = store.queries("MERGE (c:Chunk {chunk_id: $cid})")
    assert chunk_writes, "no Chunk nodes written"
    emb = chunk_writes[0][1]["emb"]
    assert len(emb) == 384 and all(isinstance(x, float) for x in emb), "chunk not embedded (384-d)"
    assert chunk_writes[0][1]["sens"] == "public"

    mentions = store.queries("MERGE (c)-[:MENTIONS]->(n)")
    assert len(mentions) == 3, "expected one MENTIONS per grounded entity"


def test_semantic_edge_written_with_provenance_and_qualifiers():
    store = FakeStore()
    _run(store, CannedExtractor(_CANNED_EXTRACTION), StubResolver())

    edge_writes = [(q, p) for q, p in store.calls if "SET r += $props" in q and "SUPPLIES_TO" in q]
    assert edge_writes, "SUPPLIES_TO edge not written by the steward"
    props = edge_writes[-1][1]["props"]
    for field in ("doc_id", "chunk_id", "page", "span", "confidence", "sensitivity",
                  "extractor_model", "as_of", "reported_at"):
        assert field in props, f"provenance field {field!r} missing from edge"
    assert props["extractor_model"] == "fake-canned"
    assert props["sensitivity"] == "public"
    # relation qualifiers ride along on the edge
    assert props["criticality"] == "critical"
    assert props["component"] == "GPU fabrication"

    written = None
    for ev in ingest_document(  # re-run to grab the WRITTEN event data
        source_from_text(TEXT, doc_id="nvda_test2"),
        store=FakeStore(),
        provider=CannedExtractor(_CANNED_EXTRACTION),
        resolver=StubResolver(),
        queue=False,
    ):
        if ev.event == EventType.WRITTEN:
            written = ev.data
    assert written["edges"] >= 4  # 3 MENTIONS + 1 SUPPLIES_TO
    assert written["rejected"] == 0


def test_resolution_counts_and_cik_enrichment():
    # NVIDIA resolves (→ CIK enrichment); TSMC stays provisional.
    store = FakeStore()
    events = _run(store, CannedExtractor(_CANNED_EXTRACTION), StubResolver({"NVIDIA"}))
    resolved = next(e for e in events if e.event == EventType.RESOLVED).data
    assert resolved == {"merged": 1, "provisional": 1}

    company_writes = [
        p for q, p in store.calls
        if "MERGE (n:Company {name: $kv})" in q and p.get("kv") == "NVIDIA"
    ]
    assert company_writes, "NVIDIA Company node not written"
    assert company_writes[0]["props"].get("cik") == "0001045810", "CIK enrichment missing"


def test_completed_event_carries_usage_and_summary():
    events = _run(FakeStore(), CannedExtractor(_CANNED_EXTRACTION), StubResolver())
    done = next(e for e in events if e.event == EventType.JOB_COMPLETED).data
    assert done["ok"] is True
    assert done["doc_id"] == "nvda_test"
    assert done["input_tokens"] == 120 and done["output_tokens"] == 40
    assert done["extractor_model"] == "fake-canned"
    assert done["chunks"] == 1 and done["relations"] == 1 and done["dropped"] == 2
