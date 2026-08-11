"""SQLite provisional-resolution queue.

When the pipeline cannot confidently pin a mention to the spine it parks a *provisional* record
here for a human steward to merge (or reject) via the `/resolve` routes. This module owns its own
table and reuses `api.stores.sqlite.connect` for the connection only — it does not modify the frozen
app-state schema.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS resolution_queue (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    mention          TEXT NOT NULL,
    normalized       TEXT,
    ticker           TEXT,
    label            TEXT NOT NULL DEFAULT 'Company',
    aliases_json     TEXT,
    span             TEXT,
    doc_id           TEXT,
    chunk_id         TEXT,
    candidate_cik    TEXT,
    candidate_lei    TEXT,
    candidate_title  TEXT,
    method           TEXT,
    confidence       REAL NOT NULL DEFAULT 0.0,
    status           TEXT NOT NULL DEFAULT 'provisional',
    candidates_json  TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Columns added after the initial release; ALTER-in for pre-existing app.db files.
_ADDED_COLUMNS = {
    "label": "TEXT NOT NULL DEFAULT 'Company'",
    "aliases_json": "TEXT",
    "span": "TEXT",
    "doc_id": "TEXT",
    "chunk_id": "TEXT",
}


def init_resolution_db(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Create the queue table if absent + backfill columns on older DBs (idempotent)."""
    conn.executescript(QUEUE_SCHEMA)
    existing = {
        r[1] for r in conn.execute("PRAGMA table_info(resolution_queue)").fetchall()
    }
    for col, ddl in _ADDED_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE resolution_queue ADD COLUMN {col} {ddl}")
    conn.commit()
    return conn


def enqueue(
    conn: sqlite3.Connection,
    *,
    mention: str,
    normalized: str | None = None,
    ticker: str | None = None,
    label: str = "Company",
    aliases: list[str] | None = None,
    span: str | None = None,
    doc_id: str | None = None,
    chunk_id: str | None = None,
    candidate_cik: str | None = None,
    candidate_lei: str | None = None,
    candidate_title: str | None = None,
    method: str | None = None,
    confidence: float = 0.0,
    candidates: list[dict[str, Any]] | None = None,
) -> int:
    """Insert a provisional record; returns its row id."""
    cur = conn.execute(
        "INSERT INTO resolution_queue "
        "(mention, normalized, ticker, label, aliases_json, span, doc_id, chunk_id, "
        " candidate_cik, candidate_lei, candidate_title, method, confidence, status, "
        " candidates_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'provisional', ?)",
        (
            mention,
            normalized,
            ticker,
            label,
            json.dumps(aliases or []),
            span,
            doc_id,
            chunk_id,
            candidate_cik,
            candidate_lei,
            candidate_title,
            method,
            confidence,
            json.dumps(candidates or []),
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def set_provenance(
    conn: sqlite3.Connection,
    queue_id: int,
    *,
    span: str | None = None,
    label: str | None = None,
    aliases: list[str] | None = None,
    doc_id: str | None = None,
    chunk_id: str | None = None,
) -> None:
    """Attach document provenance to an already-queued mention (coalesce: only overwrite when given).

    The resolver enqueues a bare provisional row; the ingest pipeline calls this to fill in the source
    span / doc / chunk it holds, so the /resolve panel can show a real snippet instead of a null span.
    A `None` argument leaves the stored value untouched; `aliases` is only written when non-empty.
    """
    aliases_json = json.dumps(aliases) if aliases else None
    conn.execute(
        "UPDATE resolution_queue SET "
        "  span = coalesce(?, span), "
        "  label = coalesce(?, label), "
        "  aliases_json = coalesce(?, aliases_json), "
        "  doc_id = coalesce(?, doc_id), "
        "  chunk_id = coalesce(?, chunk_id), "
        "  updated_at = datetime('now') "
        "WHERE id = ?",
        (span, label, aliases_json, doc_id, chunk_id, queue_id),
    )
    conn.commit()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["candidates"] = json.loads(d.pop("candidates_json") or "[]")
    d["aliases"] = json.loads(d.pop("aliases_json") or "[]")
    return d


def list_queue(
    conn: sqlite3.Connection, *, status: str | None = "provisional"
) -> list[dict[str, Any]]:
    """List queue rows, optionally filtered by status (default: only provisional)."""
    if status is None:
        rows = conn.execute(
            "SELECT * FROM resolution_queue ORDER BY id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM resolution_queue WHERE status = ? ORDER BY id", (status,)
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get(conn: sqlite3.Connection, queue_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM resolution_queue WHERE id = ?", (queue_id,)
    ).fetchone()
    return _row_to_dict(row) if row else None


def merge(
    conn: sqlite3.Connection,
    queue_id: int,
    *,
    cik: str | None = None,
    lei: str | None = None,
    title: str | None = None,
) -> dict[str, Any] | None:
    """Steward action: pin a provisional record to a canonical entity and mark it merged.

    `cik`/`lei`/`title` all `coalesce` — a `None` argument leaves the stored value untouched (a
    Person merge, for instance, carries no CIK, and repointing keys on the canonical node's name).
    """
    conn.execute(
        "UPDATE resolution_queue "
        "SET candidate_cik = coalesce(?, candidate_cik), "
        "    candidate_lei = coalesce(?, candidate_lei), "
        "    candidate_title = coalesce(?, candidate_title), method = 'steward_merge', "
        "    confidence = 1.0, status = 'merged', updated_at = datetime('now') "
        "WHERE id = ?",
        (cik, lei, title, queue_id),
    )
    conn.commit()
    return get(conn, queue_id)


def promote(
    conn: sqlite3.Connection,
    queue_id: int,
    *,
    cik: str | None = None,
    lei: str | None = None,
    title: str | None = None,
) -> dict[str, Any] | None:
    """Steward action: keep the provisional mention as its own new canonical node.

    The ingest pipeline already wrote the mention's node, so promotion is a queue-status decision:
    accept the node as canonical rather than folding it into an existing one.
    """
    conn.execute(
        "UPDATE resolution_queue "
        "SET candidate_cik = coalesce(?, candidate_cik), "
        "    candidate_lei = coalesce(?, candidate_lei), "
        "    candidate_title = coalesce(?, candidate_title), method = 'steward_promote', "
        "    confidence = 1.0, status = 'promoted', updated_at = datetime('now') "
        "WHERE id = ?",
        (cik, lei, title, queue_id),
    )
    conn.commit()
    return get(conn, queue_id)


def reject(conn: sqlite3.Connection, queue_id: int) -> dict[str, Any] | None:
    """Steward action: mark a provisional record rejected (kept for audit)."""
    conn.execute(
        "UPDATE resolution_queue SET status = 'rejected', updated_at = datetime('now') "
        "WHERE id = ?",
        (queue_id,),
    )
    conn.commit()
    return get(conn, queue_id)
