"""Graph-side reconciliation for steward resolution actions — repoint a duplicate entity node
onto its canonical twin, no APOC required.

When a steward merges a provisional mention onto an existing canonical node, the mention already
has its own node in the graph (the ingest pipeline writes *every* extracted entity, resolved or
not). This module moves that duplicate's relationships onto the canonical node, merges its
properties, and removes the duplicate — so the graph ends up with a single node per real-world
entity, matching what the SQLite queue records.

Cypher cannot `CREATE`/`MERGE` a relationship with a *dynamic* type without APOC, so the repoint
discovers the duplicate's relationship types at runtime and substitutes each as a **validated
literal** (checked against the ontology vocabulary before it ever touches a query string). No APOC
procedure is used; an APOC fast path (`apoc.refactor.mergeNodes`) could drop in later behind the
same function contract.
"""

from __future__ import annotations

from typing import Any

from ..ontology.schema import ENTITY_SPECS, RELATION_SPECS, entity_spec

# The identifiers the repoint is allowed to weave into a Cypher string. Anything outside these
# ontology-derived sets is rejected rather than interpolated — the only values that reach an
# f-string are the entity label, its key field, and relationship types, all vetted here.
_ENTITY_LABELS = frozenset(e.label for e in ENTITY_SPECS)
_REL_TYPES = frozenset(r.type for r in RELATION_SPECS)  # includes MENTIONS


def _safe_label(label: str) -> str:
    if label not in _ENTITY_LABELS:
        raise ValueError(f"refusing to repoint an unknown entity label: {label!r}")
    return label


def _safe_rel(rtype: str) -> str:
    if rtype not in _REL_TYPES:
        raise ValueError(f"refusing to repoint an unknown relationship type: {rtype!r}")
    return rtype


def repoint_entity(
    store: Any,
    *,
    label: str,
    dup_key: str,
    canonical_key: str,
    extra_props: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Move every edge from the duplicate node onto the canonical node, then delete the duplicate.

    Args:
        store: anything exposing ``run(query, **params) -> list[dict]`` (Neo4jStore or a fake).
        label: the shared entity label of both nodes (e.g. ``"Company"``).
        dup_key: the duplicate (provisional-mention) node's merge-key value.
        canonical_key: the surviving canonical node's merge-key value.
        extra_props: props to stamp on the canonical node (e.g. a confirmed ``cik``/``lei``).

    Returns ``{moved, dup_deleted, canonical_key, rel_types}``. It is a safe, idempotent no-op
    (``moved=0, dup_deleted=False``) when the two keys coincide or the duplicate has no graph node
    (e.g. a demo-fixture mention that was never ingested).
    """
    lbl = _safe_label(label)
    key = entity_spec(lbl).key
    result: dict[str, Any] = {
        "moved": 0,
        "dup_deleted": False,
        "canonical_key": canonical_key,
        "rel_types": [],
    }
    if not dup_key or not canonical_key or dup_key == canonical_key:
        return result

    # The canonical node must exist to receive the edges (idempotent — it usually already does).
    store.run(f"MERGE (n:{lbl} {{{key}: $k}})", k=canonical_key)
    if extra_props:
        store.run(
            f"MATCH (n:{lbl} {{{key}: $k}}) SET n += $props", k=canonical_key, props=extra_props
        )

    # Discover the duplicate's relationship types; if it has no node, there is nothing to move.
    rows = store.run(
        f"MATCH (d:{lbl} {{{key}: $dup}})-[r]-() RETURN collect(DISTINCT type(r)) AS types",
        dup=dup_key,
    )
    discovered = (rows[0].get("types") if rows else None) or []
    rel_types = [t for t in discovered if t in _REL_TYPES]
    result["rel_types"] = rel_types

    moved = 0
    for rtype in rel_types:
        t = _safe_rel(rtype)
        # outgoing: (dup)-[r:T]->(x)  =>  (canon)-[:T]->(x)
        out = store.run(
            f"MATCH (d:{lbl} {{{key}: $dup}})-[r:{t}]->(x) "
            f"MATCH (c:{lbl} {{{key}: $canon}}) WHERE x <> c "
            f"MERGE (c)-[nr:{t}]->(x) SET nr += properties(r) "
            "DELETE r RETURN count(*) AS n",
            dup=dup_key,
            canon=canonical_key,
        )
        moved += int((out[0].get("n") if out else 0) or 0)
        # incoming: (y)-[r:T]->(dup)  =>  (y)-[:T]->(canon)
        inc = store.run(
            f"MATCH (y)-[r:{t}]->(d:{lbl} {{{key}: $dup}}) "
            f"MATCH (c:{lbl} {{{key}: $canon}}) WHERE y <> c "
            f"MERGE (y)-[nr:{t}]->(c) SET nr += properties(r) "
            "DELETE r RETURN count(*) AS n",
            dup=dup_key,
            canon=canonical_key,
        )
        moved += int((inc[0].get("n") if inc else 0) or 0)
    result["moved"] = moved

    # Merge the duplicate's own properties (minus identity fields — canonical identity always wins)
    # onto the canonical node, then detach-delete the duplicate.
    drows = store.run(f"MATCH (d:{lbl} {{{key}: $dup}}) RETURN properties(d) AS p", dup=dup_key)
    if drows:
        dprops = dict((drows[0].get("p") if drows else None) or {})
        for drop in (key, "cik", "norm"):
            dprops.pop(drop, None)
        if dprops:
            store.run(
                f"MATCH (c:{lbl} {{{key}: $canon}}) SET c += $props",
                canon=canonical_key,
                props=dprops,
            )
    deleted = store.run(
        f"MATCH (d:{lbl} {{{key}: $dup}}) DETACH DELETE d RETURN count(*) AS n", dup=dup_key
    )
    result["dup_deleted"] = bool(deleted and (deleted[0].get("n") or 0))
    return result
