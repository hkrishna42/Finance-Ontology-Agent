"""M5 Risk Lens — integration tests against a live seeded Neo4j.

Skips cleanly when no seeded graph is reachable (keeps the offline CI gate green). Run with a
seeded DB, e.g.:  NEO4J_URI=bolt://localhost:7688 uv run pytest tests/test_risk_neo4j.py
Asserts the pure-Python metrics match the same numbers the hand-verified aggregating Cypher
returns directly from the graph.
"""

from __future__ import annotations

from _apps_neo4j import neo4j_store_or_skip

from api.modules import risk_lens as rl

GROWTH = rl.GROWTH_FUND
FOCUSED = rl.FOCUSED_FUND


def test_supplier_concentration_matches_cypher():
    store = neo4j_store_or_skip()
    try:
        res = rl.supplier_concentration(rl.fetch_slice(store, GROWTH))
        # cross-check against the explain query run directly
        rows = store.run(rl.EXPLAIN_CYPHER["supplier_concentration"], fund=GROWTH)
        by_supplier = {r["supplier"]: round(r["score"], 6) for r in rows}
        for row in res["table"]:
            assert row["score"] == by_supplier[row["supplier"]]
        top = res["table"][0]
        assert top["supplier"] == "Taiwan Semiconductor Manufacturing"
        assert top["score"] == 27.5 and top["n_holdings"] == 4
    finally:
        store.close()


def test_hhi_focused_gt_growth_live():
    store = neo4j_store_or_skip()
    try:
        g = rl.portfolio_hhi(rl.fetch_slice(store, GROWTH))
        f = rl.portfolio_hhi(rl.fetch_slice(store, FOCUSED))
        assert g == 412.5 and f == 712.75 and f > g
    finally:
        store.close()


def test_heatmap_top_is_supply_chain_live():
    store = neo4j_store_or_skip()
    try:
        res = rl.risk_heatmap(rl.fetch_slice(store, GROWTH))
        assert res["top_category"] == "supply_chain"
        assert res["heatmap"][0]["mass"] == 30.0
    finally:
        store.close()


def test_coverage_uncovered_weight_live():
    store = neo4j_store_or_skip()
    try:
        res = rl.coverage(rl.fetch_slice(store, GROWTH))
        assert res["covered_pct"] == 25.0 and res["uncovered_pct"] == 75.0
    finally:
        store.close()


def test_second_order_chokepoint_asml_live():
    store = neo4j_store_or_skip()
    try:
        res = rl.second_order_chokepoints(rl.fetch_slice(store, GROWTH), k=2)
        nodes = {c["node"] for c in res["chokepoints"]}
        assert "ASML Holding" in nodes
        assert "Taiwan Semiconductor Manufacturing" not in nodes
    finally:
        store.close()


def test_compare_funds_live():
    store = neo4j_store_or_skip()
    try:
        cmp = rl.compare_funds(store, GROWTH, FOCUSED)
        assert cmp["more_concentrated_fund"] == FOCUSED
    finally:
        store.close()
