"""M3 — validated analytics tools: read-only contract + Python wrappers + seed-graph results."""

from __future__ import annotations

import pytest

from api.config import get_settings
from api.stores.neo4j import Neo4jStore
from api.tools import analytics as A
from api.tools.cypher_guard import is_read_only

GROWTH = "Demo Growth Fund"
FOCUSED = "Demo Focused Growth Fund"


class _FakeStore:
    """Records the last query and returns canned rows (offline wrapper tests)."""

    def __init__(self, rows):
        self._rows = rows
        self.last_query = None
        self.last_params = None

    def run(self, query, **params):
        self.last_query = query
        self.last_params = params
        return self._rows


# --- read-only contract (always runs) ------------------------------------------------------


@pytest.mark.parametrize(
    "cypher",
    [
        A.SHARED_SUPPLIERS_CYPHER,
        A.CHOKEPOINT_CYPHER,
        A.COVERAGE_GAP_CYPHER,
        A.EXPOSURE_PATH_CYPHER,
        A.BOARD_ALUMNI_CYPHER,
        A.CONCENTRATION_CYPHER,
    ],
)
def test_all_analytics_queries_are_read_only(cypher):
    assert is_read_only(cypher)


def test_shared_suppliers_builds_graph_paths():
    store = _FakeStore([{"supplier": "TSMC", "holdings": ["NVIDIA", "AMD"], "n": 2}])
    res = A.shared_suppliers(store, GROWTH)
    assert res.tool == "shared_suppliers"
    assert store.last_params["fund"] == GROWTH
    assert {p["holding"] for p in res.graph_paths} == {"NVIDIA", "AMD"}
    assert all(p["supplier"] == "TSMC" for p in res.graph_paths)


def test_concentration_summary_computes_hhi():
    row = {
        "rows": [
            {"company": "A", "weight_pct": 10.0, "value_usd": 1},
            {"company": "B", "weight_pct": 5.0, "value_usd": 1},
        ],
        "total": 15.0,
        "hhi": 125.0,
        "n": 2,
    }
    res = A.concentration_summary(_FakeStore([row]), GROWTH)
    assert res.summary["n_holdings"] == 2
    assert res.summary["hhi"] == 125.0
    assert res.summary["top5_weight_pct"] == 15.0
    assert res.rows[0]["company"] == "A"  # sorted by weight desc


# --- seed-graph validation (guarded) -------------------------------------------------------


def _store_or_skip() -> Neo4jStore:
    store = Neo4jStore(get_settings())
    try:
        store.verify_connectivity()
    except Exception:  # noqa: BLE001
        store.close()
        pytest.skip("Neo4j not reachable; skipping analytics seed validation")
    return store


def test_seed_shared_supplier_growth_tsmc_held_by_4():
    store = _store_or_skip()
    try:
        rows = A.shared_suppliers(store, GROWTH).rows
        top = rows[0]
        assert top["supplier"] == "Taiwan Semiconductor Manufacturing"
        assert top["n"] == 4
        assert set(top["holdings"]) == {
            "NVIDIA",
            "Advanced Micro Devices",
            "Apple",
            "Broadcom",
        }
    finally:
        store.close()


def test_seed_second_order_chokepoint_asml_from_5():
    store = _store_or_skip()
    try:
        rows = A.second_order_chokepoints(store, GROWTH).rows
        top = rows[0]
        assert top["choke"] == "ASML Holding"
        assert top["reachable"] == 5
    finally:
        store.close()


def test_seed_shared_suppliers_fund_optional_across_all_funds():
    # fund=None → across all funds; TSMC is still the shared critical supplier of 4 held issuers.
    store = _store_or_skip()
    try:
        rows = A.shared_suppliers(store, None).rows
        top = rows[0]
        assert top["supplier"] == "Taiwan Semiconductor Manufacturing"
        assert top["n"] == 4
    finally:
        store.close()


def test_seed_coverage_gap_finds_uncovered_supply_chain_risk():
    store = _store_or_skip()
    try:
        rows = A.coverage_gap(store, GROWTH).rows
        risks = {r["uncovered_risk"] for r in rows}
        # The seed deliberately leaves the foundry/supply-chain risk uncovered by any prospectus.
        assert "Advanced foundry / supply concentration" in risks
    finally:
        store.close()


def test_seed_exposure_path_and_board_and_concentration():
    store = _store_or_skip()
    try:
        ep = A.exposure_path(store, "NVIDIA", "ASML Holding").rows[0]
        assert ep["path"] == ["NVIDIA", "Taiwan Semiconductor Manufacturing", "ASML Holding"]
        assert ep["hops"] == 2

        board = {r["person"]: r for r in A.board_and_alumni_links(store, "NVIDIA").rows}
        assert "Ada Interlock" in board and "Ben Alumni" in board

        conc = A.concentration_summary(store, FOCUSED).summary
        assert conc["n_holdings"] == 8
        assert conc["total_weight_pct"] == pytest.approx(70.5)
    finally:
        store.close()
