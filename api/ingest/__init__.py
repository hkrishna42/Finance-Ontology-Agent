"""Document ingest — upload/paste/EDGAR → ontology-generation pipeline (M1).

This package COMPOSES the already-merged building blocks (it does not reinvent them):

    sources.py   resolve an input (file | text | EDGAR) → a normalized `SourceDoc`
    pipeline.py  `ingest_document(...)` — chunk → extract (grounded) → resolve → steward-write,
                 yielding the frozen `SSEEvent` sequence as it goes
    routes.py    `POST /documents` — accepts the three input shapes and streams the SSE events

Only extraction touches the LLM (via `get_llm_provider`); resolution/steward/embeddings run on
the deterministic offline seams so ingest cost is bounded to the extraction call.
"""

from __future__ import annotations

from .pipeline import IngestResult, ingest_document
from .sources import (
    IngestSourceError,
    SourceDoc,
    source_from_bytes,
    source_from_edgar,
    source_from_file,
    source_from_text,
    strip_html,
)

__all__ = [
    "IngestResult",
    "IngestSourceError",
    "SourceDoc",
    "ingest_document",
    "source_from_bytes",
    "source_from_edgar",
    "source_from_file",
    "source_from_text",
    "strip_html",
]
