"""Fixed, hand-written, guaranteed-correct analytics Cypher tools.

The Router prefers these over free text-to-Cypher because their queries are validated once and never
drift. Each tool returns an `AnalyticsResult` carrying the exact Cypher + params it ran (so the
answer is auditable and citable) alongside rows, optional `graph_paths` for the UI, and an optional
`summary`.

Validated base queries (against the shared seed graph):
  * shared critical supplier — Growth → TSMC held by 4 (NVDA, AMD, AAPL, AVGO)
  * 2nd-order chokepoint      — Growth → ASML reachable from 5 holdings (TSMC + those four)

All queries are strictly read-only (they pass `cypher_guard.is_read_only`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# --- result envelope -----------------------------------------------------------------------


@dataclass
class AnalyticsResult:
    tool: str
    cypher: str
    params: dict[str, Any]
    rows: list[dict[str, Any]]
    graph_paths: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)


# --- validated Cypher --------------------------------------------------------------------

# Shared critical supplier: fund holdings that all depend on the same critical supplier.
# `$fund` is optional (NULL → across all funds), so a fund-agnostic question still routes here.
SHARED_SUPPLIERS_CYPHER = (
    "MATCH (f:Fund)-[:HOLDS]->(h:Company)-[st:SUPPLIES_TO]->(sup:Company) "
    "WHERE ($fund IS NULL OR f.name = $fund) AND st.criticality = 'critical' "
    "  AND ($sensitivities IS NULL OR coalesce(st.sensitivity,'public') IN $sensitivities) "
    "RETURN sup.name AS supplier, collect(DISTINCT h.name) AS holdings, "
    "       count(DISTINCT h) AS n "
    "ORDER BY n DESC, supplier"
)

# Second-order chokepoint: nodes reachable in 1-2 supply hops from holdings that the fund does NOT
# itself hold (the hidden concentration). `$fund` optional (NULL → across all funds).
CHOKEPOINT_CYPHER = (
    "MATCH (f:Fund)-[:HOLDS]->(h:Company)-[:SUPPLIES_TO*1..2]->(choke:Company) "
    "WHERE ($fund IS NULL OR f.name = $fund) AND NOT (f)-[:HOLDS]->(choke) "
    "RETURN choke.name AS choke, count(DISTINCT h) AS reachable, "
    "       collect(DISTINCT h.name) AS via "
    "ORDER BY reachable DESC, choke"
)

# Disclosure coverage gap: risk factors a held issuer is EXPOSED_TO but NO prospectus section COVERS.
COVERAGE_GAP_CYPHER = (
    "MATCH (c:Company)<-[:HOLDS]-(f:Fund) "
    "WHERE $fund IS NULL OR f.name = $fund "
    "MATCH (c)-[:EXPOSED_TO]->(rf:RiskFactor) "
    "WHERE NOT EXISTS { MATCH (:DisclosureSection)-[:COVERS]->(rf) } "
    "RETURN DISTINCT rf.title AS uncovered_risk, rf.category AS category, "
    "       collect(DISTINCT c.name) AS exposed_issuers "
    "ORDER BY uncovered_risk"
)

EXPOSURE_PATH_CYPHER = (
    "MATCH p = shortestPath((a:Company {name:$a})-[:SUPPLIES_TO*1..5]->(b:Company {name:$b})) "
    "RETURN [n IN nodes(p) | n.name] AS path, "
    "       [r IN relationships(p) | coalesce(r.component, r.criticality, 'supplies_to')] AS edges, "
    "       length(p) AS hops"
)

BOARD_ALUMNI_CYPHER = (
    "MATCH (p:Person)-[r1:DIRECTOR_OF|OFFICER_OF]->(c:Company {name:$company}) "
    "OPTIONAL MATCH (p)-[r2:DIRECTOR_OF|OFFICER_OF|FORMERLY_AT]->(other:Company) "
    "WHERE other.name <> $company "
    "RETURN p.name AS person, type(r1) AS role_at_company, "
    "       collect(DISTINCT {company: other.name, link: type(r2)}) AS other_links "
    "ORDER BY person"
)

CONCENTRATION_CYPHER = (
    "MATCH (f:Fund {name:$fund})-[h:HOLDS]->(c:Company) "
    "WITH collect({company: c.name, weight_pct: h.weight_pct, value_usd: h.value_usd}) AS rows, "
    "     sum(h.weight_pct) AS total, sum(h.weight_pct * h.weight_pct) AS hhi, count(*) AS n "
    "RETURN rows, total, hhi, n"
)


# --- tools -------------------------------------------------------------------------------


def shared_suppliers(
    store: Any, fund: str | None = None, *, sensitivities: list[str] | None = None
) -> AnalyticsResult:
    """Critical suppliers shared across a fund's holdings (systemic single points of failure)."""
    params = {"fund": fund, "sensitivities": sensitivities}
    rows = store.run(SHARED_SUPPLIERS_CYPHER, **params)
    paths: list[dict[str, Any]] = []
    for r in rows:
        for h in r["holdings"]:
            paths.append({"fund": fund, "holding": h, "supplier": r["supplier"]})
    return AnalyticsResult(
        tool="shared_suppliers",
        cypher=SHARED_SUPPLIERS_CYPHER,
        params=params,
        rows=rows,
        graph_paths=paths,
    )


def second_order_chokepoints(store: Any, fund: str | None = None) -> AnalyticsResult:
    """Second-order (1-2 hop) supply chokepoints the fund is exposed to but does not hold."""
    params = {"fund": fund}
    rows = store.run(CHOKEPOINT_CYPHER, **params)
    paths = [{"fund": fund, "choke": r["choke"], "via": r["via"]} for r in rows]
    return AnalyticsResult(
        tool="second_order_chokepoints",
        cypher=CHOKEPOINT_CYPHER,
        params=params,
        rows=rows,
        graph_paths=paths,
    )


def coverage_gap(store: Any, fund: str | None = None) -> AnalyticsResult:
    """Risk factors that held issuers are exposed to but no fund prospectus section covers."""
    params = {"fund": fund}
    rows = store.run(COVERAGE_GAP_CYPHER, **params)
    paths = [
        {"risk": r["uncovered_risk"], "category": r["category"], "issuers": r["exposed_issuers"]}
        for r in rows
    ]
    return AnalyticsResult(
        tool="coverage_gap",
        cypher=COVERAGE_GAP_CYPHER,
        params=params,
        rows=rows,
        graph_paths=paths,
    )


def exposure_path(store: Any, a: str, b: str) -> AnalyticsResult:
    """Shortest supply-chain dependency path from company `a` to company `b`."""
    params = {"a": a, "b": b}
    rows = store.run(EXPOSURE_PATH_CYPHER, **params)
    paths = [{"path": r["path"], "edges": r["edges"], "hops": r["hops"]} for r in rows]
    return AnalyticsResult(
        tool="exposure_path",
        cypher=EXPOSURE_PATH_CYPHER,
        params=params,
        rows=rows,
        graph_paths=paths,
    )


def board_and_alumni_links(store: Any, company: str) -> AnalyticsResult:
    """Board interlocks + alumni paths from a company's directors/officers to other companies."""
    params = {"company": company}
    rows = store.run(BOARD_ALUMNI_CYPHER, **params)
    paths: list[dict[str, Any]] = []
    for r in rows:
        for link in r["other_links"]:
            if link.get("company"):
                paths.append(
                    {
                        "person": r["person"],
                        "from_company": company,
                        "to_company": link["company"],
                        "link": link["link"],
                    }
                )
    return AnalyticsResult(
        tool="board_and_alumni_links",
        cypher=BOARD_ALUMNI_CYPHER,
        params=params,
        rows=rows,
        graph_paths=paths,
    )


def concentration_summary(store: Any, fund: str) -> AnalyticsResult:
    """Per-holding weights + a Herfindahl-Hirschman (HHI) concentration summary for a fund."""
    params = {"fund": fund}
    raw = store.run(CONCENTRATION_CYPHER, **params)
    if not raw:
        return AnalyticsResult("concentration_summary", CONCENTRATION_CYPHER, params, [], summary={})
    row = raw[0]
    holdings = sorted(row["rows"], key=lambda x: x["weight_pct"], reverse=True)
    top5 = round(sum(h["weight_pct"] for h in holdings[:5]), 4)
    summary = {
        "n_holdings": row["n"],
        "total_weight_pct": round(row["total"], 4),
        "hhi": round(row["hhi"], 4),
        "top5_weight_pct": top5,
    }
    return AnalyticsResult(
        tool="concentration_summary",
        cypher=CONCENTRATION_CYPHER,
        params=params,
        rows=holdings,
        summary=summary,
    )


# Registry so the router can select a tool by name.
TOOLS: dict[str, Callable[..., AnalyticsResult]] = {
    "shared_suppliers": shared_suppliers,
    "second_order_chokepoints": second_order_chokepoints,
    "coverage_gap": coverage_gap,
    "exposure_path": exposure_path,
    "board_and_alumni_links": board_and_alumni_links,
    "concentration_summary": concentration_summary,
}
