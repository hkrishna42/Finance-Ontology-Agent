"""Ingest never hangs — a timed-out extraction degrades to extracted(0)+error, not an infinite wait.

Offline: a provider whose only LLM call raises a timeout-shaped exception. Proves (a) the per-chunk
loop short-circuits on a systemic timeout instead of hammering every chunk, and (b) the pipeline
emits `extracted`(0) then a terminal `error` and ends the stream cleanly (no written/completed).
"""

from __future__ import annotations

from api.contracts.events import EventType
from api.extract.chunk import Chunk
from api.extract.extract import extract_document
from api.ingest.pipeline import ingest_document
from api.ingest.sources import source_from_text

TEXT = "NVIDIA depends on TSMC for leading-edge fabrication. Packaging capacity is constrained."


class _APITimeoutError(Exception):
    """Stands in for anthropic.APITimeoutError (matched by the 'timeout' name heuristic)."""


class TimingOutProvider:
    def __init__(self) -> None:
        self.calls = 0

    def complete_structured(self, *, role, schema, system, messages, max_tokens=2048,
                            cache_system=False):
        self.calls += 1
        raise _APITimeoutError("request timed out")

    def complete(self, *, role, system, messages, max_tokens=1024):  # pragma: no cover
        raise _APITimeoutError("request timed out")


class FakeStore:
    def run(self, query, **params):  # pragma: no cover - never reached (fails before write)
        return []

    def close(self):
        pass


def test_extract_document_short_circuits_on_systemic_timeout():
    prov = TimingOutProvider()
    chunks = [Chunk(index=i, text=TEXT, start=0, end=len(TEXT)) for i in range(4)]
    try:
        extract_document(TEXT, provider=prov, chunks=chunks)
    except _APITimeoutError:
        pass
    else:  # pragma: no cover
        raise AssertionError("an all-failed extraction should re-raise the systemic error")
    assert prov.calls == 1  # short-circuited after the first timeout, not 4 calls


def test_pipeline_degrades_on_extraction_timeout():
    events = list(
        ingest_document(
            source_from_text(TEXT, doc_id="hang_test", doc_type="10-K", sensitivity="public"),
            store=FakeStore(),
            provider=TimingOutProvider(),
            queue=False,
        )
    )
    types = [e.event for e in events]
    assert types == [
        EventType.JOB_STARTED,
        EventType.CLASSIFIED,
        EventType.PARSED,
        EventType.CHUNKED,
        EventType.EXTRACTED,
        EventType.ERROR,
    ]
    extracted = next(e for e in events if e.event == EventType.EXTRACTED).data
    assert extracted == {"entities": 0, "relations": 0, "dropped": 0}
    err = next(e for e in events if e.event == EventType.ERROR).data
    assert "timed out" in err["message"].lower()
    assert err["where"] == "extract_document"
    # the stream ended cleanly at `error` — no resolved / written / completed after a fatal extraction
    assert EventType.RESOLVED not in types
    assert EventType.WRITTEN not in types
    assert EventType.JOB_COMPLETED not in types
