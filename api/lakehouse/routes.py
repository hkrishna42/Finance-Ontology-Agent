"""Lakehouse HTTP surface (APIRouter, prefix /lakehouse). Wired into api.main via include_router.

  * GET /lakehouse/source-systems       -> the medallion source systems + trust scores
  * GET /lakehouse/dim/{dim_table}/{pk} -> a published gold golden-record row + its lineage
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException

from ..config import get_settings
from ..stores.sqlite import connect
from . import store

router = APIRouter(prefix="/lakehouse", tags=["lakehouse"])


def _conn() -> sqlite3.Connection:
    conn = connect(get_settings().sqlite_path)
    store.bootstrap(conn)
    return conn


@router.get("/source-systems")
def source_systems() -> dict[str, Any]:
    conn = _conn()
    try:
        return {"source_systems": store.list_source_systems(conn)}
    finally:
        conn.close()


@router.get("/dim/{dim_table}/{pk}")
def gold_dim(dim_table: str, pk: str) -> dict[str, Any]:
    conn = _conn()
    try:
        row = store.get_gold_dim(conn, dim_table, pk)
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail=f"no gold record for {dim_table}/{pk}")
    return row
