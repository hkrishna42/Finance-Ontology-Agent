"""M5 Risk Lens — deterministic, explainable risk metrics over the firm ontology.

Every number is produced by Cypher over the seeded graph (never by the LLM); the LLM is used
only to *narrate* a briefing (Role.SYNTHESIZER / Role.IMPACT). Each metric returns:

    { ... numbers ..., "cypher": <the exact query a reviewer can run to reproduce the number>,
      "edges": [contributing graph edges], "citations": [doc_id/chunk_id/span provenance] }

so every figure is an "explain this number" away from its supporting subgraph.

The heavy metrics are computed in pure Python from a small per-fund `GraphSlice` (holdings +
SUPPLIES_TO adjacency + EXPOSED_TO + provenance). That keeps the logic unit-testable offline
(feed a seed-mirrored slice, assert the numbers) while the same slice, fetched from Neo4j via
`fetch_slice`, drives the live app. The embedded `cypher` strings are the hand-verified
aggregating queries that reproduce each headline number directly against the graph.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..providers.base import Role

# --- Domain constants ----------------------------------------------------------------------

# Criticality → weight multiplier for the weighted supplier-dependency concentration metric.
CRITICALITY_MULTIPLIER: dict[str, float] = {"critical": 1.0, "named": 0.5, "mentioned": 0.2}

CHOKEPOINT_MAX_HOPS = 2
DEFAULT_CHOKEPOINT_K = 2
# A source older than this many days (relative to a supplied as_of) is "stale" for coverage.
STALE_AFTER_DAYS = 180

GROWTH_FUND = "Demo Growth Fund"
FOCUSED_FUND = "Demo Focused Growth Fund"

# Enumerate every Fund node — the default universe for the firm-wide projections when no explicit
# fund list is supplied. ORDER BY name gives a stable, deterministic projection order.
ALL_FUNDS_CYPHER = "MATCH (f:Fund) RETURN f.name AS name ORDER BY name"


class Runner(Protocol):
    """Anything that can execute Cypher and return rows as dicts (e.g. Neo4jStore)."""

    def run(self, query: str, **params: Any) -> list[dict]: ...


# --- Fetch queries (the exact Cypher the slice is built from) -------------------------------

CYPHER: dict[str, str] = {
    "holdings": (
        "MATCH (f:Fund {name:$fund})-[h:HOLDS]->(co:Company)\n"
        "RETURN co.name AS company, co.ticker AS ticker, co.cik AS cik,\n"
        "       h.weight_pct AS weight_pct, h.value_usd AS value_usd,\n"
        "       h.as_of AS as_of, h.source AS source\n"
        "ORDER BY h.weight_pct DESC"
    ),
    # SUPPLIES_TO is a small global adjacency; reachability/clusters are computed in Python.
    "supplies": (
        "MATCH (a:Company)-[s:SUPPLIES_TO]->(b:Company)\n"
        "RETURN a.name AS src, b.name AS dst, s.criticality AS criticality,\n"
        "       s.component AS component, s.span AS span, s.doc_id AS doc_id,\n"
        "       s.chunk_id AS chunk_id, s.confidence AS confidence"
    ),
    "exposures": (
        "MATCH (f:Fund {name:$fund})-[h:HOLDS]->(co:Company)-[e:EXPOSED_TO]->(rf:RiskFactor)\n"
        "RETURN co.name AS company, h.weight_pct AS weight_pct,\n"
        "       rf.title AS risk_title, rf.category AS category,\n"
        "       e.severity_language AS severity, e.doc_id AS doc_id, e.chunk_id AS chunk_id"
    ),
    "provenance": (
        "MATCH (f:Fund {name:$fund})-[h:HOLDS]->(co:Company)\n"
        "OPTIONAL MATCH (c:Chunk)-[:MENTIONS]->(co)\n"
        "WITH co, h, c\n"
        "OPTIONAL MATCH (d:Document {doc_id: c.doc_id})\n"
        "RETURN co.name AS company, h.weight_pct AS weight_pct,\n"
        "       collect(DISTINCT c.chunk_id) AS chunks,\n"
        "       collect(DISTINCT d.doc_id) AS docs,\n"
        "       [x IN collect(DISTINCT d.filing_date) WHERE x IS NOT NULL] AS filing_dates"
    ),
}

# Hand-verified aggregating queries: each reproduces a headline number directly against the
# graph (attached to the metric result as `cypher` so the UI can "explain this number").
EXPLAIN_CYPHER: dict[str, str] = {
    "supplier_concentration": (
        "MATCH (f:Fund {name:$fund})-[h:HOLDS]->(:Company)-[s:SUPPLIES_TO]->(sup:Company)\n"
        "WITH sup.name AS supplier,\n"
        "     CASE s.criticality WHEN 'critical' THEN 1.0 WHEN 'named' THEN 0.5\n"
        "          WHEN 'mentioned' THEN 0.2 ELSE 0.0 END AS mult, h.weight_pct AS w\n"
        "RETURN supplier, sum(w*mult) AS score ORDER BY score DESC"
    ),
    "hhi": (
        "MATCH (f:Fund {name:$fund})-[h:HOLDS]->(:Company)\n"
        "RETURN sum(h.weight_pct*h.weight_pct) AS hhi, sum(h.weight_pct) AS total_weight"
    ),
    "shared_supplier_clusters": (
        "MATCH (f:Fund {name:$fund})-[:HOLDS]->(co:Company)"
        "-[:SUPPLIES_TO {criticality:'critical'}]->(sup:Company)\n"
        "WITH sup.name AS supplier, collect(DISTINCT co.name) AS holdings\n"
        "WHERE size(holdings) >= 2 RETURN supplier, holdings ORDER BY size(holdings) DESC"
    ),
    "second_order_chokepoints": (
        "MATCH (f:Fund {name:$fund})-[:HOLDS]->(co:Company)-[:SUPPLIES_TO*2..2]->(choke:Company)\n"
        "WITH choke.name AS choke, collect(DISTINCT co.name) AS reach\n"
        "WHERE size(reach) >= $k RETURN choke, reach ORDER BY size(reach) DESC"
    ),
    "risk_heatmap": (
        "MATCH (f:Fund {name:$fund})-[h:HOLDS]->(co:Company)-[:EXPOSED_TO]->(rf:RiskFactor)\n"
        "RETURN rf.category AS category, sum(h.weight_pct) AS mass,\n"
        "       count(DISTINCT co) AS holdings ORDER BY mass DESC"
    ),
    "single_source_flags": (
        "MATCH (f:Fund {name:$fund})-[:HOLDS]->(co:Company)"
        "-[s:SUPPLIES_TO {criticality:'critical'}]->(sup:Company)\n"
        "WITH co.name AS holding, s.component AS component, collect(DISTINCT sup.name) AS suppliers\n"
        "WHERE size(suppliers) = 1 RETURN holding, component, suppliers[0] AS sole_supplier"
    ),
    "coverage": (
        "MATCH (f:Fund {name:$fund})-[h:HOLDS]->(co:Company)\n"
        "OPTIONAL MATCH (c:Chunk)-[:MENTIONS]->(co)\n"
        "WITH h.weight_pct AS w, count(c) AS chunks\n"
        "RETURN sum(w) AS total_weight, sum(CASE WHEN chunks > 0 THEN w ELSE 0 END) AS covered_weight"
    ),
}


# --- The per-fund graph slice --------------------------------------------------------------


@dataclass
class GraphSlice:
    """The compact per-fund subgraph every metric reads. Built from Cypher or a test fixture."""

    fund: str
    holdings: list[dict] = field(default_factory=list)  # company, weight_pct, value_usd, ...
    supplies: list[dict] = field(default_factory=list)  # global SUPPLIES_TO edges
    exposures: list[dict] = field(default_factory=list)  # company, weight_pct, risk_title, ...
    provenance: list[dict] = field(default_factory=list)  # company, weight_pct, chunks, docs

    def weight_of(self) -> dict[str, float]:
        return {h["company"]: float(h["weight_pct"]) for h in self.holdings}

    def total_weight(self) -> float:
        return round(sum(float(h["weight_pct"]) for h in self.holdings), 6)

    def holding_names(self) -> set[str]:
        return {h["company"] for h in self.holdings}


def fetch_slice(run: Runner | Callable[..., list[dict]], fund: str) -> GraphSlice:
    """Materialise a `GraphSlice` for `fund` from the live graph."""
    exec_ = run.run if hasattr(run, "run") else run
    return GraphSlice(
        fund=fund,
        holdings=exec_(CYPHER["holdings"], fund=fund),
        supplies=exec_(CYPHER["supplies"]),
        exposures=exec_(CYPHER["exposures"], fund=fund),
        provenance=exec_(CYPHER["provenance"], fund=fund),
    )


def all_fund_names(run: Runner | Callable[..., list[dict]]) -> list[str]:
    """Every Fund node's name (the default universe for the firm-wide projections)."""
    exec_ = run.run if hasattr(run, "run") else run
    return [r["name"] for r in exec_(ALL_FUNDS_CYPHER) if r.get("name")]


# --- Small helpers -------------------------------------------------------------------------


def _mult(criticality: str | None) -> float:
    return CRITICALITY_MULTIPLIER.get((criticality or "").lower(), 0.0)


def _cite(edge: dict) -> dict:
    """Provenance citation for a contributing edge (only the fields that are present)."""
    out = {k: edge[k] for k in ("doc_id", "chunk_id", "span") if edge.get(k)}
    return out


def _adjacency(supplies: list[dict]) -> dict[str, list[dict]]:
    adj: dict[str, list[dict]] = defaultdict(list)
    for e in supplies:
        adj[e["src"]].append(e)
    return adj


# --- Metric 1: weighted supplier-dependency concentration + HHI ----------------------------


def supplier_concentration(sl: GraphSlice) -> dict:
    """Per external supplier S: Σ_holdings weight_pct(h) × criticality_multiplier(h→S)."""
    weight = sl.weight_of()
    holdings = sl.holding_names()
    scores: dict[str, float] = defaultdict(float)
    edges: dict[str, list[dict]] = defaultdict(list)
    for e in sl.supplies:
        src, dst = e["src"], e["dst"]
        if src not in holdings:
            continue
        contrib = weight[src] * _mult(e.get("criticality"))
        if contrib == 0.0:
            continue
        scores[dst] += contrib
        edges[dst].append(
            {
                "holding": src,
                "weight_pct": weight[src],
                "criticality": e.get("criticality"),
                "multiplier": _mult(e.get("criticality")),
                "component": e.get("component"),
                "contribution": round(contrib, 6),
                "citation": _cite(e),
            }
        )
    table = [
        {
            "supplier": s,
            "score": round(sc, 6),
            "dependent_holdings": sorted({c["holding"] for c in edges[s]}),
            "n_holdings": len({c["holding"] for c in edges[s]}),
            "edges": sorted(edges[s], key=lambda c: -c["weight_pct"]),
        }
        for s, sc in scores.items()
    ]
    table.sort(key=lambda r: (-r["score"], r["supplier"]))
    citations = [c["citation"] for row in table for c in row["edges"] if c["citation"]]
    return {
        "fund": sl.fund,
        "table": table,
        "hhi": portfolio_hhi(sl),
        "cypher": EXPLAIN_CYPHER["supplier_concentration"],
        "citations": _dedup_citations(citations),
    }


def portfolio_hhi(sl: GraphSlice) -> float:
    """HHI = Σ weight_pct² over the fund's holdings (concentration index)."""
    return round(sum(float(h["weight_pct"]) ** 2 for h in sl.holdings), 6)


# --- Metric 2: shared-critical-supplier clusters -------------------------------------------


def shared_supplier_clusters(sl: GraphSlice, min_holdings: int = 2) -> dict:
    """Holdings grouped by a common SUPPLIES_TO{criticality:critical} supplier."""
    holdings = sl.holding_names()
    by_supplier: dict[str, list[dict]] = defaultdict(list)
    for e in sl.supplies:
        if e["src"] in holdings and (e.get("criticality") or "").lower() == "critical":
            by_supplier[e["dst"]].append(e)
    clusters = []
    for supplier, es in by_supplier.items():
        members = sorted({e["src"] for e in es})
        if len(members) >= min_holdings:
            clusters.append(
                {
                    "supplier": supplier,
                    "holdings": members,
                    "n_holdings": len(members),
                    "component_examples": sorted({e.get("component") for e in es if e.get("component")}),
                    "citations": _dedup_citations([_cite(e) for e in es]),
                }
            )
    clusters.sort(key=lambda c: (-c["n_holdings"], c["supplier"]))
    return {
        "fund": sl.fund,
        "clusters": clusters,
        "cypher": EXPLAIN_CYPHER["shared_supplier_clusters"],
    }


# --- Metric 3: second-order chokepoints ----------------------------------------------------


def second_order_chokepoints(sl: GraphSlice, k: int = DEFAULT_CHOKEPOINT_K) -> dict:
    """Supplier-of-supplier nodes reachable within ≤2 hops from ≥k holdings (via ≥1 hop-2 path)."""
    adj = _adjacency(sl.supplies)
    holdings = sl.holding_names()
    # For every holding, BFS ≤2 hops; record the shortest hop distance to each reached node.
    reach: dict[str, dict[str, int]] = defaultdict(dict)  # node -> {holding: min_hops}
    for h in holdings:
        seen: dict[str, int] = {h: 0}
        dq: deque[tuple[str, int]] = deque([(h, 0)])
        while dq:
            node, depth = dq.popleft()
            if depth >= CHOKEPOINT_MAX_HOPS:
                continue
            for e in adj.get(node, []):
                nxt, nd = e["dst"], depth + 1
                if nxt not in seen or nd < seen[nxt]:
                    seen[nxt] = nd
                    dq.append((nxt, nd))
        for node, d in seen.items():
            if d >= 1 and (node not in reach or h not in reach[node] or d < reach[node][h]):
                reach[node][h] = d
    chokepoints = []
    for node, reachers in reach.items():
        # second-order = at least one holding reaches it via a 2-hop (intermediary) path
        if len(reachers) >= k and any(d == 2 for d in reachers.values()):
            chokepoints.append(
                {
                    "node": node,
                    "n_holdings": len(reachers),
                    "reachers": [
                        {"holding": hh, "hops": d} for hh, d in sorted(reachers.items())
                    ],
                }
            )
    chokepoints.sort(key=lambda c: (-c["n_holdings"], c["node"]))
    return {
        "fund": sl.fund,
        "k": k,
        "chokepoints": chokepoints,
        "cypher": EXPLAIN_CYPHER["second_order_chokepoints"],
    }


# --- Metric 4: risk-factor heatmap ---------------------------------------------------------


def risk_heatmap(sl: GraphSlice) -> dict:
    """Holding-weight mass per RiskFactor.category (each holding counted once per category)."""
    by_cat: dict[str, dict[str, dict]] = defaultdict(dict)  # category -> {company: exposure}
    for e in sl.exposures:
        cat = e["category"]
        # keep the first (only) exposure per (category, company); mass counts weight once
        by_cat[cat].setdefault(
            e["company"],
            {
                "company": e["company"],
                "weight_pct": float(e["weight_pct"]),
                "risk_title": e["risk_title"],
                "severity": e.get("severity"),
                "citation": _cite(e),
            },
        )
    rows = []
    for cat, comps in by_cat.items():
        mass = round(sum(c["weight_pct"] for c in comps.values()), 6)
        rows.append(
            {
                "category": cat,
                "mass": mass,
                "n_holdings": len(comps),
                "contributors": sorted(comps.values(), key=lambda c: -c["weight_pct"]),
            }
        )
    rows.sort(key=lambda r: (-r["mass"], r["category"]))
    citations = [c["citation"] for r in rows for c in r["contributors"] if c["citation"]]
    return {
        "fund": sl.fund,
        "heatmap": rows,
        "top_category": rows[0]["category"] if rows else None,
        "cypher": EXPLAIN_CYPHER["risk_heatmap"],
        "citations": _dedup_citations(citations),
    }


# --- Metric 5: single-source flags ---------------------------------------------------------


def single_source_flags(sl: GraphSlice) -> dict:
    """A critical supplier edge with no alternate supplier for the same (holding, component)."""
    holdings = sl.holding_names()
    # (holding, component) -> set of suppliers over ALL criticalities (alternates count)
    by_key: dict[tuple[str, str], dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for e in sl.supplies:
        if e["src"] not in holdings:
            continue
        key = (e["src"], e.get("component") or "")
        by_key[key][e["dst"]].append(e)
    flags = []
    for (holding, component), suppliers in by_key.items():
        crit = {
            sup: es
            for sup, es in suppliers.items()
            if any((e.get("criticality") or "").lower() == "critical" for e in es)
        }
        if len(crit) == 1 and len(suppliers) == 1:
            sole = next(iter(crit))
            es = crit[sole]
            flags.append(
                {
                    "holding": holding,
                    "component": component,
                    "sole_supplier": sole,
                    "citations": _dedup_citations([_cite(e) for e in es]),
                }
            )
    flags.sort(key=lambda f: (f["holding"], f["component"]))
    return {
        "fund": sl.fund,
        "flags": flags,
        "cypher": EXPLAIN_CYPHER["single_source_flags"],
    }


# --- Metric 6: coverage & staleness (the honesty metric) -----------------------------------


def _days_between(a: str, b: str) -> int | None:
    from datetime import date

    try:
        ya, ma, da = (int(x) for x in a.split("-"))
        yb, mb, db = (int(x) for x in b.split("-"))
        return (date(ya, ma, da) - date(yb, mb, db)).days
    except (ValueError, AttributeError):
        return None


def coverage(sl: GraphSlice, as_of: str | None = None) -> dict:
    """% of fund weight backed by ≥1 ingested source; uncovered weight reported honestly."""
    total = sl.total_weight()
    covered_w = 0.0
    covered, uncovered = [], []
    stale = []
    for p in sl.provenance:
        w = float(p["weight_pct"])
        chunks = [c for c in (p.get("chunks") or []) if c]
        docs = [d for d in (p.get("docs") or []) if d]
        row = {
            "company": p["company"],
            "weight_pct": w,
            "chunks": chunks,
            "docs": docs,
            "filing_dates": [d for d in (p.get("filing_dates") or []) if d],
        }
        if chunks or docs:
            covered_w += w
            covered.append(row)
            if as_of:
                ages = [_days_between(as_of, d) for d in row["filing_dates"]]
                ages = [a for a in ages if a is not None]
                if ages and min(ages) > STALE_AFTER_DAYS:
                    stale.append({**row, "age_days": min(ages)})
        else:
            uncovered.append(row)
    covered_w = round(covered_w, 6)
    pct = round(100.0 * covered_w / total, 4) if total else 0.0
    return {
        "fund": sl.fund,
        "total_weight": total,
        "covered_weight": covered_w,
        "uncovered_weight": round(total - covered_w, 6),
        "covered_pct": pct,
        "uncovered_pct": round(100.0 - pct, 4) if total else 0.0,
        "covered": sorted(covered, key=lambda r: -r["weight_pct"]),
        "uncovered": sorted(uncovered, key=lambda r: -r["weight_pct"]),
        "stale_sources": stale,
        "cypher": EXPLAIN_CYPHER["coverage"],
    }


def _dedup_citations(citations: list[dict]) -> list[dict]:
    seen, out = set(), []
    for c in citations:
        if not c:
            continue
        key = (c.get("doc_id"), c.get("chunk_id"), c.get("span"))
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


# --- Full report + cross-fund comparison ---------------------------------------------------


def risk_report(run: Runner | Callable[..., list[dict]], fund: str, as_of: str | None = None) -> dict:
    """Assemble all six metrics for one fund from the live graph."""
    sl = fetch_slice(run, fund)
    return report_from_slice(sl, as_of=as_of)


def report_from_slice(sl: GraphSlice, as_of: str | None = None) -> dict:
    """Assemble all six metrics from an already-materialised slice (offline-testable)."""
    return {
        "fund": sl.fund,
        "supplier_concentration": supplier_concentration(sl),
        "shared_supplier_clusters": shared_supplier_clusters(sl),
        "second_order_chokepoints": second_order_chokepoints(sl),
        "risk_heatmap": risk_heatmap(sl),
        "single_source_flags": single_source_flags(sl),
        "coverage": coverage(sl, as_of=as_of),
        "hhi": portfolio_hhi(sl),
        "n_holdings": len(sl.holdings),
    }


def compare_funds(
    run: Runner | Callable[..., list[dict]],
    fund_a: str = GROWTH_FUND,
    fund_b: str = FOCUSED_FUND,
    as_of: str | None = None,
) -> dict:
    """Cross-fund comparison (e.g. diversified Growth vs concentrated Focused)."""
    ra = risk_report(run, fund_a, as_of=as_of)
    rb = risk_report(run, fund_b, as_of=as_of)
    return comparison_from_reports([ra, rb])


def comparison_from_reports(reports: Sequence[dict]) -> dict:
    """Cross-fund comparison over N funds: per-fund headline metrics vs the firm-average HHI.

    Generalises the old exactly-two comparison to any N. Each fund carries `hhi_delta_vs_avg`
    (its HHI minus the firm-average HHI); `more_concentrated_fund` is the single highest-HHI fund.
    No positional `rows[0]`/`rows[1]` indexing — handles N in {0, 1, 2, ...} without IndexError.
    """

    def top_supplier(r: dict) -> dict | None:
        t = r["supplier_concentration"]["table"]
        return t[0] if t else None

    rows = []
    for r in reports:
        ts = top_supplier(r)
        rows.append(
            {
                "fund": r["fund"],
                "hhi": r["hhi"],
                "n_holdings": r["n_holdings"],
                "top_risk_category": r["risk_heatmap"]["top_category"],
                "top_supplier": ts["supplier"] if ts else None,
                "top_supplier_score": ts["score"] if ts else None,
                "top_supplier_n_holdings": ts["n_holdings"] if ts else None,
                "covered_pct": r["coverage"]["covered_pct"],
                "uncovered_pct": r["coverage"]["uncovered_pct"],
            }
        )
    avg_hhi = round(sum(x["hhi"] for x in rows) / len(rows), 6) if rows else 0.0
    for x in rows:
        x["hhi_delta_vs_avg"] = round(x["hhi"] - avg_hhi, 6)
    hhis = [x["hhi"] for x in rows]
    return {
        "funds": rows,
        "more_concentrated_fund": max(rows, key=lambda x: x["hhi"])["fund"] if rows else None,
        "avg_hhi": avg_hhi,
        # Spread between the most- and least-concentrated fund (0 when N < 2). For N == 2 this is
        # |hhi_a - hhi_b|, preserving the previous two-fund `hhi_delta` semantics.
        "hhi_delta": round(max(hhis) - min(hhis), 6) if hhis else 0.0,
    }


# --- Narration (LLM only narrates; all numbers above come from Cypher) ----------------------


# --- UI collection projection (#3): GET /risk -> types.ts RiskData -----------------------------

# RiskCategory order (mirrors api/ontology/schema.RiskCategory) for stable heatmap columns.
RISK_CATEGORY_ORDER = (
    "supply_chain", "customer_concentration", "regulatory", "geopolitical",
    "technology", "litigation", "other",
)

_SEV_HIGH = ("substantial", "material", "highly", "single", "critical", "cross-strait", "more than")
_SEV_MED = ("depend", "rely", "concentration", "limited number", "intense", "significant")


def _severity_from_language(text: str | None) -> int:
    """Map verbatim hedge language to a 0..3 heatmap severity (deterministic keyword bands)."""
    t = (text or "").lower()
    if not t:
        return 0
    if any(k in t for k in _SEV_HIGH):
        return 3
    if any(k in t for k in _SEV_MED):
        return 2
    return 1


def _interpret_hhi(
    hhi: float,
    top_weight: float,
    n: int,
    baseline_hhi: float | None = None,
    n_funds: int = 1,
) -> str:
    band = (
        "Highly concentrated" if top_weight >= 15
        else "Moderately concentrated" if top_weight >= 10
        else "Diversified"
    )
    parts = [f"{band} — HHI {hhi:.0f} over {n} holdings, top position {top_weight:.1f}%."]
    # Compare each fund to the firm-average HHI (generalises the old pairwise "sibling" wording to
    # N funds; with a single fund there is no baseline to compare against, so it is skipped).
    if n_funds > 1 and baseline_hhi and baseline_hhi > 0 and hhi > 0:
        ratio = hhi / baseline_hhi
        if ratio >= 1.05:
            parts.append(f"~{ratio:.1f}x more concentrated than the firm average.")
        elif ratio <= 0.95:
            parts.append(f"~{1 / ratio:.1f}x less concentrated than the firm average.")
    return " ".join(parts)


def _single_source_supplier_major(slices: dict[str, GraphSlice], funds: Sequence[str]) -> list[dict]:
    """Pivot critical SUPPLIES_TO into supplier-major single-source flags (types.ts SingleSourceFlag)."""
    supplies = next(iter(slices.values())).supplies if slices else []
    held_by_fund = {
        f: {h["company"]: float(h["weight_pct"]) for h in slices[f].holdings} for f in funds
    }
    all_held: set[str] = set().union(*[set(d) for d in held_by_fund.values()]) if held_by_fund else set()
    by_supplier: dict[str, dict] = {}
    for e in supplies:
        if e["src"] in all_held and (e.get("criticality") or "").lower() == "critical":
            entry = by_supplier.setdefault(e["dst"], {"dependents": set(), "edges": []})
            entry["dependents"].add(e["src"])
            entry["edges"].append(e)
    flags = []
    for supplier, entry in by_supplier.items():
        deps = sorted(entry["dependents"])
        exposed_funds = sorted(f for f in funds if any(d in held_by_fund[f] for d in deps))
        # aggregate = worst-case single-fund exposure (avoids double-counting overlapping issuers)
        agg = max(
            (sum(held_by_fund[f].get(d, 0.0) for d in deps) for f in exposed_funds), default=0.0
        )
        edges = [
            {"from": e["src"], "rel": "SUPPLIES_TO", "to": e["dst"],
             "qualifiers": {"criticality": e.get("criticality") or "",
                            "component": e.get("component") or ""}}
            for e in sorted(entry["edges"], key=lambda x: x["src"])
        ]
        flags.append({
            "supplier": supplier,
            "criticality": "critical",
            "dependents": deps,
            "exposed_funds": exposed_funds,
            "aggregate_weight_pct": round(agg, 6),
            "explain": {
                "cypher": EXPLAIN_CYPHER["shared_supplier_clusters"],
                "edges": edges,
                "note": ("Aggregate weight is the worst-case single fund; "
                         f"{len(deps)} held issuers critically depend on {supplier}."),
            },
        })
    flags.sort(key=lambda x: (-x["aggregate_weight_pct"], x["supplier"]))
    return flags


def risk_collection(
    run: Runner | Callable[..., list[dict]],
    funds: Sequence[str] | None = None,
) -> dict:
    """Firm-wide RiskData projection for GET /risk (types.ts RiskData). Numbers from Cypher.

    `funds=None` projects over EVERY Fund node in the graph (`ALL_FUNDS_CYPHER`) — in the demo that
    is exactly the two demo funds, so callers that pass only the store keep working; a route may
    pass an explicit fund list to scope to one firm. Any N >= 0 is supported (N == 0 yields empty
    concentration/hhi/heatmap/single_source with no IndexError).
    """
    fund_list = list(funds) if funds is not None else all_fund_names(run)
    slices = {f: fetch_slice(run, f) for f in fund_list}

    # concentration: every holding, top-weight first, across all funds
    concentration = []
    for f in fund_list:
        for h in slices[f].holdings:
            concentration.append({
                "fund": f, "issuer": h["company"], "ticker": h.get("ticker"),
                "weight_pct": float(h["weight_pct"]), "value_usd": int(h.get("value_usd") or 0),
            })
    concentration.sort(key=lambda r: (-r["weight_pct"], r["fund"], r["issuer"]))

    # hhi: one row per fund, interpreted against the firm-average HHI + explain (top-3 HOLDS edges)
    hhi_by_fund = {f: portfolio_hhi(slices[f]) for f in fund_list}
    avg_hhi = round(sum(hhi_by_fund.values()) / len(hhi_by_fund), 6) if hhi_by_fund else 0.0
    hhi_rows = []
    for f in fund_list:
        sl = slices[f]
        hhi = hhi_by_fund[f]
        top = max((float(h["weight_pct"]) for h in sl.holdings), default=0.0)
        edges = [
            {"from": f, "rel": "HOLDS", "to": h["company"],
             "qualifiers": {"weight_pct": str(h["weight_pct"])}}
            for h in sorted(sl.holdings, key=lambda x: -float(x["weight_pct"]))[:3]
        ]
        hhi_rows.append({
            "fund": f, "hhi": round(hhi), "top_weight_pct": round(top, 6),
            "interpretation": _interpret_hhi(hhi, top, len(sl.holdings), avg_hhi, len(fund_list)),
            "explain": {"cypher": EXPLAIN_CYPHER["hhi"], "edges": edges,
                        "note": "HHI = sum(weight_pct^2) over the fund's disclosed HOLDS."},
        })

    # heatmap: firm-wide company x RiskCategory matrix from EXPOSED_TO (company-level exposures)
    exposures_by_company: dict[str, dict[str, str | None]] = {}
    for f in fund_list:
        for e in slices[f].exposures:
            exposures_by_company.setdefault(e["company"], {})[e["category"]] = e.get("severity")
    companies = sorted(exposures_by_company)
    categories = [
        c for c in RISK_CATEGORY_ORDER
        if any(c in cats for cats in exposures_by_company.values())
    ]
    cells = []
    for co in companies:
        for cat, sev_lang in sorted(exposures_by_company[co].items()):
            cells.append({"company": co, "category": cat,
                          "severity": _severity_from_language(sev_lang),
                          "severity_language": sev_lang})
    heatmap = {"companies": companies, "categories": categories, "cells": cells}

    single_source = _single_source_supplier_major(slices, fund_list)
    return {"concentration": concentration, "hhi": hhi_rows,
            "heatmap": heatmap, "single_source": single_source}


def _template_narrative(report: dict) -> str:
    sc = report["supplier_concentration"]["table"]
    hm = report["risk_heatmap"]
    cov = report["coverage"]
    top = sc[0] if sc else None
    parts = [f"{report['fund']}: portfolio HHI {report['hhi']:.1f} across {report['n_holdings']} holdings."]
    if top:
        parts.append(
            f"Top supplier dependency is {top['supplier']} (weighted score {top['score']:.1f}, "
            f"shared by {top['n_holdings']} holdings)."
        )
    if hm["top_category"]:
        top_row = hm["heatmap"][0]
        parts.append(
            f"The dominant risk-factor category by weight is {hm['top_category']} "
            f"({top_row['mass']:.1f}% of fund weight)."
        )
    parts.append(
        f"Only {cov['covered_pct']:.1f}% of fund weight is backed by an ingested source; "
        f"{cov['uncovered_pct']:.1f}% remains uncovered."
    )
    return " ".join(parts)


def narrate(provider: Any, report: dict, role: Role = Role.SYNTHESIZER) -> dict:
    """Produce a cited briefing. Numbers are deterministic; the LLM only phrases the prose.

    Returns {narrative, llm_note, role, narration_unavailable}. `narrative` is the number-bearing,
    reproducible summary (always present); `llm_note` is the raw provider output. If the provider
    call fails (e.g. zero-credit 400 in full mode) the endpoint STILL returns the deterministic
    narrative and sets `narration_unavailable` to the reason — never a 500 (bug #2).
    """
    narrative = _template_narrative(report)
    system = (
        "You are a buy-side risk analyst. Narrate the supplied risk metrics faithfully. "
        "Do not invent numbers; only rephrase the provided figures."
    )
    user = f"Fund: {report['fund']}. Metrics summary: {narrative}"
    llm_note: str | None = None
    narration_unavailable: str | None = None
    try:
        completion = provider.complete(
            role=role, system=system, messages=[{"role": "user", "content": user}]
        )
        llm_note = completion.text
        # If a real/fixture narration is available (not the canned stub token), prefer it.
        if llm_note and not llm_note.startswith("[fake:"):
            narrative = llm_note
    except Exception as exc:  # noqa: BLE001 - degrade gracefully; numbers already computed
        narration_unavailable = f"LLM narration unavailable: {exc}"
    return {
        "narrative": narrative,
        "llm_note": llm_note,
        "role": role.value,
        "narration_unavailable": narration_unavailable,
    }
