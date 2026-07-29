"""The ingest graph — `ingest_document(...)` composes the merged components and streams events.

It runs the M1 pipeline and YIELDS the frozen `SSEEvent` sequence as it goes:

    job.started → classified → parsed → chunked → extracted → resolved → written → job.completed

and, as side effects, writes to Neo4j:

  * a `Document` node + `Chunk` nodes (each carrying its embedding via `get_embedder`),
  * `MENTIONS` provenance edges (chunk → each grounded entity),
  * the extracted, span-grounded semantic edges — validated/deduped/provenanced by the **steward**
    (`Steward.review` + `GraphWriter.write`, stamping `PROVENANCE_FIELDS`).

Division of labour: only extraction uses the LLM (`get_llm_provider`); chunking, embedding,
resolution, and the steward run on the deterministic offline seams, so ingest cost is bounded to
the single extraction call per chunk. Every seam (provider/embedder/store/resolver/steward) is
injectable, so the whole pipeline runs offline in tests with a fake store.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from ..contracts.events import EventType, SSEEvent, make_event
from ..extract.chunk import Chunk, chunk_document
from ..extract.extract import DocumentExtraction, extract_document
from ..extract.grounding import DEFAULT_THRESHOLD
from ..ontology.models import ExtractedEntity
from ..ontology.schema import entity_spec, relation_spec
from ..providers.base import Usage
from ..steward.steward import (
    CandidateTriple,
    GraphWriter,
    Steward,
    existing_from_store,
)
from .sources import SourceDoc


@dataclass
class IngestResult:
    """Terminal summary of a run (also serialized into the `job.completed` event data)."""

    job_id: str
    doc_id: str
    doc_type: str
    sensitivity: str
    chars: int = 0
    pages: int = 0
    chunks: int = 0
    entities: int = 0
    relations: int = 0
    dropped: int = 0
    merged: int = 0
    provisional: int = 0
    nodes: int = 0
    edges: int = 0
    rejected: int = 0
    extractor_model: str = "fake"
    usage: Usage = field(default_factory=Usage)

    def as_dict(self) -> dict[str, Any]:
        # NB: no `job_id` key — the SSEEvent envelope already carries it (and it would collide with
        # the positional `job_id` when spread into `make_event`).
        return {
            "doc_id": self.doc_id,
            "doc_type": self.doc_type,
            "sensitivity": self.sensitivity,
            "chars": self.chars,
            "pages": self.pages,
            "chunks": self.chunks,
            "entities": self.entities,
            "relations": self.relations,
            "dropped": self.dropped,
            "merged": self.merged,
            "provisional": self.provisional,
            "nodes": self.nodes,
            "edges": self.edges,
            "rejected": self.rejected,
            "extractor_model": self.extractor_model,
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "cache_read_tokens": self.usage.cache_read_input_tokens,
            "cache_creation_tokens": self.usage.cache_creation_input_tokens,
        }


def _chunk_id(doc_id: str, index: int) -> str:
    return f"{doc_id}_c{index + 1}"


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _entity_node_props(e: ExtractedEntity, resolution: Any = None) -> dict[str, Any]:
    """Node properties for an extracted entity: schema-known attributes + resolution enrichment."""
    spec = entity_spec(e.label)
    props: dict[str, Any] = {}
    if e.category and "category" in spec.props:
        props["category"] = e.category
    if e.aliases and "aliases" in spec.props:
        props["aliases"] = list(e.aliases)
    for key, value in e.attributes.items():
        if key in spec.props and value:
            props[key] = value
    if e.label == "Company" and resolution is not None and getattr(resolution, "resolved", False):
        if getattr(resolution, "cik", None):
            props["cik"] = resolution.cik
        if getattr(resolution, "lei", None):
            props["lei"] = resolution.lei
        if getattr(resolution, "ticker", None):
            props["ticker"] = resolution.ticker
    # Agent B (Ontologist): stamp the FIBO OWL grounding on the node. Deterministic (no LLM), so it
    # runs in stub. A single-class instance is OWL-consistent by construction, so a grounded node is
    # reasoning_valid; the full OWL reasoner (Agent C) runs on demand via /fibo/validate + over MDM.
    from ..fibo import grounding as fibo_grounding

    g = fibo_grounding.ground(e.label, category=e.category, attributes=dict(e.attributes or {}))
    if g.grounded:
        props["fibo_class"] = g.curie
        props["fibo_grounded"] = True
        props["reasoning_valid"] = True
    return props


def _infer_label(name: str, name_label: dict[str, str], allowed: tuple[str, ...]) -> str:
    """Pick the entity label for a relation endpoint, honoring the relation's domain/range."""
    lbl = name_label.get(name)
    if lbl in allowed:
        return lbl
    return allowed[0]


def _default_resolver(settings: Any, embedder: Any) -> Any:
    """A deterministic, $0 resolver: real spine + exact/embedding match, but a FAKE adjudicator.

    Keeps entity resolution offline and free (only extraction is allowed to spend tokens); genuinely
    ambiguous mentions fall through to the provisional queue rather than an LLM adjudication call.
    """
    from ..providers.fake import FakeProvider
    from ..resolution.resolver import Resolver

    return Resolver(settings=settings, embedder=embedder, provider=FakeProvider())


def ingest_document(
    source: SourceDoc,
    *,
    settings: Any = None,
    provider: Any = None,
    embedder: Any = None,
    store: Any = None,
    resolver: Any = None,
    steward: Any = None,
    queue_conn: Any = None,
    queue: bool = True,
    threshold: float = DEFAULT_THRESHOLD,
    job_id: str | None = None,
) -> Iterator[SSEEvent]:
    """Run the ingest pipeline for one document, yielding SSE events in contract order.

    Extraction uses `provider` (defaults to `get_llm_provider(settings)` — the ONLY LLM call).
    Resolution/steward/embeddings use deterministic seams. `store` defaults to a real `Neo4jStore`
    (owned + closed here); inject a fake store for offline tests. `queue`/`queue_conn` control
    whether provisional mentions are parked in the SQLite resolution queue for the `/resolve` panel.
    """
    from ..config import get_settings

    settings = settings or get_settings()
    job_id = job_id or f"ingest-{uuid4().hex[:8]}"
    result = IngestResult(
        job_id=job_id,
        doc_id=source.doc_id,
        doc_type=source.doc_type,
        sensitivity=source.sensitivity,
    )

    # Lazily construct the deterministic seams (all offline / $0).
    if embedder is None:
        from ..providers.embeddings import get_embedder

        embedder = get_embedder(settings)
    if provider is None:
        from ..providers.factory import get_llm_provider

        provider = get_llm_provider(settings)
    if resolver is None:
        resolver = _default_resolver(settings, embedder)
    if steward is None:
        from ..providers.fake import FakeProvider

        steward = Steward(provider=FakeProvider())

    own_store = store is None
    if own_store:
        from ..stores.neo4j import Neo4jStore

        store = Neo4jStore(settings)

    own_conn = False
    conn = queue_conn
    if conn is None and queue:
        from ..resolution import store as queue_store
        from ..stores.sqlite import connect

        conn = connect(settings.sqlite_path)
        queue_store.init_resolution_db(conn)
        own_conn = True

    seq = 0

    def emit(event_type: EventType, **data: Any) -> SSEEvent:
        nonlocal seq
        ev = make_event(event_type, job_id, seq=seq, **data)
        seq += 1
        return ev

    try:
        # 1. job.started -----------------------------------------------------------------
        yield emit(
            EventType.JOB_STARTED,
            doc_id=source.doc_id,
            source=source.source,
            title=source.title,
        )

        # 2. classified (deterministic: from input or the keyword heuristic) -------------
        yield emit(
            EventType.CLASSIFIED,
            doc_type=source.doc_type,
            sensitivity=source.sensitivity,
        )

        # 3. parsed ----------------------------------------------------------------------
        result.chars = source.chars
        result.pages = source.pages
        yield emit(EventType.PARSED, pages=source.pages, chars=source.chars)

        # 4. chunked ---------------------------------------------------------------------
        chunks: list[Chunk] = chunk_document(source.text)
        result.chunks = len(chunks)
        yield emit(EventType.CHUNKED, chunks=len(chunks))

        # 5. extracted (the ONE LLM step) — grounding gate already applied inside ---------
        doc_meta = f"doc_id={source.doc_id} type={source.doc_type} title={source.title or ''}"
        extraction: DocumentExtraction = extract_document(
            source.text,
            doc_meta=doc_meta,
            provider=provider,
            threshold=threshold,
            chunks=chunks,
        )
        result.usage = extraction.usage
        result.entities = len(extraction.entities)
        result.relations = len(extraction.relations)
        result.dropped = len(extraction.dropped)
        if extraction.per_chunk:
            result.extractor_model = extraction.per_chunk[0].model
        yield emit(
            EventType.EXTRACTED,
            entities=result.entities,
            relations=result.relations,
            dropped=result.dropped,
        )

        # 6. resolved — pin Company mentions to the SEC/GLEIF spine (deterministic) -------
        company_reps: dict[str, ExtractedEntity] = {}
        for ce in extraction.per_chunk:
            for e in ce.result.entities:
                if e.label == "Company":
                    company_reps.setdefault(e.name, e)
        resolution_map: dict[str, Any] = {}
        for name, rep in company_reps.items():
            res = resolver.resolve(
                name,
                ticker=rep.attributes.get("ticker"),
                enrich_lei=False,
                conn=conn,
            )
            resolution_map[name] = res
            if getattr(res, "resolved", False):
                result.merged += 1
            else:
                result.provisional += 1
        yield emit(
            EventType.RESOLVED,
            merged=result.merged,
            provisional=result.provisional,
        )

        # 7. written — Document + Chunks(+embeddings) + MENTIONS + steward edges ----------
        as_of = source.filing_date or _today()
        counts = _write_graph(
            store=store,
            embedder=embedder,
            source=source,
            chunks=chunks,
            extraction=extraction,
            resolution_map=resolution_map,
            steward=steward,
            as_of=as_of,
            reported_at=_today(),
        )
        result.nodes = counts["nodes"]
        result.edges = counts["edges"]
        result.rejected = counts["rejected"]
        yield emit(
            EventType.WRITTEN,
            nodes=result.nodes,
            edges=result.edges,
            rejected=result.rejected,
        )

        # 8. job.completed ---------------------------------------------------------------
        yield emit(EventType.JOB_COMPLETED, ok=True, **result.as_dict())

    except Exception as exc:  # noqa: BLE001 - surface failures on the stream, not as a 500
        yield emit(EventType.ERROR, message=str(exc)[:400], where="ingest_document")
        raise
    finally:
        if own_conn and conn is not None:
            conn.close()
        if own_store:
            store.close()


def _norm_company(name: str, resolution: Any = None) -> str:
    """Normalized identity for a Company name (reuses the resolver's `normalized` form when present)."""
    if resolution is not None and getattr(resolution, "normalized", None):
        return resolution.normalized
    from ..resolution.normalize import normalize

    return normalize(name)


def _existing_company_name(store: Any, cik: str | None, norm: str | None) -> str | None:
    """The name of an existing Company node matching a resolved CIK, else a matching `norm`."""
    try:
        if cik:
            rows = store.run("MATCH (c:Company {cik: $cik}) RETURN c.name AS name LIMIT 1", cik=str(cik))
            if rows and rows[0].get("name"):
                return rows[0]["name"]
        if norm:
            rows = store.run("MATCH (c:Company {norm: $norm}) RETURN c.name AS name LIMIT 1", norm=norm)
            if rows and rows[0].get("name"):
                return rows[0]["name"]
    except Exception:  # noqa: BLE001 - dedup is best-effort; never break a write
        pass
    return None


def _canonical_company_names(store: Any, resolution_map: dict[str, Any]) -> dict[str, str]:
    """mention name -> canonical merge name for Companies — snap variant mentions onto ONE node.

    Groups the document's Company mentions by resolved CIK (else normalized name); a group's canonical
    name is an existing graph node (matched by CIK or `norm`) when one exists, else the shortest
    mention. So re-ingesting a document that names the same real company differently ("NVIDIA" vs
    "NVIDIA Corporation" -> same CIK) reuses the one node instead of creating a duplicate. The resolver
    already computed the CIK; this uses it as identity where the raw-name MERGE key could not.
    """
    if not resolution_map:
        return {}
    groups: dict[str, list[str]] = {}
    for name, res in resolution_map.items():
        cik = res.cik if (res is not None and getattr(res, "status", "") == "resolved") else None
        gk = f"cik:{cik}" if cik else f"norm:{_norm_company(name, res)}"
        groups.setdefault(gk, []).append(name)
    canon: dict[str, str] = {}
    for gk, names in groups.items():
        cik = gk[4:] if gk.startswith("cik:") else None
        norm = _norm_company(names[0], resolution_map.get(names[0]))
        canonical = _existing_company_name(store, cik, norm) or min(names, key=lambda n: (len(n), n))
        for n in names:
            canon[n] = canonical
    return canon


def _write_graph(
    *,
    store: Any,
    embedder: Any,
    source: SourceDoc,
    chunks: list[Chunk],
    extraction: DocumentExtraction,
    resolution_map: dict[str, Any],
    steward: Steward,
    as_of: str,
    reported_at: str,
) -> dict[str, int]:
    """Deterministic graph write. Returns {nodes, edges, rejected}."""
    # --- Document node ------------------------------------------------------------------
    doc_props = {
        "doc_type": source.doc_type,
        "sensitivity": source.sensitivity,
        "title": source.title or source.doc_id,
    }
    if source.source:  # provenance: how the doc entered (upload / text / edgar), so it's discoverable
        doc_props["source"] = source.source
    if source.accession_no:
        doc_props["accession_no"] = source.accession_no
    if source.filing_date:
        doc_props["filing_date"] = source.filing_date
    if source.url:
        doc_props["url"] = source.url
    store.run("MERGE (d:Document {doc_id: $id}) SET d += $props", id=source.doc_id, props=doc_props)
    nodes = 1

    # --- Chunk nodes (+ embeddings, batched) --------------------------------------------
    vectors = embedder.embed([c.text for c in chunks]) if chunks else []
    for chunk, vec in zip(chunks, vectors, strict=True):
        cid = _chunk_id(source.doc_id, chunk.index)
        store.run(
            "MERGE (c:Chunk {chunk_id: $cid}) "
            "SET c.doc_id = $doc_id, c.text = $text, c.start = $start, c.end = $end, "
            "    c.page = $page, c.sensitivity = $sens, c.embedding = $emb",
            cid=cid,
            doc_id=source.doc_id,
            text=chunk.text,
            start=chunk.start,
            end=chunk.end,
            page=str(chunk.index + 1),
            sens=source.sensitivity,
            emb=[float(x) for x in vec],
        )
    nodes += len(chunks)

    # --- Entity nodes + MENTIONS provenance ---------------------------------------------
    canon_company = _canonical_company_names(store, resolution_map)
    name_label: dict[str, str] = {}
    written_nodes: set[tuple[str, str]] = set()
    mentions_seen: set[tuple[str, str, str]] = set()
    edges = 0
    for chunk, ce in zip(chunks, extraction.per_chunk, strict=True):
        cid = _chunk_id(source.doc_id, chunk.index)
        for e in ce.result.entities:
            name_label.setdefault(e.name, e.label)
            key = entity_spec(e.label).key
            # a Company mention snaps onto its canonical node (dedup by resolved CIK / norm)
            merge_name = canon_company.get(e.name, e.name) if e.label == "Company" else e.name
            node_id = (e.label, merge_name)
            if node_id not in written_nodes:
                written_nodes.add(node_id)
                props = _entity_node_props(
                    e, resolution_map.get(e.name) if e.label == "Company" else None
                )
                if e.label == "Company":
                    props["norm"] = _norm_company(merge_name, resolution_map.get(e.name))
                store.run(
                    f"MERGE (n:{e.label} {{{key}: $kv}}) SET n += $props",
                    kv=merge_name,
                    props=props,
                )
            mention_id = (cid, e.label, merge_name)
            if mention_id not in mentions_seen:
                mentions_seen.add(mention_id)
                store.run(
                    f"MATCH (c:Chunk {{chunk_id: $cid}}), (n:{e.label} {{{key}: $kv}}) "
                    "MERGE (c)-[:MENTIONS]->(n)",
                    cid=cid,
                    kv=merge_name,
                )
                edges += 1
    nodes += len(written_nodes)

    # --- Semantic edges via the steward (domain/range + dedupe + provenance) ------------
    triples = _build_triples(
        source=source,
        chunks=chunks,
        extraction=extraction,
        name_label=name_label,
        canon_company=canon_company,
        as_of=as_of,
        reported_at=reported_at,
    )
    if triples:
        existing = existing_from_store(store, triples)
        review = steward.review(triples, existing=existing)
        write_counts = GraphWriter(store).write(review)
        edges += write_counts["edges"]
        rejected = write_counts["rejected"]
    else:
        rejected = 0

    return {"nodes": nodes, "edges": edges, "rejected": rejected}


def _build_triples(
    *,
    source: SourceDoc,
    chunks: list[Chunk],
    extraction: DocumentExtraction,
    name_label: dict[str, str],
    canon_company: dict[str, str],
    as_of: str,
    reported_at: str,
) -> list[CandidateTriple]:
    """Turn each grounded relation into a provenance-stamped `CandidateTriple` for the steward.

    Company endpoints are rewritten to their canonical merge name (`canon_company`) so the steward's
    edges attach to the same deduped Company node the entity write used — not a variant duplicate.
    """
    triples: list[CandidateTriple] = []
    for chunk, ce in zip(chunks, extraction.per_chunk, strict=True):
        cid = _chunk_id(source.doc_id, chunk.index)
        for r in ce.result.relations:
            spec = relation_spec(r.type)
            subject_label = _infer_label(r.subject, name_label, spec.domain)
            object_label = _infer_label(r.object, name_label, spec.range)
            subject_key = canon_company.get(r.subject, r.subject) if subject_label == "Company" else r.subject
            object_key = canon_company.get(r.object, r.object) if object_label == "Company" else r.object
            provenance = {
                "doc_id": source.doc_id,
                "chunk_id": cid,
                "page": str(chunk.index + 1),
                "span": r.span,
                "confidence": r.confidence,
                "as_of": as_of,
                "reported_at": reported_at,
                "extractor_model": ce.model,
                "sensitivity": source.sensitivity,
            }
            triples.append(
                CandidateTriple(
                    subject_label=subject_label,
                    subject_key=subject_key,
                    rel_type=r.type,
                    object_label=object_label,
                    object_key=object_key,
                    qualifiers=dict(r.qualifiers),
                    provenance=provenance,
                    confidence=r.confidence,
                )
            )
    return triples
