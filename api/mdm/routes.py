"""MDM wizard HTTP surface (APIRouter, prefix /mdm). Wired into api.main via include_router.

The 5-step "Multi-Source Entity Resolution -> Single Golden Record" flow:
  1. GET  /mdm/entities                     -> selectable master entities (clusters)
  2. GET  /mdm/entities/{entity_id}/sources -> the conflicting source-system records
  3. POST /mdm/match {entity_id}            -> ontology-driven matching (blocking + confidence %)
  4. GET  /mdm/survivorship-policy          -> the attribute-level rule set
  5. POST /mdm/merge {entity_id}            -> run match & merge; publish the golden record
     GET  /mdm/golden/{dim_table}/{pk}      -> re-fetch a published golden record + lineage
"""

from __future__ import annotations

import sqlite3
from datetime import UTC
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import get_settings
from ..fibo import grounding
from ..lakehouse import store as lakehouse
from ..stores.sqlite import connect
from . import survivorship

router = APIRouter(prefix="/mdm", tags=["mdm"])


def _conn() -> sqlite3.Connection:
    conn = connect(get_settings().sqlite_path)
    lakehouse.bootstrap(conn)
    return conn


def _split(entity_id: str) -> tuple[str, str]:
    entity_type, _, master_key = entity_id.partition(":")
    if not entity_type or not master_key:
        raise HTTPException(status_code=400, detail="entity_id must be 'EntityType:master_key'")
    return entity_type, master_key


def _policy_meta(entity_type: str) -> dict[str, Any]:
    et = survivorship.load_policy()["entity_types"].get(entity_type)
    if et is None:
        raise HTTPException(status_code=404, detail=f"no MDM policy for {entity_type}")
    return et


def _representative(records: list[dict[str, Any]]) -> dict[str, Any]:
    """The most-trusted record's attributes (for display name + FIBO grounding of the cluster)."""
    if not records:
        return {}
    best = max(records, key=lambda r: {"curated_gold": 3, "pms": 2}.get(r["system_id"], 1))
    return best.get("attributes", {})


@router.get("/entities")
def list_entities() -> dict[str, Any]:
    conn = _conn()
    try:
        policy = survivorship.load_policy()["entity_types"]
        out = []
        for m in lakehouse.master_entities(conn):
            et = m["entity_type"]
            spec = policy.get(et)
            if spec is None:
                continue
            records = lakehouse.bronze_for_master(conn, et, m["master_key"])
            attrs = _representative(records)
            g = grounding.ground(et, attributes=attrs)
            out.append({
                "entity_id": f"{et}:{m['master_key']}",
                "entity_type": et,
                "master_key": m["master_key"],
                "display_name": attrs.get(spec["name_field"]) or m["master_key"],
                "n_sources": m["n_sources"],
                "n_attributes": len(spec["attributes"]),
                "fibo_curie": g.curie or None,
                "fibo_class": g.class_iri or None,
            })
        return {"entities": out}
    finally:
        conn.close()


@router.get("/entities/{entity_id}/sources")
def entity_sources(entity_id: str) -> dict[str, Any]:
    entity_type, master_key = _split(entity_id)
    et = _policy_meta(entity_type)
    conn = _conn()
    try:
        systems = {s["system_id"]: s for s in lakehouse.list_source_systems(conn)}
        records = lakehouse.bronze_for_master(conn, entity_type, master_key)
        sources = [{
            "system": r["system_id"],
            "system_name": systems.get(r["system_id"], {}).get("name", r["system_id"]),
            "layer": systems.get(r["system_id"], {}).get("layer"),
            "trust": systems.get(r["system_id"], {}).get("trust_score"),
            "natural_key": r.get("natural_key"),
            "values": r["attributes"],
        } for r in records]
        sources.sort(key=lambda s: -(s["trust"] or 0))
        return {"entity_id": entity_id, "entity_type": entity_type,
                "name_field": et["name_field"], "sources": sources}
    finally:
        conn.close()


class EntityRequest(BaseModel):
    entity_id: str


@router.post("/match")
def match(req: EntityRequest) -> dict[str, Any]:
    entity_type, master_key = _split(req.entity_id)
    conn = _conn()
    try:
        return survivorship.match_only(conn, entity_type, master_key)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        conn.close()


@router.get("/survivorship-policy")
def survivorship_policy() -> dict[str, Any]:
    return survivorship.load_policy()


@router.post("/merge")
def merge(req: EntityRequest) -> dict[str, Any]:
    entity_type, master_key = _split(req.entity_id)
    conn = _conn()
    neo4j_store = _neo4j_store()
    try:
        return survivorship.run_match_and_merge(
            conn, entity_type, master_key, neo4j_store=neo4j_store, now=_now(),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        conn.close()
        if neo4j_store is not None:
            neo4j_store.close()


@router.get("/golden/{dim_table}/{pk}")
def golden(dim_table: str, pk: str) -> dict[str, Any]:
    conn = _conn()
    try:
        row = lakehouse.get_gold_dim(conn, dim_table, pk)
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail=f"no golden record for {dim_table}/{pk}")
    return row


def _neo4j_store() -> Any:
    """Best-effort Neo4j store for publishing the canonical node; None when the graph is unavailable."""
    try:
        from ..stores.neo4j import Neo4jStore

        store = Neo4jStore(get_settings())
        store.verify_connectivity()
        return store
    except Exception:  # noqa: BLE001 - publish still writes the Gold dim; the graph node is best-effort
        return None


def _now() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat(timespec="seconds")
