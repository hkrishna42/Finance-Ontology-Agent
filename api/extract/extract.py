"""Extraction agent — schema-guided per-chunk extraction, then the grounding gate.

`extract_chunk` sends the ontology schema-card (prompt-cached) + document context + the chunk to
the provider under Role.EXTRACTION, gets back schema-valid JSON (Anthropic output_config.format,
or a FakeProvider/cassette in stub), parses it, and passes it through the grounding gate. Agent
code never names a model — the provider routes Role.EXTRACTION to Sonnet 5 (Balanced).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from api.config import get_settings
from api.ontology.models import ExtractionResult
from api.ontology.schema import extraction_json_schema, schema_card
from api.providers.base import LLMProvider, Message, Role, Usage
from api.providers.factory import get_llm_provider

from .chunk import Chunk, chunk_document
from .grounding import DEFAULT_THRESHOLD, DroppedTriple, filter_extraction

logger = logging.getLogger(__name__)

# Role.EXTRACTION runs Sonnet-5 with adaptive thinking, which draws from the same token budget as the
# JSON output. The old 2048 cap let a dense chunk's entity/relation JSON truncate mid-string (invalid
# JSON → the whole document aborted). Give extraction ample room for thinking + a rich extraction.
EXTRACTION_MAX_TOKENS = 8192


@dataclass
class ChunkExtraction:
    result: ExtractionResult
    dropped: list[DroppedTriple]
    usage: Usage
    model: str


@dataclass
class DocumentExtraction:
    per_chunk: list[ChunkExtraction] = field(default_factory=list)
    failed_chunks: int = 0  # chunks whose extraction call errored and were skipped (kept aligned)

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
        max_tokens=EXTRACTION_MAX_TOKENS,  # room for adaptive thinking + a rich extraction
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
    last_exc: Exception | None = None
    for i, ch in enumerate(chunks):
        try:
            ce = extract_chunk(ch.text, doc_meta=doc_meta, provider=provider, threshold=threshold)
        except Exception as exc:  # noqa: BLE001 - isolate a bad chunk; don't lose the whole document
            out.failed_chunks += 1
            last_exc = exc
            logger.warning("extraction failed for chunk %d/%d: %s", i + 1, len(chunks), exc)
            ce = ChunkExtraction(ExtractionResult(), [], Usage(), "error")
        out.per_chunk.append(ce)  # kept 1:1 with `chunks` so downstream zip(strict=True) holds
    # Every chunk failing is systemic (bad key / network / schema), not one awkward chunk — surface it.
    if chunks and out.failed_chunks == len(chunks) and last_exc is not None:
        raise last_exc
    return out
