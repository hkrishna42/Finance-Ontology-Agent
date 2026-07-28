"""Neo4j-side firm operations backing the /firms registry (read + cascade-delete of a firm).

Thin Cypher helpers over ``api.stores.neo4j.Neo4jStore.run``; they never touch SQLite. The delete
removes the firm ``Company`` node and its ``Fund`` nodes/relationships but leaves shared issuer
``Company`` nodes (the securities a fund holds) intact.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps this module import offline
    from api.stores.neo4j import Neo4jStore


def firm_funds(store: Neo4jStore, firm_name: str) -> list[str]:
    """Fund names managed by ``firm_name``, alphabetically."""
    rows = store.run(
        "MATCH (f:Fund)-[:MANAGED_BY]->(:Company {name: $firm}) "
        "RETURN f.name AS name ORDER BY f.name",
        firm=firm_name,
    )
    return [r["name"] for r in rows]


def list_graph_firms(store: Neo4jStore) -> list[dict]:
    """Every firm in the graph with its managed funds: ``[{name, funds:[{name,series_id,cik,lei}]}]``."""
    return store.run(
        "MATCH (firm:Company)<-[:MANAGED_BY]-(f:Fund) "
        "RETURN firm.name AS name, "
        "collect({name: f.name, series_id: f.series_id, cik: f.cik, lei: f.lei}) AS funds"
    )


def delete_firm_graph(store: Neo4jStore, firm_name: str) -> dict:
    """Detach-delete the firm ``Company`` + its ``Fund`` nodes (shared issuer Companies preserved)."""
    store.run(
        "MATCH (firm:Company {name: $firm}) "
        "OPTIONAL MATCH (firm)<-[:MANAGED_BY]-(f:Fund) "
        "DETACH DELETE f, firm",
        firm=firm_name,
    )
    return {"deleted_firm": firm_name}
