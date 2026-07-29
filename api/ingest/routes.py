"""`POST /documents` — accept a document and stream the ingest pipeline as Server-Sent Events.

Accepts three request shapes on one endpoint (branch on Content-Type):

  * multipart/form-data with a `file` field (+ optional `doc_id`/`doc_type`/`sensitivity`),
  * JSON `{text, doc_id?, doc_type?, sensitivity?, title?}`,
  * JSON `{edgar: {accession | ticker, form, sections?}}`.

The response is an `EventSourceResponse` (sse-starlette) streaming the frozen `SSEEvent` sequence
(`job.started → … → written → job.completed`) so the UI Ingest panel renders real edges as they
land. Defined as an `APIRouter` (prefix `/ingest`); the orchestrator wires it into `api.main` via
`include_router` — this module never edits `api/main.py`.

The pipeline is a synchronous generator (it does blocking Neo4j / embedding I/O), so it runs in a
worker thread that feeds an `asyncio.Queue`; the async SSE generator drains the queue, flushing each
event as soon as the step that produced it completes.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from ..contracts.events import EventType, make_event
from .pipeline import ingest_document
from .sources import (
    IngestSourceError,
    SourceDoc,
    source_from_bytes,
    source_from_edgar,
    source_from_text,
)

router = APIRouter(tags=["ingest"])

_STREAM_SENTINEL = object()


async def _source_from_request(request: Request) -> SourceDoc:
    """Resolve the request body (multipart file OR JSON text/edgar) into a `SourceDoc`."""
    content_type = request.headers.get("content-type", "")

    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(status_code=400, detail="multipart request needs a 'file' field")
        data = await upload.read()
        return source_from_bytes(
            getattr(upload, "filename", "upload") or "upload",
            data,
            doc_id=_form_str(form, "doc_id"),
            doc_type=_form_str(form, "doc_type"),
            sensitivity=_form_str(form, "sensitivity"),
            title=_form_str(form, "title"),
        )

    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=400,
            detail="expected multipart file or a JSON body ({text} or {edgar})",
        ) from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")

    if body.get("edgar"):
        edgar = body["edgar"]
        return source_from_edgar(
            accession=edgar.get("accession"),
            ticker=edgar.get("ticker"),
            form=edgar.get("form"),
            sections=edgar.get("sections"),
            doc_id=edgar.get("doc_id"),
            doc_type=edgar.get("doc_type"),
            sensitivity=edgar.get("sensitivity"),
        )
    if body.get("text"):
        return source_from_text(
            body["text"],
            doc_id=body.get("doc_id"),
            doc_type=body.get("doc_type"),
            sensitivity=body.get("sensitivity"),
            title=body.get("title"),
        )
    raise HTTPException(
        status_code=400, detail="provide a multipart 'file', JSON {text}, or JSON {edgar}"
    )


def _form_str(form: Any, key: str) -> str | None:
    value = form.get(key)
    return value if isinstance(value, str) and value else None


async def _sse_events(source: SourceDoc) -> AsyncIterator[dict[str, str]]:
    """Bridge the synchronous pipeline generator to an async SSE stream via a worker thread.

    A request-scoped SQLite connection is threaded into the pipeline as `queue_conn` so an uploaded
    document's unresolved mentions are parked in the resolution queue for MDM review (the pipeline
    would otherwise open + own its own connection). `connect(check_same_thread=False)` makes it safe
    to hand the connection to the worker thread.
    """
    from ..config import get_settings
    from ..resolution import store as queue_store
    from ..stores.sqlite import connect

    queue: asyncio.Queue[Any] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    queue_conn = connect(get_settings().sqlite_path)
    queue_store.init_resolution_db(queue_conn)

    def worker() -> None:
        # The worker is the sole user of `queue_conn` after hand-off, so it also closes it — no
        # cross-thread close race if the SSE consumer disconnects mid-stream.
        try:
            for event in ingest_document(source, queue_conn=queue_conn):
                loop.call_soon_threadsafe(queue.put_nowait, event)
        except Exception as exc:  # noqa: BLE001 - the ERROR event was already emitted upstream
            fallback = make_event(
                EventType.ERROR, f"ingest-{source.doc_id}", message=str(exc)[:400]
            )
            loop.call_soon_threadsafe(queue.put_nowait, fallback)
        finally:
            queue_conn.close()
            loop.call_soon_threadsafe(queue.put_nowait, _STREAM_SENTINEL)

    threading.Thread(target=worker, name="ingest-pipeline", daemon=True).start()

    while True:
        item = await queue.get()
        if item is _STREAM_SENTINEL:
            break
        yield item.to_sse()


# Primary path per the M1 contract; `/ingest/documents` is an alias that sits in the same namespace
# as the existing `/ingest/demo/stream` demo endpoint.
@router.post("/documents")
@router.post("/ingest/documents")
async def ingest_documents(request: Request) -> EventSourceResponse:
    """Ingest one document and stream its pipeline events (SSE)."""
    try:
        source = await _source_from_request(request)
    except IngestSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return EventSourceResponse(_sse_events(source))
