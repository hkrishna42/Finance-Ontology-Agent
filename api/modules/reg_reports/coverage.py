"""M7(b) — principal-risks coverage check: graph exposure heatmap vs DisclosureSection/COVERS.

Reports, per fund and cited both ways:
  * gaps         — RiskFactors a held company is EXPOSED_TO but no matching prospectus section
                   COVERS (the VALIDATED base query); each cited by the exposing holdings.
  * overcoverage — prospectus sections that COVER a RiskFactor no held company is exposed to;
                   each cited by the covering section.

The VALIDATED base query (Growth → supply_chain + geopolitical uncovered) is exposed verbatim as
`COVERAGE_GAP_CYPHER`. Pure `compute_coverage` runs the same logic over rows for offline tests.
"""

from __future__ import annotations

from typing import Any

# The VALIDATED base query (from the milestone spec) — the canonical "explain the gap" query.
COVERAGE_GAP_CYPHER = (
    "MATCH (f:Fund {name:$fund})-[:HOLDS]->(:Company)-[:EXPOSED_TO]->(rf:RiskFactor)\n"
    "WHERE NOT EXISTS {\n"
    "  MATCH (ds:DisclosureSection)-[:COVERS]->(rf) WHERE ds.form_ref STARTS WITH f.series_id\n"
    "}\n"
    "RETURN DISTINCT rf.category AS category, rf.title AS title ORDER BY category, title"
)

OVERCOVERAGE_CYPHER = (
    "MATCH (f:Fund {name:$fund})\n"
    "MATCH (ds:DisclosureSection)-[:COVERS]->(rf:RiskFactor)\n"
    "WHERE ds.form_ref STARTS WITH f.series_id\n"
    "  AND NOT EXISTS { MATCH (f)-[:HOLDS]->(:Company)-[:EXPOSED_TO]->(rf) }\n"
    "RETURN ds.key AS section_key, ds.form_ref AS form_ref, ds.title AS section_title,\n"
    "       rf.category AS category, rf.title AS risk_title ORDER BY section_key"
)

# Inputs for the pure computation (also usable directly for the graph heatmap side).
EXPOSED_CYPHER = (
    "MATCH (f:Fund {name:$fund})-[h:HOLDS]->(co:Company)-[e:EXPOSED_TO]->(rf:RiskFactor)\n"
    "RETURN co.name AS company, h.weight_pct AS weight_pct, rf.title AS risk_title,\n"
    "       rf.category AS category, e.severity_language AS severity, e.doc_id AS doc_id\n"
    "ORDER BY rf.category, co.name"
)
COVERED_CYPHER = (
    "MATCH (ds:DisclosureSection)-[:COVERS]->(rf:RiskFactor)\n"
    "RETURN ds.key AS section_key, ds.form_ref AS form_ref, ds.title AS section_title,\n"
    "       rf.title AS risk_title, rf.category AS category ORDER BY section_key"
)
SERIES_CYPHER = "MATCH (f:Fund {name:$fund}) RETURN f.series_id AS series_id"


def compute_coverage(exposed_rows: list[dict], covered_rows: list[dict], series_id: str) -> dict:
    """Pure coverage logic. `covered_rows` may include all sections; filtered by series_id here."""
    fund_covered = [r for r in covered_rows if (r.get("form_ref") or "").startswith(series_id)]
    covered_titles = {r["risk_title"] for r in fund_covered}

    # exposed risk factors, grouped by title, with the exposing holdings as citations
    exposed_by: dict[str, dict] = {}
    for r in exposed_rows:
        e = exposed_by.setdefault(
            r["risk_title"],
            {"title": r["risk_title"], "category": r["category"], "exposed_by": []},
        )
        e["exposed_by"].append(
            {"company": r["company"], "weight_pct": r.get("weight_pct"),
             "severity": r.get("severity"), "doc_id": r.get("doc_id")}
        )

    gaps = []
    for title, e in exposed_by.items():
        if title not in covered_titles:
            gaps.append(
                {"category": e["category"], "title": title,
                 "exposed_by": sorted(e["exposed_by"], key=lambda c: -(c["weight_pct"] or 0))}
            )
    gaps.sort(key=lambda g: (g["category"], g["title"]))

    exposed_titles = set(exposed_by)
    overcoverage = []
    for r in fund_covered:
        if r["risk_title"] not in exposed_titles:
            overcoverage.append(
                {"section_key": r["section_key"], "form_ref": r.get("form_ref"),
                 "section_title": r.get("section_title"), "category": r["category"],
                 "risk_title": r["risk_title"]}
            )
    overcoverage.sort(key=lambda o: o["section_key"])

    return {
        "series_id": series_id,
        "gap_categories": sorted({g["category"] for g in gaps}),
        "gaps": gaps,
        "overcoverage": overcoverage,
        "covered": sorted(
            [{"section_key": r["section_key"], "form_ref": r.get("form_ref"),
              "risk_title": r["risk_title"], "category": r["category"]} for r in fund_covered],
            key=lambda r: r["section_key"],
        ),
        "summary": {
            "n_gaps": len(gaps), "n_overcoverage": len(overcoverage),
            "n_exposed_risk_factors": len(exposed_titles), "n_covered": len(covered_titles),
        },
        "cypher": {"gaps": COVERAGE_GAP_CYPHER, "overcoverage": OVERCOVERAGE_CYPHER},
    }


def coverage_check(run: Any, fund: str) -> dict:
    """Run the coverage check for a fund against the live graph."""
    exec_ = run.run if hasattr(run, "run") else run
    series_rows = exec_(SERIES_CYPHER, fund=fund)
    series_id = series_rows[0]["series_id"] if series_rows else ""
    exposed_rows = exec_(EXPOSED_CYPHER, fund=fund)
    covered_rows = exec_(COVERED_CYPHER)
    result = compute_coverage(exposed_rows, covered_rows, series_id)
    result["fund"] = fund
    return result
