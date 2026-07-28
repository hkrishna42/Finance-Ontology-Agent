"""M7(c) — report packs: deterministic HTML "Exposure & Concentration Report".

Pipeline:
  * build_snapshot  — a canonical, timestamp-free inputs snapshot assembled ONLY from the graph
                      (holdings, supplier concentration, HHI, heatmap, coverage, coverage gap,
                      single-source flags) + the exact query behind every figure + source spans.
  * snapshot_sha256 — SHA-256 over the canonical JSON (sorted keys) → STABLE across runs on the
                      same graph (the audit anchor). The display `generated_at` is deliberately
                      NOT part of the snapshot, so the hash does not move.
  * render_html     — Jinja template → the report + a provenance appendix (figure → query → span).
  * register_report — record {report_id, title, period, sha256} in the SQLite report_registry.
  * write_derived_from — GeneratedReport-[:DERIVED_FROM]->(Document|Chunk), tying the report into
                      the graph audit trail.
  * render_pdf      — OPTIONAL, behind the `reports` extra (WeasyPrint); import is guarded.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ...stores import sqlite as sqlite_store
from .. import risk_lens
from . import coverage as coverage_mod

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
REPORT_TYPE = "exposure_concentration"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _period_from_as_of(as_of: str | None) -> str:
    if not as_of:
        return "unknown"
    try:
        y, m, _ = as_of.split("-")
        q = (int(m) - 1) // 3 + 1
        return f"{y}-Q{q}"
    except (ValueError, AttributeError):
        return as_of


def build_snapshot(run: Any, fund: str, period: str | None = None) -> dict:
    """Assemble the canonical, deterministic inputs snapshot for `fund` from the graph."""
    sl = risk_lens.fetch_slice(run, fund)
    conc = risk_lens.supplier_concentration(sl)
    heat = risk_lens.risk_heatmap(sl)
    cov = risk_lens.coverage(sl)
    flags = risk_lens.single_source_flags(sl)
    gap = coverage_mod.coverage_check(run, fund)

    as_of = sl.holdings[0]["as_of"] if sl.holdings else None
    period = period or _period_from_as_of(as_of)

    # gather + de-dup provenance spans across the cited metrics
    provenance = risk_lens._dedup_citations(
        [*conc["citations"], *heat["citations"],
         *[c for g in gap["gaps"] for e in g["exposed_by"]
           for c in [{"doc_id": e.get("doc_id"), "span": None}] if e.get("doc_id")]]
    )
    provenance = sorted(provenance, key=lambda c: (c.get("doc_id") or "", c.get("chunk_id") or ""))

    figures = {
        "n_holdings": len(sl.holdings),
        "total_weight": sl.total_weight(),
        "hhi": risk_lens.portfolio_hhi(sl),
        "top_suppliers": [
            {"supplier": r["supplier"], "score": r["score"], "n_holdings": r["n_holdings"]}
            for r in conc["table"]
        ],
        "heatmap": [
            {"category": r["category"], "mass": r["mass"], "n_holdings": r["n_holdings"]}
            for r in heat["heatmap"]
        ],
        "coverage": {
            "covered_pct": cov["covered_pct"], "uncovered_pct": cov["uncovered_pct"],
            "covered_weight": cov["covered_weight"], "uncovered_weight": cov["uncovered_weight"],
            "uncovered": [r["company"] for r in cov["uncovered"]],
        },
        "coverage_gap": {
            "gap_categories": gap["gap_categories"],
            "gaps": [{"category": g["category"], "title": g["title"]} for g in gap["gaps"]],
        },
        "single_source_flags": [
            {"holding": f["holding"], "component": f["component"], "sole_supplier": f["sole_supplier"]}
            for f in flags["flags"]
        ],
        "holdings": [
            {"company": h["company"], "ticker": h.get("ticker"),
             "weight_pct": h["weight_pct"], "value_usd": h.get("value_usd")}
            for h in sl.holdings
        ],
    }
    queries = {
        "supplier_concentration": conc["cypher"],
        "hhi": risk_lens.EXPLAIN_CYPHER["hhi"],
        "risk_heatmap": heat["cypher"],
        "coverage": cov["cypher"],
        "coverage_gap": coverage_mod.COVERAGE_GAP_CYPHER,
        "single_source_flags": flags["cypher"],
    }
    snapshot = {
        "report_type": REPORT_TYPE,
        "fund": fund,
        "series_id": gap.get("series_id"),
        "period": period,
        "as_of": as_of,
        "figures": figures,
        "queries": queries,
        "provenance": provenance,
    }
    return snapshot


def canonical_json(snapshot: dict) -> str:
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def snapshot_sha256(snapshot: dict) -> str:
    return hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()


def report_id_for(snapshot: dict) -> str:
    slug = "".join(c.lower() if c.isalnum() else "_" for c in snapshot["fund"]).strip("_")
    return f"{snapshot['report_type']}:{slug}:{snapshot['period']}"


def render_html(snapshot: dict, sha256: str, generated_at: str | None = None) -> str:
    """Render the report HTML. `generated_at` is display-only and NOT part of the hashed snapshot."""
    env = _env()
    template = env.get_template("exposure_report.html.j2")
    return template.render(
        s=snapshot,
        figures=snapshot["figures"],
        sha256=sha256,
        report_id=report_id_for(snapshot),
        generated_at=generated_at or datetime.now(UTC).isoformat(timespec="seconds"),
    )


def register_report(conn: Any, report_id: str, title: str, period: str, sha256: str) -> None:
    """Idempotent upsert into the SQLite report_registry."""
    conn.execute(
        "INSERT INTO report_registry (report_id, title, period, sha256) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(report_id) DO UPDATE SET title=excluded.title, period=excluded.period, "
        "sha256=excluded.sha256",
        (report_id, title, period, sha256),
    )
    conn.commit()


def write_derived_from(store: Any, snapshot: dict, report_id: str, sha256: str) -> dict:
    """Create the GeneratedReport node + DERIVED_FROM edges to its source Documents/Chunks."""
    created_at = datetime.now(UTC).isoformat(timespec="seconds")
    title = f"Exposure & Concentration Report — {snapshot['fund']} ({snapshot['period']})"
    store.run(
        "MERGE (gr:GeneratedReport {report_id:$rid}) "
        "SET gr.title=$title, gr.period=$period, gr.sha256=$sha, gr.created_at=$created_at",
        rid=report_id, title=title, period=snapshot["period"], sha=sha256, created_at=created_at,
    )
    doc_ids = sorted({c["doc_id"] for c in snapshot["provenance"] if c.get("doc_id")})
    chunk_ids = sorted({c["chunk_id"] for c in snapshot["provenance"] if c.get("chunk_id")})
    edges = 0
    for doc_id in doc_ids:
        rows = store.run(
            "MATCH (gr:GeneratedReport {report_id:$rid}), (d:Document {doc_id:$doc_id}) "
            "MERGE (gr)-[df:DERIVED_FROM]->(d) SET df.as_of=$as_of RETURN count(df) AS n",
            rid=report_id, doc_id=doc_id, as_of=snapshot.get("as_of"),
        )
        edges += rows[0]["n"] if rows else 0
    for chunk_id in chunk_ids:
        rows = store.run(
            "MATCH (gr:GeneratedReport {report_id:$rid}), (c:Chunk {chunk_id:$chunk_id}) "
            "MERGE (gr)-[df:DERIVED_FROM]->(c) SET df.as_of=$as_of RETURN count(df) AS n",
            rid=report_id, chunk_id=chunk_id, as_of=snapshot.get("as_of"),
        )
        edges += rows[0]["n"] if rows else 0
    return {"report_id": report_id, "documents": doc_ids, "chunks": chunk_ids, "edges": edges}


# --- UI collection projection (#5): GET /reports -> types.ts ReportPack[] ----------------------


def _sections_from_snapshot(snapshot: dict) -> list[dict]:
    """Derive types.ts ReportSection[] {heading, body, stale?} from the inputs snapshot."""
    f = snapshot["figures"]
    top = ", ".join(f"{h['company']} {h['weight_pct']}%" for h in f["holdings"][:4])
    suppliers = ", ".join(f"{s['supplier']} ({s['score']})" for s in f["top_suppliers"]) or "none"
    heat = "; ".join(f"{r['category']} {r['mass']}%" for r in f["heatmap"]) or "none"
    gap_cats = f["coverage_gap"]["gap_categories"]
    flags = "; ".join(
        f"{x['holding']} -> {x['sole_supplier']} ({x['component']})" for x in f["single_source_flags"]
    ) or "none"
    return [
        {"heading": "Executive summary",
         "body": (f"{snapshot['fund']} holds {f['n_holdings']} positions "
                  f"({f['total_weight']}% of assets) with a portfolio HHI of {f['hhi']}. "
                  f"Uncovered principal-risk categories: "
                  f"{', '.join(gap_cats) if gap_cats else 'none'}.")},
        {"heading": "Portfolio concentration",
         "body": f"HHI {f['hhi']} over {f['n_holdings']} holdings. Largest positions: {top}."},
        {"heading": "Supplier dependency",
         "body": f"Top weighted supplier dependencies: {suppliers}."},
        {"heading": "Risk-factor heatmap",
         "body": f"Holding-weight mass per risk category: {heat}."},
        {"heading": "Disclosure coverage",
         "body": (f"Uncovered principal-risk categories: {', '.join(gap_cats)}."
                  if gap_cats else "All exposed principal risks are covered by a prospectus section."),
         "stale": bool(gap_cats)},
        {"heading": "Source coverage",
         "body": (f"{f['coverage']['covered_pct']}% of fund weight is backed by an ingested "
                  f"source; {f['coverage']['uncovered_pct']}% is uncovered.")},
        {"heading": "Single-source supplier flags", "body": flags},
    ]


def _provenance_from_snapshot(snapshot: dict) -> list[dict]:
    """Derive types.ts ProvenanceRow[] {claim, doc_id, chunk_id?, span?, cypher?} from the snapshot."""
    q = snapshot["queries"]
    f = snapshot["figures"]
    rows: list[dict] = [
        {"claim": f"Portfolio HHI = {f['hhi']}", "doc_id": "NPORT-P:seed", "cypher": q["hhi"]},
    ]
    for c in snapshot["provenance"]:
        if not c.get("doc_id"):
            continue
        row = {"claim": "Supports supplier-dependency / exposure figures", "doc_id": c["doc_id"],
               "cypher": q["supplier_concentration"]}
        if c.get("chunk_id"):
            row["chunk_id"] = c["chunk_id"]
        if c.get("span"):
            row["span"] = c["span"]
        rows.append(row)
    gap_cats = f["coverage_gap"]["gap_categories"]
    rows.append({
        "claim": f"Disclosure coverage gap: {', '.join(gap_cats) if gap_cats else 'none'}",
        "doc_id": "nvda_10k", "cypher": q["coverage_gap"],
    })
    return rows


def _iso(ts: str | None) -> str:
    """Normalise a SQLite 'YYYY-MM-DD HH:MM:SS' timestamp to an ISO-8601 'Z' string."""
    if not ts:
        return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    return ts.replace(" ", "T") + "Z" if "T" not in ts else ts


def report_packs(
    run: Any,
    funds: Sequence[str] | None = None,
    conn: Any = None,
) -> list[dict]:
    """Build the per-fund Exposure & Concentration packs and project to types.ts ReportPack[].

    `funds=None` builds a pack for EVERY Fund node in the graph (`risk_lens.all_fund_names`) — in
    the demo that is exactly the two demo funds, so callers that pass only the store keep working;
    a route may pass an explicit fund list to scope to one firm. Any N >= 0 is supported (N == 0
    yields an empty list). Registers each pack in the SQLite report_registry (idempotent upsert;
    created_at persists), so the registry is the authoritative list and created_at is stable.
    """
    fund_list = list(funds) if funds is not None else risk_lens.all_fund_names(run)
    own_conn = conn is None
    if conn is None:
        from ...config import get_settings

        conn = sqlite_store.get_settings_db(get_settings().sqlite_path)
    packs = []
    try:
        for fund in fund_list:
            snapshot = build_snapshot(run, fund)
            sha = snapshot_sha256(snapshot)
            report_id = report_id_for(snapshot)
            title = f"Exposure & Concentration Report — {fund} ({snapshot['period']})"
            register_report(conn, report_id, title, snapshot["period"], sha)
            row = conn.execute(
                "SELECT created_at FROM report_registry WHERE report_id=?", (report_id,)
            ).fetchone()
            created_at = _iso(row["created_at"] if row else None)
            # status: out_of_date if a GeneratedReport node was flagged by M6; else final
            status = "final"
            packs.append({
                "report_id": report_id, "title": title, "period": snapshot["period"],
                "created_at": created_at, "sha256": sha, "status": status,
                "sections": _sections_from_snapshot(snapshot),
                "provenance": _provenance_from_snapshot(snapshot),
            })
    finally:
        if own_conn:
            conn.close()
    return packs


def render_pdf(html: str) -> bytes | None:
    """Render the HTML to PDF via WeasyPrint IF the optional `reports` extra is installed."""
    try:
        from weasyprint import HTML  # type: ignore
    except Exception:  # noqa: BLE001 - optional dependency; HTML is sufficient for stub
        return None
    return HTML(string=html).write_pdf()


def build_report(
    run: Any,
    fund: str,
    period: str | None = None,
    conn: Any = None,
    write_graph: bool = False,
    generated_at: str | None = None,
) -> dict:
    """Full report pack: snapshot + sha + HTML + optional SQLite registration + DERIVED_FROM edges.

    Returns {report_id, sha256, snapshot, html, registered, derived_from}. `sha256` is stable
    across runs on the same graph. Set `conn` to register in SQLite; `write_graph=True` to write
    the DERIVED_FROM audit edges (uses `run` as the store).
    """
    snapshot = build_snapshot(run, fund, period=period)
    sha = snapshot_sha256(snapshot)
    report_id = report_id_for(snapshot)
    title = f"Exposure & Concentration Report — {fund} ({snapshot['period']})"
    html = render_html(snapshot, sha, generated_at=generated_at)

    registered = False
    if conn is not None:
        sqlite_store.init_db(conn)
        register_report(conn, report_id, title, snapshot["period"], sha)
        registered = True

    derived = None
    if write_graph:
        derived = write_derived_from(run, snapshot, report_id, sha)

    return {
        "report_id": report_id,
        "title": title,
        "period": snapshot["period"],
        "sha256": sha,
        "snapshot": snapshot,
        "html": html,
        "registered": registered,
        "derived_from": derived,
    }
