"""#5 — GET /reports ReportPack[] projection (offline).

Validates the ReportPack shape against web/src/types.ts and the derived sections/provenance,
using the seed-mirrored runner + an in-memory SQLite registry (no Neo4j / no network).
"""

from __future__ import annotations

from _apps_neo4j import neo4j_store_or_skip
from _apps_seed_fixtures import FOCUSED_FUND, GROWTH_FUND, seed_runner

from api.modules import risk_lens as rl
from api.modules.reg_reports import coverage as cov
from api.modules.reg_reports import report_pack as rp
from api.stores.sqlite import connect, init_db

GROWTH = "Demo Growth Fund"
FUNDS = (GROWTH_FUND, FOCUSED_FUND)


def _keys(d: dict) -> set[str]:
    return set(d.keys())


def test_get_reports_endpoint_live():
    """Seeded HTTP path: GET /reports on the wired app returns 200 ReportPack[]."""
    neo4j_store_or_skip().close()
    from fastapi.testclient import TestClient

    from api.main import app

    r = TestClient(app).get("/reports")
    assert r.status_code == 200
    d = r.json()
    assert isinstance(d, list) and len(d) == 2
    for p in d:
        assert _keys(p) == {"report_id", "title", "period", "created_at", "sha256",
                            "status", "sections", "provenance"}


def _packs():
    conn = init_db(connect(":memory:"))
    return rp.report_packs(seed_runner(), funds=FUNDS, conn=conn), conn


def test_reports_collection_is_array_of_reportpacks():
    packs, conn = _packs()
    try:
        assert isinstance(packs, list) and len(packs) == 2
        for p in packs:
            assert _keys(p) == {"report_id", "title", "period", "created_at", "sha256",
                                "status", "sections", "provenance"}
            assert p["status"] in {"final", "draft", "out_of_date"}
            assert len(p["sha256"]) == 64
            assert p["created_at"].endswith("Z")
    finally:
        conn.close()


def test_sections_match_ReportSection():
    packs, conn = _packs()
    try:
        growth = [p for p in packs if "Demo Growth Fund" in p["title"] and "Focused" not in p["title"]][0]
        headings = [s["heading"] for s in growth["sections"]]
        assert "Executive summary" in headings and "Disclosure coverage" in headings
        for s in growth["sections"]:
            assert _keys(s) <= {"heading", "body", "stale"}
            assert {"heading", "body"} <= _keys(s)
        cov = [s for s in growth["sections"] if s["heading"] == "Disclosure coverage"][0]
        # Growth has a real coverage gap -> section flagged stale
        assert cov["stale"] is True
        assert "supply_chain" in cov["body"] and "geopolitical" in cov["body"]
    finally:
        conn.close()


def test_provenance_match_ProvenanceRow():
    packs, conn = _packs()
    try:
        growth = [p for p in packs if "Focused" not in p["title"]][0]
        assert growth["provenance"]
        for row in growth["provenance"]:
            assert _keys(row) <= {"claim", "doc_id", "chunk_id", "span", "cypher"}
            assert {"claim", "doc_id"} <= _keys(row)
        # a cited source span from the TSMC dependency evidence is present
        assert any(r.get("span") and "TSMC" in r["span"] for r in growth["provenance"])
    finally:
        conn.close()


def test_sha256_stable_and_registry_populated():
    conn = init_db(connect(":memory:"))
    try:
        p1 = rp.report_packs(seed_runner(), funds=FUNDS, conn=conn)
        p2 = rp.report_packs(seed_runner(), funds=FUNDS, conn=conn)
        assert [p["sha256"] for p in p1] == [p["sha256"] for p in p2]
        # registry has exactly the 2 packs (idempotent upsert), created_at persisted
        rows = list(conn.execute("SELECT report_id, created_at FROM report_registry ORDER BY report_id"))
        assert len(rows) == 2
        first_created = {r["report_id"]: r["created_at"] for r in rows}
        rp.report_packs(seed_runner(), funds=FUNDS, conn=conn)
        rows2 = list(conn.execute("SELECT report_id, created_at FROM report_registry ORDER BY report_id"))
        assert {r["report_id"]: r["created_at"] for r in rows2} == first_created  # created_at unchanged
    finally:
        conn.close()


# --- N-fund generalization: default None -> all graph funds, and N in {0, 1, 3} ---------------


class _SyntheticRegRunner:
    """Offline `.run()` mapping every query report_packs/build_snapshot need for synthetic funds."""

    def __init__(self, holdings_by_fund: dict[str, list[tuple[str, float, int]]],
                 series_by_fund: dict[str, str]):
        self._h = holdings_by_fund
        self._series = series_by_fund

    def run(self, query: str, **params):
        fund = params.get("fund")
        if query == rl.ALL_FUNDS_CYPHER:
            return [{"name": name} for name in sorted(self._h)]
        if query == rl.CYPHER["holdings"]:
            return [
                {"company": c, "ticker": None, "cik": None, "weight_pct": w,
                 "value_usd": v, "as_of": "2026-05-31", "source": "test"}
                for (c, w, v) in self._h.get(fund, [])
            ]
        if query in (rl.CYPHER["supplies"], rl.CYPHER["exposures"], rl.CYPHER["provenance"]):
            return []
        if query == cov.SERIES_CYPHER:
            return [{"series_id": self._series.get(fund, "")}]
        if query in (cov.EXPOSED_CYPHER, cov.COVERED_CYPHER):
            return []
        raise KeyError(f"_SyntheticRegRunner: unmapped query:\n{query}")


def _seed_runner_all_funds():
    """seed_runner extended to answer ALL_FUNDS_CYPHER with the two demo funds (ORDER BY name)."""
    base = seed_runner()

    class _R:
        def run(self, query: str, **params):
            if query == rl.ALL_FUNDS_CYPHER:
                return [{"name": FOCUSED_FUND}, {"name": GROWTH_FUND}]
            return base.run(query, **params)

    return _R()


def test_report_packs_default_none_builds_all_graph_funds():
    """Backward-compat: report_packs(store) with no funds builds a pack for every Fund node."""
    conn = init_db(connect(":memory:"))
    try:
        packs = rp.report_packs(_seed_runner_all_funds(), conn=conn)
        assert len(packs) == 2
        assert {p["report_id"] for p in packs} == {
            "exposure_concentration:demo_growth_fund:2026-Q2",
            "exposure_concentration:demo_focused_growth_fund:2026-Q2",
        }
    finally:
        conn.close()


def test_report_packs_one_fund_seed_subset():
    """N == 1: an explicit single-fund list builds exactly one pack (no fixed 2-fund default)."""
    conn = init_db(connect(":memory:"))
    try:
        packs = rp.report_packs(seed_runner(), funds=(GROWTH_FUND,), conn=conn)
        assert len(packs) == 1
        assert GROWTH_FUND in packs[0]["title"]
        assert len(packs[0]["sha256"]) == 64
    finally:
        conn.close()


def test_report_packs_three_synthetic_funds():
    """N == 3: report_packs builds one pack per fund for an arbitrary N without IndexError."""
    runner = _SyntheticRegRunner(
        holdings_by_fund={
            "Fund A": [("Acme", 30.0, 300)],
            "Fund B": [("Beta", 20.0, 200)],
            "Fund C": [("Gamma", 50.0, 500)],
        },
        series_by_fund={"Fund A": "S1", "Fund B": "S2", "Fund C": "S3"},
    )
    conn = init_db(connect(":memory:"))
    try:
        packs = rp.report_packs(runner, funds=("Fund A", "Fund B", "Fund C"), conn=conn)
        assert len(packs) == 3
        assert {p["report_id"] for p in packs} == {
            "exposure_concentration:fund_a:2026-Q2",
            "exposure_concentration:fund_b:2026-Q2",
            "exposure_concentration:fund_c:2026-Q2",
        }
        for p in packs:
            assert _keys(p) == {"report_id", "title", "period", "created_at", "sha256",
                                "status", "sections", "provenance"}
    finally:
        conn.close()


def test_report_packs_zero_funds_empty():
    """N == 0: an empty graph yields an empty pack list, not an IndexError."""
    conn = init_db(connect(":memory:"))
    try:
        assert rp.report_packs(_SyntheticRegRunner({}, {}), conn=conn) == []
    finally:
        conn.close()
