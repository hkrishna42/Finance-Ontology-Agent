"""Thin SQLite app-state store (stdlib `sqlite3`, no extra deps).

Holds the non-graph state: ingest/query **jobs**, pipeline **spans**, the LLM-call **agent_logs**
that back the trace drawer, the **report_registry** (M7), and the **firms** registry (Phase-1
multi-firm selector). `log_llm_call()` is the observability hook every provider call site can write
to — full traceability is a project principle.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    meta        TEXT
);

CREATE TABLE IF NOT EXISTS spans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT,
    seq         INTEGER NOT NULL DEFAULT 0,
    name        TEXT NOT NULL,
    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at    TEXT,
    data        TEXT
);

CREATE TABLE IF NOT EXISTS agent_logs (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                      TEXT NOT NULL DEFAULT (datetime('now')),
    job_id                  TEXT,
    role                    TEXT NOT NULL,
    model                   TEXT NOT NULL,
    input_tokens            INTEGER NOT NULL DEFAULT 0,
    output_tokens           INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens       INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens   INTEGER NOT NULL DEFAULT 0,
    stop_reason             TEXT,
    latency_ms              INTEGER
);

CREATE TABLE IF NOT EXISTS report_registry (
    report_id   TEXT PRIMARY KEY,
    title       TEXT,
    period      TEXT,
    sha256      TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS firms (
    firm_id          TEXT PRIMARY KEY,               -- slugify(name)
    name             TEXT NOT NULL UNIQUE,
    lei              TEXT,
    cik              TEXT,
    status           TEXT NOT NULL DEFAULT 'ready',  -- demo | onboarding | ready | failed
    source           TEXT NOT NULL DEFAULT 'edgar',  -- demo | edgar
    is_active        INTEGER NOT NULL DEFAULT 0,     -- 0/1, at most one =1
    identifiers_json TEXT,                           -- JSON object (series/tickers/…)
    funds_json       TEXT,                           -- JSON array of {name,series_id,cik,lei}
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect(path: str = ":memory:") -> sqlite3.Connection:
    """Open a connection, ensure the parent dir exists, and enable dict-like row access.

    `check_same_thread=False`: FastAPI runs sync routes and their generator-dependency setup/teardown
    on anyio's threadpool, which can create a per-request connection on one thread and close it on
    another. SQLite's default same-thread guard turns that into a 500 under any concurrent load — e.g.
    the UI firing `GET /firms` + `GET /firms/active` together made the whole firm selector go
    "offline". Each request still gets its OWN connection used serially (never shared across threads
    simultaneously), so relaxing the guard is safe.
    """
    if path != ":memory:":
        Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Create all tables if absent (idempotent)."""
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def log_llm_call(
    conn: sqlite3.Connection,
    *,
    role: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    stop_reason: str | None = None,
    latency_ms: int | None = None,
    job_id: str | None = None,
) -> int:
    """Record one LLM call for the trace drawer; returns the new row id."""
    cur = conn.execute(
        "INSERT INTO agent_logs (job_id, role, model, input_tokens, output_tokens, "
        "cache_read_tokens, cache_creation_tokens, stop_reason, latency_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job_id,
            role,
            model,
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_creation_tokens,
            stop_reason,
            latency_ms,
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def record_job(
    conn: sqlite3.Connection,
    job_id: str,
    kind: str,
    status: str = "pending",
    meta: str | None = None,
) -> None:
    """Upsert a job row (used by the ingest/query pipelines in later milestones)."""
    conn.execute(
        "INSERT INTO jobs (job_id, kind, status, meta) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(job_id) DO UPDATE SET status=excluded.status, "
        "meta=excluded.meta, updated_at=datetime('now')",
        (job_id, kind, status, meta),
    )
    conn.commit()


def get_settings_db(path: str) -> sqlite3.Connection:
    """Convenience: open + initialise the app DB at `path`."""
    return init_db(connect(path))


def _main(argv: list[str] | None = None) -> int:  # pragma: no cover - manual smoke helper
    import argparse

    parser = argparse.ArgumentParser(description="SQLite app-state utility")
    parser.add_argument("--init", action="store_true", help="create tables")
    parser.add_argument("--path", default=None, help="db path (defaults to settings.sqlite_path)")
    args = parser.parse_args(argv)

    from ..config import get_settings

    path = args.path or get_settings().sqlite_path
    conn = get_settings_db(path)
    if args.init:
        tables = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        print(f"initialised {path}; tables: {', '.join(tables)}")
    conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
