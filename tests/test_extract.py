"""Extraction wiring tests — offline, with an inline fake provider (no key, no network)."""

from api.extract.extract import extract_chunk, extract_document
from api.providers.base import Completion, Role, StructuredResult, Usage

CHUNK = (
    "We depend on a limited number of foundries, principally Taiwan Semiconductor "
    "Manufacturing Company (TSMC), for the fabrication of our GPUs."
)


class _FakeExtractor:
    """Returns one grounded + one hallucinated triple, so the gate must drop the hallucination."""

    def complete(self, *, role, system, messages, max_tokens=1024):  # pragma: no cover
        return Completion(text="", model="fake", usage=Usage())

    def complete_structured(self, *, role, schema, system, messages, max_tokens=2048,
                            cache_system=False):
        assert role == Role.EXTRACTION
        data = {
            "entities": [
                {"label": "Company", "name": "TSMC",
                 "span": "Taiwan Semiconductor Manufacturing Company (TSMC)", "confidence": 0.95},
                {"label": "Company", "name": "Intel",
                 "span": "Intel is our exclusive foundry", "confidence": 0.9},  # hallucinated
            ],
            "relations": [
                {"type": "SUPPLIES_TO", "subject": "TSMC", "object": "NVIDIA",
                 "qualifiers": {"criticality": "critical"},
                 "span": "principally Taiwan Semiconductor Manufacturing Company (TSMC)",
                 "confidence": 0.9},
            ],
        }
        return StructuredResult(data=data, raw="{...}", model="fake",
                                usage=Usage(input_tokens=100, output_tokens=50))


def test_extract_chunk_applies_grounding_gate():
    ce = extract_chunk(CHUNK, doc_meta="NVIDIA 10-K", provider=_FakeExtractor())
    # TSMC entity grounded, Intel hallucination dropped
    assert [e.name for e in ce.result.entities] == ["TSMC"]
    assert [r.type for r in ce.result.relations] == ["SUPPLIES_TO"]
    assert len(ce.dropped) == 1 and ce.dropped[0].detail == "Intel"
    assert ce.usage.input_tokens == 100 and ce.usage.output_tokens == 50


def test_extract_document_aggregates():
    doc = CHUNK + "\n\n" + CHUNK
    de = extract_document(doc, doc_meta="NVIDIA 10-K", provider=_FakeExtractor(),
                          chunks=None)
    assert len(de.entities) >= 1
    assert all(r.type == "SUPPLIES_TO" for r in de.relations)
    assert de.usage.output_tokens >= 50


def test_stub_provider_returns_valid_empty():
    """With the default stub FakeProvider, extraction returns a schema-valid (possibly empty)
    result and does not raise."""
    ce = extract_chunk(CHUNK, doc_meta="NVIDIA 10-K")
    assert isinstance(ce.result.entities, list)
    assert isinstance(ce.result.relations, list)
