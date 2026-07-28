"""Shared firm-scoping helpers — the single source of truth for "which firm is this read for?".

Every read endpoint (risk, reports, query, graph, documents) resolves the *active* firm (or an
explicit ``?firm=`` override) through these helpers so scoping is defined once and applied
uniformly. Lifted + generalized from the previously-duplicated ``_active_firm_name`` /
``_resolve_funds`` in ``api.modules.risk_routes`` and ``reg_routes``.

Everything here is **best-effort**: a missing/unreadable registry or an unavailable graph degrades
to ``None`` / ``[]`` (an empty, unscoped-or-empty projection) rather than raising a 500.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..config import get_settings
from ..stores.sqlite import connect, init_db
from . import graph_ops
from . import store as firms_store

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps this module import offline
    import sqlite3

    from ..stores.neo4j import Neo4jStore

# DISTINCT names of the Companies the firm's funds HOLD (its issuer holdings).
_FIRM_COMPANIES_CYPHER = (
    "MATCH (f:Fund)-[:MANAGED_BY]->(:Company {name: $firm}) "
    "MATCH (f)-[:HOLDS]->(co:Company) "
    "RETURN DISTINCT co.name AS name ORDER BY name"
)


def active_firm_name(conn: sqlite3.Connection | None = None) -> str | None:
    """The active firm's name from the SQLite registry, or ``None`` (no registry / none active).

    Opens (and closes) its own connection when ``conn`` is not supplied. Best-effort: a missing or
    unreadable registry degrades to ``None`` rather than a 500 — it never raises.
    """
    try:
        if conn is None:
            own = connect(get_settings().sqlite_path)
            try:
                init_db(own)
                active = firms_store.get_active_firm(own)
            finally:
                own.close()
        else:
            init_db(conn)
            active = firms_store.get_active_firm(conn)
    except Exception:  # no registry yet / DB-less boot → treat as "no active firm"
        return None
    return active["name"] if active else None


def resolve_firm(firm: str | None, conn: sqlite3.Connection | None = None) -> str | None:
    """The requested ``firm`` (query param) if truthy, else the active firm; ``None`` if neither."""
    if firm:
        return firm
    return active_firm_name(conn)


def firm_fund_names(store: Neo4jStore, firm: str | None) -> list[str]:
    """Fund names managed by the resolved firm; ``[]`` when no firm resolves (best-effort)."""
    name = resolve_firm(firm)
    if not name:
        return []
    try:
        return graph_ops.firm_funds(store, name)
    except Exception:  # graph unavailable → empty projection, never a 500
        return []


def firm_company_names(store: Neo4jStore, firm: str | None) -> list[str]:
    """DISTINCT names of the Companies the resolved firm's funds HOLD; ``[]`` if none (best-effort)."""
    name = resolve_firm(firm)
    if not name:
        return []
    try:
        rows = store.run(_FIRM_COMPANIES_CYPHER, firm=name)
    except Exception:  # graph unavailable → empty projection, never a 500
        return []
    return [r["name"] for r in rows if r.get("name")]
