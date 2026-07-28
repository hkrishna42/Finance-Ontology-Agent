"""Onboarding HTTP surface — firm search + the streaming onboard endpoint.

Two POST routes (declared on a prefix-less `APIRouter`, wired into `api.main` via `include_router`;
both already sit under the Phase-1 `/firms` proxy allow-list, so no proxy change is needed):

  * `POST /firms/search`  — body `{query}` → a JSON list of `FirmCandidate` dicts
    (`discovery.search_firms`, best-effort; 200 with `[]` even when nothing matches).
  * `POST /firms/onboard` — body `{name, cik?, lei?, series:[{series_id, name}]}` → an
    `EventSourceResponse` streaming the `onboard_firm(...)` SSE sequence.

`onboard_firm` is a synchronous generator (blocking Neo4j / SQLite / spine I/O), so — exactly like
`api/ingest/routes.py::_sse_events` — it runs in a worker thread that feeds an `asyncio.Queue`; the
async SSE generator drains the queue, flushing each event the moment its step completes. `discovery`
is imported lazily inside the handler so importing this module (and `api.main`) never fails if the
network-facing module is mid-build.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from ..config import get_settings
from ..contracts.events import EventType, make_event
from ..firms import store as firms_store
from .enrich import enrich_firm
from .pipeline import onboard_firm

router = APIRouter(tags=["onboarding"])

_STREAM_SENTINEL = object()


@router.post("/firms/search")
async def firms_search(request: Request) -> list[dict[str, Any]]:
    """Search EDGAR/GLEIF for firm onboarding candidates. Always 200 (best-effort; `[]` if empty)."""
    body = await _json_object(request)
    query = str(body.get("query") or "").strip()
    if not query:
        return []
    from . import discovery

    settings = get_settings()
    return list(discovery.search_firms(query, limit=int(body.get("limit") or 8), settings=settings))


@router.post("/firms/onboard")
async def firms_onboard(request: Request) -> EventSourceResponse:
    """Onboard a picked firm and stream the pipeline events (SSE)."""
    body = await _json_object(request)
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="onboard request needs a 'name'")
    picked = {
        "name": name,
        "cik": body.get("cik"),
        "lei": body.get("lei"),
        "series": body.get("series") or [],
    }
    return EventSourceResponse(_onboard_sse(picked))


@router.post("/firms/{firm_id}/enrich")
async def firms_enrich(firm_id: str, top: int = 3) -> EventSourceResponse:
    """Semantically enrich an onboarded firm's top holdings and stream the pipeline events (SSE).

    Resolves the firm name from `firm_id` in the SQLite registry (404 if unknown), then streams
    `enrich_firm(firm_name, top=top)` via the same worker-thread/queue bridge as onboarding. `top`
    is an optional query param (default 3, hard-capped downstream at 5).
    """
    settings = get_settings()
    from ..stores.sqlite import connect, init_db

    conn = init_db(connect(settings.sqlite_path))
    try:
        firm = firms_store.get_firm(conn, firm_id)
    finally:
        conn.close()
    if firm is None:
        raise HTTPException(status_code=404, detail=f"unknown firm_id: {firm_id}")
    return EventSourceResponse(_enrich_sse(firm["name"], top))


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="expected a JSON object body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")
    return body


async def _onboard_sse(picked: dict[str, Any]) -> AsyncIterator[dict[str, str]]:
    """Bridge the synchronous `onboard_firm` generator to an async SSE stream via a worker thread."""
    queue: asyncio.Queue[Any] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def worker() -> None:
        try:
            for event in onboard_firm(picked):
                loop.call_soon_threadsafe(queue.put_nowait, event)
        except Exception as exc:  # noqa: BLE001 - the ERROR event was already emitted upstream
            fallback = make_event(
                EventType.ERROR, f"onboard-{picked.get('name', '?')}", message=str(exc)[:400]
            )
            loop.call_soon_threadsafe(queue.put_nowait, fallback)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _STREAM_SENTINEL)

    threading.Thread(target=worker, name="onboard-pipeline", daemon=True).start()

    while True:
        item = await queue.get()
        if item is _STREAM_SENTINEL:
            break
        yield item.to_sse()


async def _enrich_sse(firm_name: str, top: int) -> AsyncIterator[dict[str, str]]:
    """Bridge the synchronous `enrich_firm` generator to an async SSE stream via a worker thread."""
    queue: asyncio.Queue[Any] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def worker() -> None:
        try:
            for event in enrich_firm(firm_name, top=top):
                loop.call_soon_threadsafe(queue.put_nowait, event)
        except Exception as exc:  # noqa: BLE001 - surface any hard failure on the stream
            fallback = make_event(EventType.ERROR, f"enrich-{firm_name}", message=str(exc)[:400])
            loop.call_soon_threadsafe(queue.put_nowait, fallback)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _STREAM_SENTINEL)

    threading.Thread(target=worker, name="enrich-pipeline", daemon=True).start()

    while True:
        item = await queue.get()
        if item is _STREAM_SENTINEL:
            break
        yield item.to_sse()
