"""M7 Regulatory Reporting Assistant HTTP surface (APIRouter, prefix /reports).

Wired into api.main by the orchestrator via include_router; this module never edits api/main.py.
GET endpoints are read-only (they never mutate the graph or SQLite); persistence of a report pack
(SQLite registry + DERIVED_FROM edges) is done via build_report in code/tests.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Response

from ..config import get_settings
from ..firms import graph_ops
from ..firms import store as firms_store
from ..stores.neo4j import Neo4jStore
from ..stores.sqlite import connect, init_db
from .reg_reports import coverage as coverage_mod
from .reg_reports import report_pack, thirteen_f

router = APIRouter(prefix="/reports", tags=["reports"])


def _store() -> Neo4jStore:
    return Neo4jStore(get_settings())


def _active_firm_name() -> str | None:
    """The active firm's name from the SQLite registry, or None (no registry / none active).

    Best-effort: a missing or unreadable registry degrades to an unscoped (empty) projection rather
    than a 500 (contract §E).
    """
    try:
        conn = connect(get_settings().sqlite_path)
        try:
            init_db(conn)
            active = firms_store.get_active_firm(conn)
        finally:
            conn.close()
    except Exception:  # no registry yet / DB-less boot → treat as "no active firm"
        return None
    return active["name"] if active else None


def _resolve_funds(store: Neo4jStore, firm: str | None) -> list[str]:
    """Funds for the requested firm (query param) or the active firm; [] when neither resolves."""
    name = firm or _active_firm_name()
    if not name:
        return []
    return graph_ops.firm_funds(store, name)


@router.get("")
def reports_collection(firm: str | None = Query(default=None)) -> list[dict[str, Any]]:
    """GET /reports -> types.ts ReportPack[] for the active firm (or `?firm=<name>`).

    Scoped to the requested/active firm's funds; no firm or no funds → an empty list (200).
    """
    store = _store()
    try:
        return report_pack.report_packs(store, _resolve_funds(store, firm))
    finally:
        store.close()


@router.get("/13f/{fund}")
def thirteen_f_json(fund: str) -> dict[str, Any]:
    store = _store()
    try:
        return thirteen_f.build_13f_from_graph(store, fund)
    finally:
        store.close()


@router.get("/13f/{fund}/informationtable.xml")
def thirteen_f_xml(fund: str) -> Response:
    store = _store()
    try:
        res = thirteen_f.build_13f_from_graph(store, fund)
    finally:
        store.close()
    return Response(content=res["xml"], media_type="application/xml")


@router.get("/13f/{fund}/reviewer.csv")
def thirteen_f_csv(fund: str) -> Response:
    store = _store()
    try:
        res = thirteen_f.build_13f_from_graph(store, fund)
    finally:
        store.close()
    return Response(content=res["reviewer_csv"], media_type="text/csv")


@router.get("/coverage/{fund}")
def coverage(fund: str) -> dict[str, Any]:
    store = _store()
    try:
        return coverage_mod.coverage_check(store, fund)
    finally:
        store.close()


@router.get("/pack/{fund}")
def report_pack_json(fund: str, period: str | None = Query(default=None)) -> dict[str, Any]:
    """Build the report pack (read-only): snapshot + stable SHA-256 + rendered HTML."""
    store = _store()
    try:
        res = report_pack.build_report(store, fund, period=period)
    finally:
        store.close()
    # keep the JSON light: HTML length only (fetch the HTML view for the document itself)
    return {
        "report_id": res["report_id"], "title": res["title"], "period": res["period"],
        "sha256": res["sha256"], "snapshot": res["snapshot"], "html_bytes": len(res["html"]),
    }


@router.get("/pack/{fund}/html")
def report_pack_html(fund: str, period: str | None = Query(default=None)) -> Response:
    store = _store()
    try:
        res = report_pack.build_report(store, fund, period=period)
    finally:
        store.close()
    return Response(content=res["html"], media_type="text/html")
