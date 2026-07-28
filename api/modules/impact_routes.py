"""M6 Change Impact HTTP surface (APIRouter, prefix /impact).

Wired into api.main by the orchestrator via include_router; this module never edits api/main.py.
Diffs a v1→v2 fixture pair, propagates through the policy against the live graph, and returns a
cited briefing. /impact/stream emits the frozen `impact` SSEEvent.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Query
from sse_starlette.sse import EventSourceResponse

from ..config import get_settings
from ..providers.factory import get_llm_provider
from ..stores.neo4j import Neo4jStore
from . import change_impact as ci

router = APIRouter(prefix="/impact", tags=["impact"])


def _store() -> Neo4jStore:
    return Neo4jStore(get_settings())


@router.get("")
def impact_collection(
    v1: str = Query(default="v1.json"),
    v2: str = Query(default="v2.json"),
) -> list[dict[str, Any]]:
    """GET /impact -> types.ts ImpactBriefing[] for the committed v1->v2 fixture(s). A read; no LLM."""
    f1, f2 = ci.load_fixture(v1), ci.load_fixture(v2)
    store = _store()
    try:
        resolver = ci.Neo4jResolver(store)
        return ci.impact_briefings(f1, f2, resolver)
    finally:
        store.close()


@router.get("/policy")
def policy() -> dict[str, Any]:
    p = ci.load_policy()
    return {
        "version": p.version,
        "hop_limit": p.hop_limit,
        "rules": [asdict(r) for r in p.rules],
    }


@router.get("/run")
def run(
    v1: str = Query(default="v1.json"),
    v2: str = Query(default="v2.json"),
    narrate: bool = Query(default=True),
) -> dict[str, Any]:
    """Diff the two fixtures, propagate against the live graph, return the cited briefing."""
    f1, f2 = ci.load_fixture(v1), ci.load_fixture(v2)
    store = _store()
    try:
        resolver = ci.Neo4jResolver(store)
        diff = ci.diff_fixtures(f1, f2)
        propagation = ci.propagate(diff, resolver)
    finally:
        store.close()
    provider = get_llm_provider(get_settings()) if narrate else None
    return ci.impact_briefing(diff, propagation, provider=provider)


@router.get("/stream")
async def stream(
    v1: str = Query(default="v1.json"),
    v2: str = Query(default="v2.json"),
) -> EventSourceResponse:
    """SSE: job.started → impact → job.completed for the v1→v2 change."""
    f1, f2 = ci.load_fixture(v1), ci.load_fixture(v2)
    store = _store()
    try:
        resolver = ci.Neo4jResolver(store)
        diff = ci.diff_fixtures(f1, f2)
        propagation = ci.propagate(diff, resolver)
    finally:
        store.close()
    job_id = f"impact-{uuid.uuid4().hex[:8]}"

    async def _events() -> AsyncIterator[dict]:
        from ..contracts.events import EventType, make_event

        yield make_event(EventType.JOB_STARTED, job_id, seq=0, kind="change_impact").to_sse()
        yield ci.make_impact_event(job_id, diff, propagation, seq=1).to_sse()
        yield make_event(EventType.JOB_COMPLETED, job_id, seq=2, ok=True).to_sse()

    return EventSourceResponse(_events())
