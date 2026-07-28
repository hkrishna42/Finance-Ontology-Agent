"""`/firms` REST surface — the firm registry + active-firm selection (Phase 1, contract §E).

`router = APIRouter(prefix="/firms")`; the app wires it in via `include_router` (api/main.py).
SQLite holds the registry (`api.firms.store`); the graph is the source of truth for a firm's funds
and its subtree is cleaned up on delete (`api.firms.graph_ops.delete_firm_graph`). The SQLite
connection is a FastAPI dependency so tests can override it with an in-memory database, mirroring
`api/resolution/routes.py`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from ..config import get_settings
from ..stores.neo4j import Neo4jStore
from ..stores.sqlite import connect, init_db
from . import graph_ops
from . import store as firms_store

router = APIRouter(prefix="/firms", tags=["firms"])


def get_conn() -> Iterator[sqlite3.Connection]:
    """Yield a SQLite connection with the app schema ensured.

    The `firms` table lives in the shared `api.stores.sqlite.SCHEMA` (contract §A), so `init_db`
    creates it idempotently — no per-module init is needed (unlike the resolution queue).
    """
    conn = connect(get_settings().sqlite_path)
    init_db(conn)
    try:
        yield conn
    finally:
        conn.close()


# Annotated dependency alias (avoids a call-in-defaults lint on every handler).
ConnDep = Annotated[sqlite3.Connection, Depends(get_conn)]


def _graph_store() -> Neo4jStore:
    return Neo4jStore(get_settings())


@router.get("")
def get_firms(conn: ConnDep) -> list[dict[str, Any]]:
    """GET /firms -> FirmDict[] (demo first, then active, then name)."""
    return firms_store.list_firms(conn)


@router.get("/active")
def read_active_firm(conn: ConnDep) -> dict[str, Any] | None:
    """GET /firms/active -> FirmDict | null (the currently selected firm, if any)."""
    return firms_store.get_active_firm(conn)


@router.post("/{firm_id}/select")
def select_firm(firm_id: str, conn: ConnDep) -> dict[str, Any]:
    """POST /firms/{firm_id}/select -> FirmDict (make this firm active). 404 if unknown."""
    try:
        return firms_store.set_active_firm(conn, firm_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown firm: {firm_id}") from exc


@router.delete("/{firm_id}")
def remove_firm(firm_id: str, conn: ConnDep) -> dict[str, bool]:
    """DELETE /firms/{firm_id} -> {"deleted": true}.

    Removes the SQLite registry row and the firm's graph subtree (its `Company` node + the `Fund`
    nodes it manages); shared issuer `Company` nodes are left intact. 404 if unknown.
    """
    firm = firms_store.get_firm(conn, firm_id)
    if firm is None:
        raise HTTPException(status_code=404, detail=f"unknown firm: {firm_id}")
    firms_store.delete_firm(conn, firm_id)
    store = _graph_store()
    try:
        graph_ops.delete_firm_graph(store, firm["name"])
    finally:
        store.close()
    return {"deleted": True}
