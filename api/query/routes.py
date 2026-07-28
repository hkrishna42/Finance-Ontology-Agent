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

try:  # shared firm-scope helper (owner: be-graphdocs). Guarded so /query stays importable and
    from ..firms import scope  # alive if the sibling module is still landing — degrades to global.
except ImportError:  # pragma: no cover - transient during parallel integration only
    scope = None  # type: ignore[assignment]

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
    # Active firm to answer for. When omitted, resolve the registry's active firm. The answer is
    # scoped to this firm so a non-demo firm never surfaces demo citations.
    firm: str | None = None


def resolve_firm_scope(store: Neo4jStore, req: QueryRequest) -> tuple[str | None, list[str] | None]:
    """(active firm name, its held-company names) via the shared scope helper; (None, None) global.

    Best-effort: any failure (incl. the helper not yet present) degrades to the unscoped global
    behaviour rather than erroring — firm scoping should never take the endpoint down.
    """
    if scope is None:
        return None, None
    try:
        firm = scope.resolve_firm(req.firm)
        firm_companies = scope.firm_company_names(store, firm) if firm else None
        return firm, firm_companies
    except Exception:  # noqa: BLE001 - never fail the query on a scoping hiccup
        return None, None


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
    firm, firm_companies = resolve_firm_scope(store, req)
    try:
        qg = QueryGraph(store, settings=get_settings())
        ans = qg.answer(
            req.question,
            entitlements=entitlements,
            mode=mode,
            firm=firm,
            firm_companies=firm_companies,
        )
        return ans.as_dict()
    except Exception as exc:  # noqa: BLE001 - never surface a raw 500 to the UI
        degraded: dict[str, Any] = {
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
        if firm is not None:
            degraded["firm"] = firm
        return degraded
