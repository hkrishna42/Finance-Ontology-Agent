"""`/resolve` API — the provisional queue + steward merge action.

Defined as an `APIRouter` (prefix `/resolve`); the orchestrator wires it in via `include_router`.
The SQLite connection is a FastAPI dependency so tests can override it with an in-memory database.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..config import get_settings
from ..stores.sqlite import connect
from . import store as queue_store
from .resolver import Resolver

router = APIRouter(prefix="/resolve", tags=["resolve"])

_DEMO_FIXTURE = (
    Path(__file__).resolve().parents[2] / "fixtures" / "resolution" / "demo_provisional.json"
)

# Live queue status → UI ProvisionalEntity.status (types.ts: 'pending'|'merged'|'kept_new'|'rejected').
_STATUS_MAP = {
    "provisional": "pending",
    "merged": "merged",
    "promoted": "kept_new",
    "rejected": "rejected",
}


def get_conn() -> Iterator[sqlite3.Connection]:
    """Yield a SQLite connection with the resolution queue table ensured (overridable in tests)."""
    conn = connect(get_settings().sqlite_path)
    queue_store.init_resolution_db(conn)
    try:
        yield conn
    finally:
        conn.close()


# FastAPI dependency alias (Annotated form avoids a call-in-defaults lint on every handler).
ConnDep = Annotated[sqlite3.Connection, Depends(get_conn)]


class ResolveRequest(BaseModel):
    name: str
    ticker: str | None = None
    enrich_lei: bool = False


class MergeRequest(BaseModel):
    queue_id: int
    cik: str | None = Field(None, description="10-digit CIK to pin this mention to (optional)")
    lei: str | None = None
    title: str | None = None
    canonical_key: str | None = Field(
        None,
        description="Merge key of the surviving canonical node; when set (and different from the "
        "mention) the duplicate node's edges are repointed onto it in Neo4j and the duplicate is "
        "removed. Omit for a SQLite-only pin (the legacy behaviour).",
    )
    canonical_label: str | None = Field(
        None, description="Entity label of the canonical node (defaults to the queue row's label)."
    )


class QueueAction(BaseModel):
    """Body for the reject / promote steward actions."""

    queue_id: int
    cik: str | None = None
    lei: str | None = None
    title: str | None = None


@lru_cache(maxsize=1)
def _demo_provisional() -> list[dict[str, Any]]:
    """Committed demo provisional entities (returned when the live queue is empty)."""
    try:
        return json.loads(_DEMO_FIXTURE.read_text())
    except (OSError, ValueError):
        return []


def _candidate_to_ui(c: dict[str, Any]) -> dict[str, Any]:
    """Normalize a stored candidate (resolver Candidate or already-UI shape) to ResolutionCandidate."""
    if "existing_id" in c:  # already UI-shaped (demo fixture / ingest)
        return c
    title = c.get("title") or c.get("name") or ""
    return {
        "existing_id": c.get("existing_id") or title or c.get("cik") or "",
        "name": title,
        "label": c.get("label", "Company"),
        "score": round(float(c.get("score", 0.0) or 0.0), 4),
        "reason": c.get("reason") or f"embedding cosine {round(float(c.get('score', 0.0) or 0.0), 2)}",
    }


def _row_to_provisional_entity(row: dict[str, Any]) -> dict[str, Any]:
    """Map a live queue row (store._row_to_dict) → types.ts ProvisionalEntity."""
    return {
        "id": f"prov-{row['id']}",
        "label": row.get("label") or "Company",
        "name": row.get("mention", ""),
        "aliases": row.get("aliases") or [],
        "span": row.get("span") or "",
        "doc_id": row.get("doc_id") or "",
        "chunk_id": row.get("chunk_id") or "",
        "confidence": float(row.get("confidence", 0.0) or 0.0),
        "candidates": [_candidate_to_ui(c) for c in (row.get("candidates") or [])],
        "status": _STATUS_MAP.get(row.get("status", "provisional"), "pending"),
    }


@router.get("")
def resolution_queue(conn: ConnDep) -> list[dict[str, Any]]:
    """UI contract: the provisional-entity queue as a bare `ProvisionalEntity[]`.

    Returns the live queue when it has entries; otherwise the committed demo fixture so the panel is
    non-empty and the merge action is demoable before any real ingest has run.
    """
    rows = queue_store.list_queue(conn, status="provisional")
    if rows:
        return [_row_to_provisional_entity(r) for r in rows]
    return _demo_provisional()


@router.post("/")
def resolve_mention(req: ResolveRequest, conn: ConnDep) -> dict[str, Any]:
    """Resolve a single mention; unresolved mentions are queued as provisional."""
    resolver = Resolver(settings=get_settings())
    res = resolver.resolve(
        req.name, ticker=req.ticker, enrich_lei=req.enrich_lei, conn=conn
    )
    return res.as_dict()


@router.get("/provisional")
def list_provisional(conn: ConnDep) -> dict[str, Any]:
    """List all mentions awaiting a steward decision."""
    items = queue_store.list_queue(conn, status="provisional")
    return {"count": len(items), "items": items}


@router.get("/queue")
def list_all(conn: ConnDep) -> dict[str, Any]:
    """List the full queue (any status) for audit."""
    items = queue_store.list_queue(conn, status=None)
    return {"count": len(items), "items": items}


def _repoint_graph(
    *, label: str, dup_key: str, canonical_key: str, cik: str | None, lei: str | None
) -> dict[str, Any]:
    """Open a Neo4jStore and repoint the duplicate mention node onto the canonical node.

    Best-effort: any Neo4j error is caught and surfaced in the response so a steward merge never
    500s just because the graph is unreachable (the SQLite audit record is still written). Offline
    tests inject a fake store via `_REPOINT_STORE`.
    """
    from .graph_reconcile import repoint_entity

    extra = {k: v for k, v in {"cik": cik, "lei": lei}.items() if v}
    store = _REPOINT_STORE
    own = store is None
    if own:
        from ..stores.neo4j import Neo4jStore

        store = Neo4jStore(get_settings())
    try:
        return repoint_entity(
            store, label=label, dup_key=dup_key, canonical_key=canonical_key, extra_props=extra
        )
    finally:
        if own:
            store.close()


# Test seam: inject a fake store so the repoint path is exercised offline without a live Neo4j.
_REPOINT_STORE: Any = None


@router.post("/merge")
def merge_provisional(req: MergeRequest, conn: ConnDep) -> dict[str, Any]:
    """Steward action: merge a provisional mention onto a canonical entity.

    Always records the decision in the SQLite queue (audit). When `canonical_key` is supplied and
    differs from the mention, it *also* reconciles Neo4j: the duplicate mention node's edges are
    repointed onto the canonical node and the duplicate removed (no APOC).
    """
    existing = queue_store.get(conn, req.queue_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"no queue item {req.queue_id}")

    graph: dict[str, Any] | None = None
    canonical = (req.canonical_key or "").strip()
    if canonical and canonical != existing.get("mention"):
        label = req.canonical_label or existing.get("label") or "Company"
        try:
            graph = _repoint_graph(
                label=label,
                dup_key=existing.get("mention", ""),
                canonical_key=canonical,
                cik=req.cik,
                lei=req.lei,
            )
        except Exception as exc:  # noqa: BLE001 - never fail the audit write on a graph hiccup
            graph = {"error": str(exc)[:300], "moved": 0, "dup_deleted": False}

    merged = queue_store.merge(
        conn, req.queue_id, cik=req.cik, lei=req.lei, title=req.title or canonical or None
    )
    return {"merged": merged, "graph": graph}


@router.post("/promote")
def promote_provisional(req: QueueAction, conn: ConnDep) -> dict[str, Any]:
    """Steward action: keep a provisional mention as its own new canonical node (no repoint)."""
    existing = queue_store.get(conn, req.queue_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"no queue item {req.queue_id}")
    promoted = queue_store.promote(
        conn, req.queue_id, cik=req.cik, lei=req.lei, title=req.title
    )
    return {"promoted": promoted}


@router.post("/reject")
def reject_provisional(req: QueueAction, conn: ConnDep) -> dict[str, Any]:
    """Steward action: reject a provisional mention (marked rejected, kept for audit)."""
    existing = queue_store.get(conn, req.queue_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"no queue item {req.queue_id}")
    rejected = queue_store.reject(conn, req.queue_id)
    return {"rejected": rejected}
