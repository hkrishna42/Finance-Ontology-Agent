"""SQLite firm registry — the multi-firm control table behind the header firm selector.

Pure stdlib ``sqlite3``. Persists one row per firm (the demo firm plus any onboarded firms),
tracks which firm is *active* (the single-active invariant: at most one row has ``is_active=1``),
and mirrors the funds each firm manages so the UI can show a fund count without touching Neo4j.

The ``firms`` table itself lives in ``api.stores.sqlite.SCHEMA`` (auto-created by ``--init`` /
``init_db``); this module only reads and writes it. Row ↔ ``FirmDict`` conversion inflates the two
JSON columns (``identifiers_json`` → dict, ``funds_json`` → list).
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, TypedDict

# The fixed demo firm already present in the committed graph/snapshot (see the Phase-1 contract).
DEMO_FIRM_NAME = "Demo Investment Management"
DEMO_FUNDS: list[dict[str, str]] = [
    {"name": "Demo Growth Fund", "series_id": "S000090001",
     "cik": "0009000001", "lei": "DEMOGROWTHFUND000001"},
    {"name": "Demo Focused Growth Fund", "series_id": "S000090002",
     "cik": "0009000002", "lei": "DEMOFOCUSEDGRWTH0001"},
]


class FirmDict(TypedDict):
    """The API/UI shape for one firm (JSON columns inflated to native objects)."""

    firm_id: str
    name: str
    lei: str | None
    cik: str | None
    status: str
    source: str
    is_active: bool
    fund_count: int
    funds: list[dict[str, Any]]
    identifiers: dict[str, Any]
    created_at: str
    updated_at: str


def slugify(name: str) -> str:
    """Lowercase; runs of non-alphanumerics collapse to a single ``_``; strip leading/trailing ``_``."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _row_to_firm(row: sqlite3.Row) -> FirmDict:
    funds = json.loads(row["funds_json"] or "[]")
    identifiers = json.loads(row["identifiers_json"] or "{}")
    return FirmDict(
        firm_id=row["firm_id"],
        name=row["name"],
        lei=row["lei"],
        cik=row["cik"],
        status=row["status"],
        source=row["source"],
        is_active=bool(row["is_active"]),
        fund_count=len(funds),
        funds=funds,
        identifiers=identifiers,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _any_active(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM firms WHERE is_active = 1 LIMIT 1").fetchone() is not None


def _activate(conn: sqlite3.Connection, firm_id: str) -> None:
    """Enforce the single-active invariant: clear every active flag, then set this one."""
    conn.execute("UPDATE firms SET is_active = 0 WHERE is_active = 1")
    conn.execute("UPDATE firms SET is_active = 1 WHERE firm_id = ?", (firm_id,))


def upsert_firm(
    conn: sqlite3.Connection,
    *,
    name: str,
    lei: str | None = None,
    cik: str | None = None,
    status: str = "ready",
    source: str = "edgar",
    identifiers: dict[str, Any] | None = None,
    funds: list[dict[str, Any]] | None = None,
    activate: bool = False,
) -> str:
    """Insert (or update by ``name``) a firm row and return its firm_id (=``slugify(name)``).

    Preserves ``created_at`` on update, always stamps ``updated_at``, and makes this firm the active
    one when ``activate`` is set *or* no firm is currently active (bootstrapping the invariant).
    """
    firm_id = slugify(name)
    identifiers_json = json.dumps(identifiers or {})
    funds_json = json.dumps(funds or [])
    exists = conn.execute("SELECT 1 FROM firms WHERE name = ?", (name,)).fetchone() is not None
    if exists:
        conn.execute(
            "UPDATE firms SET lei = ?, cik = ?, status = ?, source = ?, "
            "identifiers_json = ?, funds_json = ?, updated_at = datetime('now') WHERE name = ?",
            (lei, cik, status, source, identifiers_json, funds_json, name),
        )
    else:
        conn.execute(
            "INSERT INTO firms (firm_id, name, lei, cik, status, source, is_active, "
            "identifiers_json, funds_json) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (firm_id, name, lei, cik, status, source, identifiers_json, funds_json),
        )
    if activate or not _any_active(conn):
        _activate(conn, firm_id)
    conn.commit()
    return firm_id


def list_firms(conn: sqlite3.Connection) -> list[FirmDict]:
    """All firms, demo first, then the active firm, then alphabetically by name."""
    rows = conn.execute(
        "SELECT * FROM firms ORDER BY (status = 'demo') DESC, is_active DESC, name ASC"
    ).fetchall()
    return [_row_to_firm(r) for r in rows]


def get_firm(conn: sqlite3.Connection, firm_id: str) -> FirmDict | None:
    row = conn.execute("SELECT * FROM firms WHERE firm_id = ?", (firm_id,)).fetchone()
    return _row_to_firm(row) if row else None


def get_active_firm(conn: sqlite3.Connection) -> FirmDict | None:
    row = conn.execute("SELECT * FROM firms WHERE is_active = 1 LIMIT 1").fetchone()
    return _row_to_firm(row) if row else None


def set_active_firm(conn: sqlite3.Connection, firm_id: str) -> FirmDict:
    """Make ``firm_id`` the active firm (deactivating the rest). Raise ``KeyError`` if unknown."""
    if conn.execute("SELECT 1 FROM firms WHERE firm_id = ?", (firm_id,)).fetchone() is None:
        raise KeyError(firm_id)
    _activate(conn, firm_id)
    conn.commit()
    active = get_firm(conn, firm_id)
    assert active is not None  # just activated -> row exists
    return active


def delete_firm(conn: sqlite3.Connection, firm_id: str) -> bool:
    """Delete a firm; return whether a row was removed.

    If the deleted firm was active, re-activate the most-recently-updated remaining firm (or leave
    none active when the registry becomes empty).
    """
    row = conn.execute("SELECT is_active FROM firms WHERE firm_id = ?", (firm_id,)).fetchone()
    if row is None:
        return False
    was_active = bool(row["is_active"])
    conn.execute("DELETE FROM firms WHERE firm_id = ?", (firm_id,))
    if was_active:
        nxt = conn.execute(
            "SELECT firm_id FROM firms ORDER BY updated_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        if nxt is not None:
            _activate(conn, nxt["firm_id"])
    conn.commit()
    return True


def ensure_demo_firm(conn: sqlite3.Connection) -> str:
    """Idempotently register the fixed demo firm; activate it only if no firm is active yet."""
    return upsert_firm(
        conn,
        name=DEMO_FIRM_NAME,
        status="demo",
        source="demo",
        funds=[dict(f) for f in DEMO_FUNDS],
    )


def sync_from_graph(conn: sqlite3.Connection, graph_firms: list[dict[str, Any]]) -> None:
    """Register graph-discovered firms that are absent locally; never downgrade an existing row.

    Each ``graph_firms`` item is ``{name, funds:[{name,series_id,cik,lei}]}``. Existing rows are
    left untouched (so a manually-set status/source is preserved); only genuinely new firms are
    inserted — the demo firm as ``demo``/``demo``, everything else as ``ready``/``edgar``.
    """
    for gf in graph_firms:
        name = gf.get("name")
        if not name:
            continue
        firm_id = slugify(name)
        already = conn.execute(
            "SELECT 1 FROM firms WHERE firm_id = ? OR name = ?", (firm_id, name)
        ).fetchone()
        if already is not None:
            continue
        is_demo = name == DEMO_FIRM_NAME
        upsert_firm(
            conn,
            name=name,
            status="demo" if is_demo else "ready",
            source="demo" if is_demo else "edgar",
            funds=gf.get("funds") or [],
        )
