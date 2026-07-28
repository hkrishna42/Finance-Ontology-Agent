"""Extraction agent — schema-guided per-chunk extraction, then the grounding gate.

`extract_chunk` sends the ontology schema-card (prompt-cached) + document context + the chunk to
the provider under Role.EXTRACTION, gets back schema-valid JSON (Anthropic output_config.format,
or a FakeProvider/cassette in stub), parses it, and passes it through the grounding gate. Agent
code never names a model — the provider routes Role.EXTRACTION to Sonnet 5 (Balanced).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from api.config import get_settings
from api.ontology.models import ExtractionResult
from api.ontology.schema import extraction_json_schema, schema_card
from api.providers.base import LLMProvider, Message, Role, Usage
from api.providers.factory import get_llm_provider

from .chunk import Chunk, chunk_document
from .grounding import DEFAULT_THRESHOLD, DroppedTriple, filter_extraction


@dataclass
class ChunkExtraction:
    result: ExtractionResult
    dropped: list[DroppedTriple]
    usage: Usage
    model: str


@dataclass
class DocumentExtraction:
    per_chunk: list[ChunkExtraction] = field(default_factory=list)

    @property
    def entities(self):
        return [e for ce in self.per_chunk for e in ce.result.entities]

    @property
    def relations(self):
        return [r for ce in self.per_chunk for r in ce.result.relations]

    @property
    def dropped(self):
        return [d for ce in self.per_chunk for d in ce.dropped]

    @property
    def usage(self) -> Usage:
        total = Usage()
        for ce in self.per_chunk:
            total = total + ce.usage
        return total


def _system(doc_meta: str) -> str:
    card = schema_card()
    if doc_meta:
        return f"{card}\n\nDOCUMENT CONTEXT:\n{doc_meta}"
    return card


def extract_chunk(
    chunk_text: str,
    *,
    doc_meta: str = "",
    provider: LLMProvider | None = None,
    threshold: float = DEFAULT_THRESHOLD,
) -> ChunkExtraction:
    provider = provider or get_llm_provider(get_settings())
    messages: list[Message] = [{"role": "user", "content": chunk_text}]
    res = provider.complete_structured(
        role=Role.EXTRACTION,
        schema=extraction_json_schema(),
        system=_system(doc_meta),
        messages=messages,
        cache_system=True,  # prompt-cache the stable schema-card
    )
    parsed = ExtractionResult.model_validate(res.data)
    grounded, dropped = filter_extraction(parsed, chunk_text, threshold)
    return ChunkExtraction(grounded, dropped, res.usage, res.model)


def extract_document(
    text: str,
    *,
    doc_meta: str = "",
    provider: LLMProvider | None = None,
    threshold: float = DEFAULT_THRESHOLD,
    chunks: list[Chunk] | None = None,
) -> DocumentExtraction:
    provider = provider or get_llm_provider(get_settings())
    chunks = chunks if chunks is not None else chunk_document(text)
    out = DocumentExtraction()
    for ch in chunks:
        out.per_chunk.append(
            extract_chunk(ch.text, doc_meta=doc_meta, provider=provider, threshold=threshold)
        )
    return out
