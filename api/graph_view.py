"""Graph-read endpoints for the UI: GET /graph (explorer) and GET /documents (doc viewer).

Both are entitlement-aware: nodes/edges/documents carrying `sensitivity: internal` are excluded
unless `internal` is in the caller's entitlements (default `["public"]`). This enforces the wall
at the data layer — the read literally cannot return internal content without the entitlement.

Cypher returns plain fields (not driver objects) so the shape functions are pure and offline-
testable. Embedding vectors are stripped.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from neo4j import GraphDatabase

from .config import get_settings
from .firms import scope

router = APIRouter(tags=["graph"])

_LABEL_COALESCE = "coalesce(x.name, x.title, x.form, x.key, x.report_id, x.doc_id, x.chunk_id)"

_SUBGRAPH_CYPHER = f"""
MATCH (n)-[r]->(m)
WHERE coalesce(r.confidence, 1.0) >= $min_conf
  AND (n.sensitivity IS NULL OR n.sensitivity IN $entitlements)
  AND (m.sensitivity IS NULL OR m.sensitivity IN $entitlements)
  AND (r.sensitivity IS NULL OR r.sensitivity IN $entitlements)
RETURN elementId(n) AS n_id, labels(n)[0] AS n_type,
       {_LABEL_COALESCE.replace('x.', 'n.')} AS n_label, properties(n) AS n_props,
       elementId(m) AS m_id, labels(m)[0] AS m_type,
       {_LABEL_COALESCE.replace('x.', 'm.')} AS m_label, properties(m) AS m_props,
       elementId(r) AS r_id, type(r) AS r_type, r.confidence AS r_conf, properties(r) AS r_props
LIMIT $limit
"""

_DOCS_CYPHER = """
MATCH (d:Document)
WHERE d.sensitivity IS NULL OR d.sensitivity IN $entitlements
OPTIONAL MATCH (c:Chunk {doc_id: d.doc_id})
WITH d, c ORDER BY coalesce(c.page, '0')
RETURN d.doc_id AS doc_id, d.title AS title, d.doc_type AS doc_type,
       coalesce(d.sensitivity, 'public') AS sensitivity, d.filing_date AS filing_date,
       d.url AS url,
       collect(CASE WHEN c IS NULL THEN NULL
                    ELSE {chunk_id: c.chunk_id, text: c.text,
                          sensitivity: coalesce(c.sensitivity, 'public')} END) AS chunks
ORDER BY doc_id
"""

# Firm-scoped explorer: the firm Company, its Funds (MANAGED_BY), their holdings (HOLDS→Company),
# the SUPPLIES_TO / EXPOSED_TO edges among/from those holdings, and (once enriched) the Chunk nodes
# whose MENTIONS target one of the holdings. Same returned columns as _SUBGRAPH_CYPHER, so the pure
# shape functions stay identical; only the WHERE narrows the edge set to the firm's subgraph.
_SUBGRAPH_FIRM_CYPHER = f"""
MATCH (n)-[r]->(m)
WHERE coalesce(r.confidence, 1.0) >= $min_conf
  AND (n.sensitivity IS NULL OR n.sensitivity IN $entitlements)
  AND (m.sensitivity IS NULL OR m.sensitivity IN $entitlements)
  AND (r.sensitivity IS NULL OR r.sensitivity IN $entitlements)
  AND (
        (type(r) = 'MANAGED_BY' AND m.name = $firm)
     OR (type(r) = 'HOLDS'
         AND EXISTS {{ (n)-[:MANAGED_BY]->(:Company {{name: $firm}}) }})
     OR (type(r) = 'SUPPLIES_TO'
         AND EXISTS {{ (n)<-[:HOLDS]-(:Fund)-[:MANAGED_BY]->(:Company {{name: $firm}}) }}
         AND EXISTS {{ (m)<-[:HOLDS]-(:Fund)-[:MANAGED_BY]->(:Company {{name: $firm}}) }})
     OR (type(r) = 'EXPOSED_TO'
         AND EXISTS {{ (n)<-[:HOLDS]-(:Fund)-[:MANAGED_BY]->(:Company {{name: $firm}}) }})
     OR (type(r) = 'MENTIONS'
         AND EXISTS {{ (m)<-[:HOLDS]-(:Fund)-[:MANAGED_BY]->(:Company {{name: $firm}}) }})
  )
RETURN elementId(n) AS n_id, labels(n)[0] AS n_type,
       {_LABEL_COALESCE.replace('x.', 'n.')} AS n_label, properties(n) AS n_props,
       elementId(m) AS m_id, labels(m)[0] AS m_type,
       {_LABEL_COALESCE.replace('x.', 'm.')} AS m_label, properties(m) AS m_props,
       elementId(r) AS r_id, type(r) AS r_type, r.confidence AS r_conf, properties(r) AS r_props
LIMIT $limit
"""

# Firm-scoped documents: a Document is in-scope when one of its Chunks MENTIONS a Company held by a
# Fund the firm manages. A not-yet-enriched firm (no such chunks) legitimately yields [] — never the
# demo's filings. Same returned columns as _DOCS_CYPHER.
_DOCS_FIRM_CYPHER = """
MATCH (d:Document)
WHERE (d.sensitivity IS NULL OR d.sensitivity IN $entitlements)
  AND EXISTS {
        (:Chunk {doc_id: d.doc_id})-[:MENTIONS]->(:Company)
          <-[:HOLDS]-(:Fund)-[:MANAGED_BY]->(:Company {name: $firm})
      }
OPTIONAL MATCH (c:Chunk {doc_id: d.doc_id})
WITH d, c ORDER BY coalesce(c.page, '0')
RETURN d.doc_id AS doc_id, d.title AS title, d.doc_type AS doc_type,
       coalesce(d.sensitivity, 'public') AS sensitivity, d.filing_date AS filing_date,
       d.url AS url,
       collect(CASE WHEN c IS NULL THEN NULL
                    ELSE {chunk_id: c.chunk_id, text: c.text,
                          sensitivity: coalesce(c.sensitivity, 'public')} END) AS chunks
ORDER BY doc_id
"""


def _clean_props(props: dict) -> dict:
    return {k: v for k, v in (props or {}).items() if k != "embedding"}


def _driver():
    s = get_settings()
    return GraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password))


def shape_subgraph(rows: list[dict]) -> dict:
    """Pure: fold flat (n, r, m) rows into deduped nodes + edges. Null edge confidence
    (structural edges) normalizes to 1.0 so the UI's numeric confidence filter always works."""
    nodes: dict[str, dict] = {}
    edges: dict[str, dict] = {}
    for row in rows:
        for prefix in ("n", "m"):
            nid = row[f"{prefix}_id"]
            if nid not in nodes:
                nodes[nid] = {
                    "id": nid,
                    "type": row[f"{prefix}_type"],
                    "label": row[f"{prefix}_label"] or row[f"{prefix}_type"],
                    "props": _clean_props(row[f"{prefix}_props"]),
                }
        rid = row["r_id"]
        conf = row.get("r_conf")
        edges[rid] = {
            "id": rid,
            "source": row["n_id"],
            "target": row["m_id"],
            "type": row["r_type"],
            "confidence": 1.0 if conf is None else conf,
            "props": _clean_props(row["r_props"]),
        }
    return {"nodes": list(nodes.values()), "edges": list(edges.values())}


def shape_documents(rows: list[dict]) -> list[dict]:
    """Pure: build DocRecord[] (doc_id,title,doc_type,sensitivity,filing_date,url,text,spans[])."""
    out: list[dict] = []
    for row in rows:
        chunks = [c for c in (row.get("chunks") or []) if c]
        text = "\n\n".join(c["text"] for c in chunks if c.get("text"))
        spans = [
            {"chunk_id": c["chunk_id"], "quote": c["text"],
             "sensitivity": c.get("sensitivity", "public")}
            for c in chunks
            if c.get("text")
        ]
        out.append({
            "doc_id": row["doc_id"],
            "title": row.get("title") or row["doc_id"],
            "doc_type": row.get("doc_type") or "",
            "sensitivity": row.get("sensitivity") or "public",
            "filing_date": row.get("filing_date"),
            "url": row.get("url"),
            "text": text,
            "spans": spans,
        })
    return out


@router.get("/graph")
def get_graph(
    limit: int = Query(300, ge=1, le=2000),
    min_confidence: float = Query(0.0, ge=0.0, le=1.0),
    entitlements: list[str] = Query(default=["public"]),  # noqa: B008 (FastAPI idiom)
    firm: str | None = Query(default=None),
) -> dict:
    """Explorer subgraph, scoped to the active firm (or `?firm=<name>`).

    When a firm resolves → ONLY its subgraph (firm + funds + holdings + their supply/exposure edges
    + mention chunks). When none resolves (demo-less / fresh install) → the whole graph, so the
    explorer is never blank.
    """
    resolved = scope.resolve_firm(firm)
    driver = _driver()
    try:
        with driver.session() as sess:
            if resolved is None:
                rows = sess.run(
                    _SUBGRAPH_CYPHER, min_conf=min_confidence, limit=limit,
                    entitlements=entitlements,
                ).data()
            else:
                rows = sess.run(
                    _SUBGRAPH_FIRM_CYPHER, firm=resolved, min_conf=min_confidence, limit=limit,
                    entitlements=entitlements,
                ).data()
    finally:
        driver.close()
    return shape_subgraph(rows)


@router.get("/documents")
def get_documents(
    entitlements: list[str] = Query(default=["public"]),  # noqa: B008 (FastAPI idiom)
    firm: str | None = Query(default=None),
) -> list[dict]:
    """Document viewer, scoped to the active firm (or `?firm=<name>`).

    When a firm resolves → only Documents whose chunks mention one of its holdings (a not-yet-
    enriched firm legitimately yields `[]`, never the demo's filings). When none resolves → all
    Documents.
    """
    resolved = scope.resolve_firm(firm)
    driver = _driver()
    try:
        with driver.session() as sess:
            if resolved is None:
                rows = sess.run(_DOCS_CYPHER, entitlements=entitlements).data()
            else:
                rows = sess.run(
                    _DOCS_FIRM_CYPHER, firm=resolved, entitlements=entitlements
                ).data()
    finally:
        driver.close()
    return shape_documents(rows)
