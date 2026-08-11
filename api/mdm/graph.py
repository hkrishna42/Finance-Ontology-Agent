"""Graph-derived MDM master entities — the entities that actually exist in the ingested graph.

The lakehouse seed gives the wizard four demoable master entities (with full source records for the
match/merge flow). This module surfaces ADDITIONAL master entities read straight from the knowledge
graph: `Company` nodes that recur — mentioned two-or-more times, across one-or-more source documents
— which is the graph analog of "one real-world entity seen across many source systems". They are
mapped into the same `MdmEntity` shape `/mdm/entities` already returns, tagged `source: "graph"` so
the panel can tell a live-graph entity from a seed one (the wizard's match/merge stay pointed at the
lakehouse seed, which has the bronze source records those steps need).

Read-only + best-effort: the single Cypher read is issued by `api/mdm/routes.py` inside a try/except
so any Neo4j hiccup yields an empty list and the neutral seed remains the fallback.
"""

from __future__ import annotations

from typing import Any

from ..fibo import grounding

# Company nodes mentioned >= 2 times are the master-entity candidates. `n_docs` counts the DISTINCT
# source documents that mention the node (the "source systems" analog); `n_mentions` is the raw
# total; `n_props` is how many populated attributes the node carries. Ordered by recurrence.
GRAPH_MASTER_CYPHER = """
MATCH (c:Company)<-[m:MENTIONS]-(ch:Chunk)
WITH c, count(DISTINCT ch.doc_id) AS n_docs, count(m) AS n_mentions
WHERE n_mentions >= 2
RETURN c.name AS name, c.cik AS cik, c.lei AS lei, c.category AS category,
       c.fibo_class AS fibo_class, n_docs, n_mentions,
       size([k IN keys(c) WHERE c[k] IS NOT NULL]) AS n_props
ORDER BY n_mentions DESC, n_docs DESC, name
LIMIT $limit
"""

# Firm-scoped master entities: the Companies the firm's funds HOLD (its issuer holdings), which is the
# firm analog of "the master entities this firm actually owns". Same firm-subgraph anchor the Graph
# Explorer uses ((c)<-[:HOLDS]-(:Fund)-[:MANAGED_BY]->(:Company {name:$firm})). Mentions/docs are an
# OPTIONAL MATCH so a held issuer with zero ingested mentions still surfaces (n_mentions = 0). Counts
# are DISTINCT because a company held by several of the firm's funds fans the row out. Ranked by the
# firm's largest position (max holding weight) then recurrence — the same "biggest holdings first"
# ordering as /graph/neighbors. A firm holding nothing yields []. Same returned columns as above.
GRAPH_MASTER_FIRM_CYPHER = """
MATCH (c:Company)<-[h:HOLDS]-(:Fund)-[:MANAGED_BY]->(:Company {name: $firm})
OPTIONAL MATCH (c)<-[m:MENTIONS]-(ch:Chunk)
WITH c, coalesce(max(h.weight_pct), 0.0) AS max_weight,
     count(DISTINCT ch.doc_id) AS n_docs, count(DISTINCT m) AS n_mentions
RETURN c.name AS name, c.cik AS cik, c.lei AS lei, c.category AS category,
       c.fibo_class AS fibo_class, n_docs, n_mentions,
       size([k IN keys(c) WHERE c[k] IS NOT NULL]) AS n_props
ORDER BY max_weight DESC, n_mentions DESC, name
LIMIT $limit
"""


def _to_mdm_entity(row: dict[str, Any]) -> dict[str, Any] | None:
    """Map one graph row → the `MdmEntity` shape (types.ts) with an additive `graph` discriminator."""
    name = row.get("name")
    if not name:
        return None
    n_docs = int(row.get("n_docs") or 0)
    n_mentions = int(row.get("n_mentions") or 0)
    # Ground to FIBO for the same fibo_curie/fibo_class pair the seed entities carry; prefer the
    # class already stamped on the node (ingest may have refined it, e.g. a debt issuer).
    g = grounding.ground("Company", category=row.get("category"))
    curie = row.get("fibo_class") or g.curie or None
    return {
        "entity_id": f"Company:{name}",
        "entity_type": "Company",
        "master_key": name,
        "display_name": name,
        "n_sources": n_docs or n_mentions,
        "n_attributes": int(row.get("n_props") or 0),
        "fibo_curie": curie,
        "fibo_class": g.class_iri or None,
        # Additive fields (ignored by consumers that only read the core MdmEntity shape):
        "source": "graph",
        "n_mentions": n_mentions,
    }


def graph_master_entities(
    store: Any, *, firm: str | None = None, limit: int = 25
) -> list[dict[str, Any]]:
    """Read `Company` master entities from the graph as `MdmEntity[]`.

    Unscoped (`firm=None`) → the recurring Companies across the whole corpus (the demo/seed listing).
    Scoped (`firm=<name>`) → only the Companies the firm's funds HOLD, ranked by holding weight; a
    firm holding nothing yields `[]` (never the demo issuers).

    `store` is any object exposing `.run(cypher, **params) -> list[dict]` (a `Neo4jStore`, or a fake
    in tests). Raises only if `store.run` raises — the caller wraps this best-effort.
    """
    if firm:
        rows = store.run(GRAPH_MASTER_FIRM_CYPHER, firm=firm, limit=limit)
    else:
        rows = store.run(GRAPH_MASTER_CYPHER, limit=limit)
    out: list[dict[str, Any]] = []
    for r in rows:
        entity = _to_mdm_entity(r)
        if entity is not None:
            out.append(entity)
    return out
