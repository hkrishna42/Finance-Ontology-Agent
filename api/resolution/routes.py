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

# Live queue status → UI ProvisionalEntity.status (types.ts: 'pending'|'merged'|'kept_new').
_STATUS_MAP = {"provisional": "pending", "merged": "merged", "rejected": "kept_new"}


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
    cik: str = Field(..., description="10-digit CIK to pin this mention to")
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


@router.post("/merge")
def merge_provisional(req: MergeRequest, conn: ConnDep) -> dict[str, Any]:
    """Steward action: pin a provisional mention to a CIK/LEI and mark it merged."""
    existing = queue_store.get(conn, req.queue_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"no queue item {req.queue_id}")
    merged = queue_store.merge(
        conn, req.queue_id, cik=req.cik, lei=req.lei, title=req.title
    )
    return {"merged": merged}
