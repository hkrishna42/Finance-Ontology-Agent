"""M7 Reg Assistant — integration tests against a live seeded Neo4j.

Skips when no seeded graph is reachable. Validates: 13F XML matches the committed golden when
built from the graph; the VALIDATED coverage-gap query reproduces (Growth → supply_chain +
geopolitical); the report SHA-256 is stable across two runs; DERIVED_FROM audit edges are written.
"""

from __future__ import annotations

from _apps_neo4j import neo4j_store_or_skip

from api.modules.reg_reports import coverage as cov
from api.modules.reg_reports import report_pack as rp
from api.modules.reg_reports import thirteen_f as tf
from api.stores.sqlite import connect, init_db

GROWTH = "Demo Growth Fund"
FOCUSED = "Demo Focused Growth Fund"


def test_13f_from_graph_matches_golden_both_funds():
    store = neo4j_store_or_skip()
    try:
        for fund in (GROWTH, FOCUSED):
            res = tf.build_13f_from_graph(store, fund)
            assert res["xml"] == tf.golden_path(fund).read_text()
    finally:
        store.close()


def test_coverage_gap_reproduces_live():
    store = neo4j_store_or_skip()
    try:
        res = cov.coverage_check(store, GROWTH)
        assert res["gap_categories"] == ["geopolitical", "supply_chain"]
        # cross-check the VALIDATED base query directly
        rows = store.run(cov.COVERAGE_GAP_CYPHER, fund=GROWTH)
        assert {r["category"] for r in rows} == {"geopolitical", "supply_chain"}
    finally:
        store.close()


def test_report_sha256_stable_across_two_runs_live():
    store = neo4j_store_or_skip()
    try:
        sha1 = rp.build_report(store, GROWTH)["sha256"]
        sha2 = rp.build_report(store, GROWTH)["sha256"]
        assert sha1 == sha2 and len(sha1) == 64
    finally:
        store.close()


def test_report_pack_writes_derived_from_edges_with_cleanup():
    store = neo4j_store_or_skip()
    conn = init_db(connect(":memory:"))
    try:
        res = rp.build_report(store, GROWTH, conn=conn, write_graph=True)
        assert res["registered"] is True
        assert res["derived_from"]["edges"] >= 1
        rid = res["report_id"]
        rows = store.run(
            "MATCH (gr:GeneratedReport {report_id:$rid})-[:DERIVED_FROM]->(src) "
            "RETURN count(src) AS n, gr.sha256 AS sha",
            rid=rid,
        )
        assert rows[0]["n"] >= 1
        assert rows[0]["sha"] == res["sha256"]
    finally:
        # cleanup: drop the synthetic GeneratedReport (and its DERIVED_FROM edges)
        store.run("MATCH (gr:GeneratedReport) DETACH DELETE gr")
        conn.close()
        store.close()


def test_report_html_renders_from_live_graph():
    store = neo4j_store_or_skip()
    try:
        res = rp.build_report(store, GROWTH, generated_at="2026-07-27T00:00:00+00:00")
        assert "Exposure &amp; Concentration Report" in res["html"]
        assert "supply_chain" in res["html"]
    finally:
        store.close()
