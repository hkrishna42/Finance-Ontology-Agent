"""M5 Risk Lens HTTP surface (APIRouter, prefix /risk).

The orchestrator wires this into api.main via include_router at merge; this module never touches
api/main.py. Every endpoint returns deterministic Cypher-derived numbers; /risk/{fund}/narrative
additionally returns an LLM-narrated (stub-fixture) briefing.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ..config import get_settings
from ..firms import graph_ops
from ..firms import store as firms_store
from ..providers.base import Role
from ..providers.factory import get_llm_provider
from ..stores.neo4j import Neo4jStore
from ..stores.sqlite import connect, init_db
from . import risk_lens

router = APIRouter(prefix="/risk", tags=["risk"])

_METRICS = {
    "supplier_concentration": lambda sl, _q: risk_lens.supplier_concentration(sl),
    "shared_supplier_clusters": lambda sl, _q: risk_lens.shared_supplier_clusters(sl),
    "second_order_chokepoints": lambda sl, q: risk_lens.second_order_chokepoints(
        sl, k=int(q.get("k", risk_lens.DEFAULT_CHOKEPOINT_K))
    ),
    "risk_heatmap": lambda sl, _q: risk_lens.risk_heatmap(sl),
    "single_source_flags": lambda sl, _q: risk_lens.single_source_flags(sl),
    "coverage": lambda sl, q: risk_lens.coverage(sl, as_of=q.get("as_of")),
}


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
def risk_collection(firm: str | None = Query(default=None)) -> dict[str, Any]:
    """GET /risk -> types.ts RiskData for the active firm (or `?firm=<name>`).

    Scoped to the requested/active firm's funds; no firm or no funds → an empty projection (200).
    """
    store = _store()
    try:
        return risk_lens.risk_collection(store, _resolve_funds(store, firm))
    finally:
        store.close()


@router.get("/metrics")
def list_metrics() -> dict[str, Any]:
    """Discoverability: the metric names + the exact fetch/explain Cypher behind them."""
    return {
        "metrics": list(_METRICS.keys()),
        "fetch_cypher": risk_lens.CYPHER,
        "explain_cypher": risk_lens.EXPLAIN_CYPHER,
        "criticality_multiplier": risk_lens.CRITICALITY_MULTIPLIER,
    }


@router.get("/{fund}/report")
def fund_report(fund: str, as_of: str | None = Query(default=None)) -> dict[str, Any]:
    store = _store()
    try:
        return risk_lens.risk_report(store, fund, as_of=as_of)
    finally:
        store.close()


@router.get("/{fund}/metric/{metric}")
def fund_metric(fund: str, metric: str, k: int | None = Query(default=None),
                as_of: str | None = Query(default=None)) -> dict[str, Any]:
    if metric not in _METRICS:
        raise HTTPException(status_code=404, detail=f"unknown metric: {metric}")
    store = _store()
    try:
        sl = risk_lens.fetch_slice(store, fund)
        q = {"k": k, "as_of": as_of}
        return _METRICS[metric](sl, {k2: v for k2, v in q.items() if v is not None})
    finally:
        store.close()


@router.get("/{fund}/narrative")
def fund_narrative(fund: str, as_of: str | None = Query(default=None)) -> dict[str, Any]:
    store = _store()
    try:
        report = risk_lens.risk_report(store, fund, as_of=as_of)
    finally:
        store.close()
    provider = get_llm_provider(get_settings())
    narration = risk_lens.narrate(provider, report, role=Role.SYNTHESIZER)
    # Graceful degradation (#2): always 200 with the deterministic result; narration may be omitted.
    return {
        "fund": fund,
        "narrative": narration,
        "hhi": report["hhi"],
        "narration_unavailable": narration.get("narration_unavailable"),
    }


@router.get("/compare")
def compare(
    fund_a: str | None = Query(default=None),
    fund_b: str | None = Query(default=None),
    as_of: str | None = Query(default=None),
) -> dict[str, Any]:
    """GET /risk/compare -> pairwise HHI comparison.

    `fund_a`/`fund_b` default to the active firm's first two funds; a firm with fewer than two funds
    (and no explicit override) returns 400.
    """
    store = _store()
    try:
        funds = _resolve_funds(store, None)
        a = fund_a or (funds[0] if len(funds) >= 1 else None)
        b = fund_b or (funds[1] if len(funds) >= 2 else None)
        if not a or not b:
            raise HTTPException(
                status_code=400,
                detail="compare requires two funds; the active firm has fewer than two "
                "(pass ?fund_a=&fund_b= explicitly)",
            )
        return risk_lens.compare_funds(store, a, b, as_of=as_of)
    finally:
        store.close()
