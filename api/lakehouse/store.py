"""Simulated internal lakehouse / warehouse — a medallion model over SQLite.

Bronze (raw, conflicting per-source records) → [Silver, computed by MDM matching] → Gold (canonical
"golden record" dims, each with a PK + FIBO class). Owns its own `lh_*` tables in the app SQLite
database (reusing `api.stores.sqlite.connect`), exactly as `api/resolution/store.py` owns the
resolution queue — never touching the frozen `api/stores/sqlite.py` SCHEMA.

The knowledge graph holds only the published canonical layer; the medallion source tables stay
relational here, and per-attribute Gold←Bronze lineage lives in `lh_lineage`. All idempotent /
`IF NOT EXISTS`, so it coexists with the firms registry and the resolution queue in one file.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

LAKEHOUSE_SCHEMA = """
CREATE TABLE IF NOT EXISTS lh_source_system (
    system_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    layer       TEXT NOT NULL,              -- gold | operational | unstructured
    trust_score INTEGER NOT NULL,           -- 0..100
    description TEXT
);
CREATE TABLE IF NOT EXISTS lh_bronze_record (
    record_id       TEXT PRIMARY KEY,
    system_id       TEXT NOT NULL,
    entity_type     TEXT NOT NULL,          -- RealProperty | LegalEntity | Fund | ...
    master_key      TEXT,                   -- demo grouping of records for one real-world entity
    natural_key     TEXT,                   -- the source system's own id
    attributes_json TEXT NOT NULL,          -- the raw (conflicting) attribute bag
    ingested_at     TEXT
);
CREATE TABLE IF NOT EXISTS lh_silver_record (
    record_id        TEXT PRIMARY KEY,
    bronze_record_id TEXT NOT NULL,
    entity_type      TEXT NOT NULL,
    match_group      TEXT,
    normalized_json  TEXT NOT NULL,
    updated_at       TEXT
);
CREATE TABLE IF NOT EXISTS lh_gold_dim (
    dim_table        TEXT NOT NULL,         -- dim_property | dim_fund | dim_legal_entity | ...
    pk               TEXT NOT NULL,         -- canonical PK, e.g. PROP-1001
    entity_type      TEXT NOT NULL,
    attributes_json  TEXT NOT NULL,         -- canonical golden attributes
    fibo_class       TEXT,
    golden_record_id TEXT,
    published_at     TEXT,
    PRIMARY KEY (dim_table, pk)
);
CREATE TABLE IF NOT EXISTS lh_lineage (
    dim_table        TEXT NOT NULL,
    golden_pk        TEXT NOT NULL,
    bronze_record_id TEXT NOT NULL,
    attribute        TEXT NOT NULL,         -- which attribute this bronze record contributed
    contributed      INTEGER NOT NULL       -- 1 if this record WON the attribute (the survivor)
);
CREATE INDEX IF NOT EXISTS ix_lh_bronze_master ON lh_bronze_record(entity_type, master_key);
CREATE INDEX IF NOT EXISTS ix_lh_lineage_pk ON lh_lineage(dim_table, golden_pk);
"""

_SEED = Path(__file__).parent.parent.parent / "fixtures" / "lakehouse" / "seed.json"


def init_lakehouse_db(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Create the `lh_*` tables if absent (idempotent)."""
    conn.executescript(LAKEHOUSE_SCHEMA)
    conn.commit()
    return conn


def bootstrap(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Ensure the schema exists and the neutral demo seed is loaded (idempotent)."""
    init_lakehouse_db(conn)
    seed_if_empty(conn)
    return conn


# --- source systems -------------------------------------------------------------------------


def upsert_source_system(conn: sqlite3.Connection, **row: Any) -> None:
    conn.execute(
        "INSERT INTO lh_source_system (system_id, name, layer, trust_score, description) "
        "VALUES (:system_id, :name, :layer, :trust_score, :description) "
        "ON CONFLICT(system_id) DO UPDATE SET name=excluded.name, layer=excluded.layer, "
        "trust_score=excluded.trust_score, description=excluded.description",
        {"description": None, **row},
    )
    conn.commit()


def list_source_systems(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT system_id, name, layer, trust_score, description FROM lh_source_system "
        "ORDER BY trust_score DESC, name"
    ).fetchall()
    return [dict(r) for r in rows]


def trust_by_system(conn: sqlite3.Connection) -> dict[str, int]:
    return {r["system_id"]: r["trust_score"] for r in list_source_systems(conn)}


# --- bronze ---------------------------------------------------------------------------------


def insert_bronze_record(conn: sqlite3.Connection, *, attributes: dict[str, Any], **row: Any) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO lh_bronze_record "
        "(record_id, system_id, entity_type, master_key, natural_key, attributes_json, ingested_at) "
        "VALUES (:record_id, :system_id, :entity_type, :master_key, :natural_key, :attributes_json, "
        ":ingested_at)",
        {
            "master_key": None, "natural_key": None, "ingested_at": None,
            **row, "attributes_json": json.dumps(attributes),
        },
    )
    conn.commit()


def _bronze_row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["attributes"] = json.loads(d.pop("attributes_json") or "{}")
    return d


def bronze_for_master(conn: sqlite3.Connection, entity_type: str, master_key: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM lh_bronze_record WHERE entity_type = ? AND master_key = ? ORDER BY record_id",
        (entity_type, master_key),
    ).fetchall()
    return [_bronze_row(r) for r in rows]


def master_entities(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Distinct (entity_type, master_key) clusters with a source-record count — the MDM candidates."""
    rows = conn.execute(
        "SELECT entity_type, master_key, COUNT(*) AS n_sources FROM lh_bronze_record "
        "WHERE master_key IS NOT NULL GROUP BY entity_type, master_key ORDER BY entity_type, master_key"
    ).fetchall()
    return [dict(r) for r in rows]


# --- gold + lineage -------------------------------------------------------------------------


def upsert_gold_dim(conn: sqlite3.Connection, *, attributes: dict[str, Any], **row: Any) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO lh_gold_dim "
        "(dim_table, pk, entity_type, attributes_json, fibo_class, golden_record_id, published_at) "
        "VALUES (:dim_table, :pk, :entity_type, :attributes_json, :fibo_class, :golden_record_id, "
        ":published_at)",
        {
            "fibo_class": None, "golden_record_id": None, "published_at": None,
            **row, "attributes_json": json.dumps(attributes),
        },
    )
    conn.commit()


def get_gold_dim(conn: sqlite3.Connection, dim_table: str, pk: str) -> dict[str, Any] | None:
    r = conn.execute(
        "SELECT * FROM lh_gold_dim WHERE dim_table = ? AND pk = ?", (dim_table, pk)
    ).fetchone()
    if r is None:
        return None
    d = dict(r)
    d["attributes"] = json.loads(d.pop("attributes_json") or "{}")
    d["lineage"] = get_lineage(conn, dim_table, pk)
    return d


def replace_lineage(conn: sqlite3.Connection, dim_table: str, golden_pk: str,
                    rows: list[dict[str, Any]]) -> None:
    conn.execute("DELETE FROM lh_lineage WHERE dim_table = ? AND golden_pk = ?", (dim_table, golden_pk))
    conn.executemany(
        "INSERT INTO lh_lineage (dim_table, golden_pk, bronze_record_id, attribute, contributed) "
        "VALUES (:dim_table, :golden_pk, :bronze_record_id, :attribute, :contributed)",
        [{"dim_table": dim_table, "golden_pk": golden_pk, **r} for r in rows],
    )
    conn.commit()


def get_lineage(conn: sqlite3.Connection, dim_table: str, golden_pk: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT bronze_record_id, attribute, contributed FROM lh_lineage "
        "WHERE dim_table = ? AND golden_pk = ? ORDER BY attribute, bronze_record_id",
        (dim_table, golden_pk),
    ).fetchall()
    return [dict(r) for r in rows]


# --- seed -----------------------------------------------------------------------------------


def seed_if_empty(conn: sqlite3.Connection, path: Path = _SEED) -> bool:
    """Load the neutral demo seed (source systems + conflicting bronze records) if none exist."""
    have = conn.execute("SELECT 1 FROM lh_source_system LIMIT 1").fetchone()
    if have or not path.exists():
        return False
    data = json.loads(path.read_text())
    for sys_ in data.get("source_systems", []):
        upsert_source_system(conn, **sys_)
    for rec in data.get("bronze_records", []):
        insert_bronze_record(
            conn,
            record_id=rec["record_id"],
            system_id=rec["system_id"],
            entity_type=rec["entity_type"],
            master_key=rec.get("master_key"),
            natural_key=rec.get("natural_key"),
            ingested_at=rec.get("ingested_at"),
            attributes=rec.get("attributes", {}),
        )
    return True
