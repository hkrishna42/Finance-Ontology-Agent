"""`/query` API — the M3 question-answering endpoint (UI contract).

Request (web/src/api.ts): `{question, mode, entitlement_wall}` where `mode ∈ 'graph'|'side_by_side'`
and `entitlement_wall: bool`. The wall maps to sensitivity scope:
    ON  → entitlements = ['public']              (internal hidden)
    OFF → entitlements = ['public','internal']   (internal included)
Legacy `entitlements: list[str]` and internal modes ('auto'/'graph_only'/'vector_only') are still
accepted. Response is `QueryResponse` (see graph.QueryAnswer.as_dict).

The handler NEVER returns a 500: provider failures degrade inside the pipeline, and any unexpected
error is caught here and returned as a graceful 200 with a `narration_unavailable` note.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..config import get_settings
from ..stores.neo4j import Neo4jStore
from .graph import QueryGraph

router = APIRouter(prefix="/query", tags=["query"])

_UI_MODES = {"graph", "side_by_side"}
_LEGACY_MODES = {"auto", "graph_only", "vector_only"}


class QueryRequest(BaseModel):
    question: str
    # UI: 'graph' | 'side_by_side'. Accept a free str (not Literal) so an unexpected value degrades
    # instead of 422-ing; unknown values are normalized to 'graph'.
    mode: str = "graph"
    entitlement_wall: bool | None = None
    entitlements: list[str] | None = Field(default=None)


def resolve_entitlements(req: QueryRequest) -> list[str]:
    """Map the UI wall toggle (or an explicit list) to a sensitivity scope."""
    if req.entitlements is not None:
        return req.entitlements
    if req.entitlement_wall is None:
        return ["public"]  # default: wall ON (safest)
    return ["public"] if req.entitlement_wall else ["public", "internal"]


def _normalize_mode(mode: str) -> str:
    return mode if mode in (_UI_MODES | _LEGACY_MODES) else "graph"


def get_store() -> Iterator[Neo4jStore]:
    """Yield a Neo4j store for the request (overridable in tests)."""
    store = Neo4jStore(get_settings())
    try:
        yield store
    finally:
        store.close()


StoreDep = Annotated[Neo4jStore, Depends(get_store)]


@router.post("")
def run_query(req: QueryRequest, store: StoreDep) -> dict[str, Any]:
    """Route → analytics|cypher+vector → synthesize; entitlement-filtered, cited answer.

    Graceful by construction: analytics/Cypher answer without the LLM, and provider failures never
    escalate to a 500.
    """
    mode = _normalize_mode(req.mode)
    ui_mode = "side_by_side" if mode == "side_by_side" else "graph"
    entitlements = resolve_entitlements(req)
    try:
        qg = QueryGraph(store, settings=get_settings())
        ans = qg.answer(req.question, entitlements=entitlements, mode=mode)
        return ans.as_dict()
    except Exception as exc:  # noqa: BLE001 - never surface a raw 500 to the UI
        return {
            "question": req.question,
            "mode": ui_mode,
            "answer": "The query service is temporarily degraded and could not complete this "
            "request. Please retry.",
            "citations": [],
            "graph_paths": [],
            "plan": {"strategy": "degraded", "steps": []},
            "withheld_count": 0,
            "source": "error",
            "narration_unavailable": str(exc)[:200],
        }
