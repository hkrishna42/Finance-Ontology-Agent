#!/usr/bin/env python
"""Ingest one document through the M1 pipeline and print the event stream + a summary.

Usage:
    uv run python scripts/ingest_one.py --text "NVIDIA depends on TSMC ..."
    uv run python scripts/ingest_one.py --file path/to/filing.html
    uv run python scripts/ingest_one.py --edgar 0001045810-25-000023
    uv run python scripts/ingest_one.py --ticker NVDA --form 10-K --sections 1A

Runs against the Neo4j pointed to by NEO4J_URI (default bolt://localhost:7687). Extraction uses the
provider selected by PROVIDER_MODE (stub = FakeProvider/$0; full = real Anthropic). Resolution,
the steward, and embeddings always run on the deterministic offline seams.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.config import get_settings  # noqa: E402
from api.contracts.events import EventType  # noqa: E402
from api.ingest.pipeline import ingest_document  # noqa: E402
from api.ingest.sources import (  # noqa: E402
    source_from_edgar,
    source_from_file,
    source_from_text,
)
from api.providers.base import Usage  # noqa: E402

# Public list price per million tokens, by model (input, output, cache-read, cache-write).
# Sonnet-class default; used only to print an *estimate* alongside the exact token counts.
_PRICING: dict[str, tuple[float, float, float, float]] = {
    "claude-sonnet-5": (3.00, 15.00, 0.30, 3.75),
    "claude-haiku-4-5": (1.00, 5.00, 0.10, 1.25),
    "claude-opus-5": (15.00, 75.00, 1.50, 18.75),
}


def estimate_cost(model: str, usage: Usage) -> float:
    rates = _PRICING.get(model)
    if rates is None:
        return 0.0
    inp, out, cache_r, cache_w = rates
    return (
        usage.input_tokens / 1e6 * inp
        + usage.output_tokens / 1e6 * out
        + usage.cache_read_input_tokens / 1e6 * cache_r
        + usage.cache_creation_input_tokens / 1e6 * cache_w
    )


def _build_source(args: argparse.Namespace):
    if args.text:
        return source_from_text(
            args.text, doc_id=args.doc_id, doc_type=args.doc_type, sensitivity=args.sensitivity
        )
    if args.file:
        return source_from_file(
            args.file, doc_id=args.doc_id, doc_type=args.doc_type, sensitivity=args.sensitivity
        )
    sections = [s.strip() for s in args.sections.split(",")] if args.sections else None
    return source_from_edgar(
        accession=args.edgar,
        ticker=args.ticker,
        form=args.form,
        sections=sections,
        doc_id=args.doc_id,
        doc_type=args.doc_type,
        sensitivity=args.sensitivity,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest one document (M1 pipeline).")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--text", help="pasted document text")
    src.add_argument("--file", help="path to a .txt / .html file")
    src.add_argument("--edgar", metavar="ACCESSION", help="EDGAR accession number")
    parser.add_argument("--ticker", help="EDGAR: issuer ticker (with --form)")
    parser.add_argument("--form", help="EDGAR: form type, e.g. 10-K (with --ticker)")
    parser.add_argument("--sections", help="EDGAR: comma-separated Items to keep, e.g. 1,1A,7")
    parser.add_argument("--doc-id", dest="doc_id", help="override the document id")
    parser.add_argument("--doc-type", dest="doc_type", help="override the detected doc type")
    parser.add_argument("--sensitivity", help="override sensitivity (public|internal)")
    parser.add_argument("--no-queue", action="store_true", help="do not queue provisional mentions")
    args = parser.parse_args(argv)

    settings = get_settings()
    source = _build_source(args)

    print(f"# ingest_one  mode={settings.provider_mode}  neo4j={settings.neo4j_uri}")
    print(f"# source={source.source} doc_id={source.doc_id} type={source.doc_type} "
          f"sensitivity={source.sensitivity} chars={source.chars}\n")

    completed: dict | None = None
    error: dict | None = None
    print("event stream:")
    for ev in ingest_document(source, settings=settings, queue=not args.no_queue):
        print(f"  [{ev.seq:>2}] {ev.event.value:<14} {ev.data}")
        if ev.event == EventType.JOB_COMPLETED:
            completed = ev.data
        elif ev.event == EventType.ERROR:
            error = ev.data

    if error is not None:
        print(f"\nERROR: {error.get('message')}")
        return 1
    if completed is None:
        print("\nERROR: pipeline did not complete")
        return 1

    usage = Usage(
        input_tokens=completed.get("input_tokens", 0),
        output_tokens=completed.get("output_tokens", 0),
        cache_read_input_tokens=completed.get("cache_read_tokens", 0),
        cache_creation_input_tokens=completed.get("cache_creation_tokens", 0),
    )
    model = completed.get("extractor_model", "fake")
    cost = estimate_cost(model, usage)

    print("\nsummary:")
    print(f"  Document        {completed['doc_id']}  ({completed['doc_type']}, "
          f"{completed['sensitivity']})")
    print(f"  chunks          {completed['chunks']}")
    print(f"  extracted       {completed['entities']} entities, {completed['relations']} relations")
    print(f"  dropped (gate)  {completed['dropped']}")
    print(f"  resolved        {completed['merged']} merged, {completed['provisional']} provisional")
    print(f"  written         {completed['nodes']} nodes, {completed['edges']} edges, "
          f"{completed['rejected']} rejected")
    print(f"  extractor       {model}")
    print(f"  tokens          in={usage.input_tokens} out={usage.output_tokens} "
          f"cache_read={usage.cache_read_input_tokens} "
          f"cache_write={usage.cache_creation_input_tokens}")
    if model in _PRICING:
        print(f"  est. cost       ${cost:.4f}  (list price for {model})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
