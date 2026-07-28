"""M2 / L2 — deterministic N-PORT-P parser tests.

Pure-offline parse tests always run. The write+query test is guarded: it skips unless the
worktree's isolated Neo4j is reachable (bolt://localhost:7687 by default).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from api.config import get_settings
from api.l2 import nport
from api.stores.neo4j import Neo4jStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "nport"
GROWTH = FIXTURES / "demo_growth_S000090001.xml"
FOCUSED = FIXTURES / "demo_focused_S000090002.xml"


# --- offline parse tests -------------------------------------------------------------------


def test_parse_growth_fund_identity():
    filing = nport.parse_nport(GROWTH)
    assert filing.fund_name == "Demo Growth Fund"
    assert filing.series_id == "S000090001"
    assert filing.cik == "0009000001"
    assert filing.lei == "DEMOGROWTHFUND000001"
    assert filing.as_of == "2026-05-31"
    assert len(filing.holdings) == 9


def test_parse_focused_fund_identity():
    filing = nport.parse_nport(FOCUSED)
    assert filing.fund_name == "Demo Focused Growth Fund"
    assert filing.series_id == "S000090002"
    assert filing.cik == "0009000002"
    assert filing.lei == "DEMOFOCUSEDGRWTH0001"
    assert len(filing.holdings) == 8


def test_parse_holding_values_and_identifiers():
    filing = nport.parse_nport(GROWTH)
    by_name = {h.name: h for h in filing.holdings}
    nvda = by_name["NVIDIA"]
    assert nvda.weight_pct == 11.5
    assert nvda.value_usd == 920000000.0
    assert nvda.ticker == "NVDA"
    assert nvda.cusip == "67066G104"
    assert nvda.shares == 2400000.0
    # canonical issuer names align with the shared demo graph (so HOLDS attach to seed nodes)
    assert "Taiwan Semiconductor Manufacturing" in by_name
    assert "Advanced Micro Devices" in by_name


def test_holds_rows_carry_structured_parse_provenance():
    filing = nport.parse_nport(GROWTH)
    rows = nport.holds_rows(filing, source="NPORT-P:test")
    assert len(rows) == 9
    for r in rows:
        assert r["method"] == "structured_parse"
        assert r["confidence"] == 1.0
        assert r["source"] == "NPORT-P:test"
        assert r["as_of"] == "2026-05-31"
        assert r["weight_pct"] > 0
        assert r["value_usd"] > 0
        assert r["fund_name"] == "Demo Growth Fund"


def test_parse_accepts_raw_xml_string_and_is_namespace_robust():
    xml = GROWTH.read_text()
    filing = nport.parse_nport(xml)
    assert filing.series_id == "S000090001"
    # strip the default namespace entirely → still parses (namespace-agnostic walk)
    stripped = xml.replace(' xmlns="http://www.sec.gov/edgar/nport"', "")
    filing2 = nport.parse_nport(stripped)
    assert filing2.series_id == "S000090001"
    assert len(filing2.holdings) == 9


def test_cypher_write_plan_shape():
    filing = nport.parse_nport(FOCUSED)
    plan = nport.cypher_writes(filing)
    # 1 Fund MERGE + one HOLDS MERGE per holding
    assert len(plan) == 1 + 8
    fund_query, fund_params = plan[0]
    assert "MERGE (f:Fund" in fund_query
    assert fund_params["series_id"] == "S000090002"
    holds_query, holds_params = plan[1]
    assert "HOLDS" in holds_query
    assert holds_params["method"] == "structured_parse"


# --- guarded integration test (needs the worktree Neo4j) -----------------------------------


def _store_or_skip() -> Neo4jStore:
    store = Neo4jStore(get_settings())
    try:
        store.verify_connectivity()
    except Exception:  # noqa: BLE001 - offline CI: skip rather than fail
        store.close()
        pytest.skip("Neo4j not reachable; skipping N-PORT write/query integration test")
    return store


def _restore_seed_holds(store: Neo4jStore) -> None:
    """Undo this test's mutation of the SHARED seed HOLDS edges so it can't pollute order-dependent
    DB tests (e.g. the 13F golden, which asserts sshPrnamt=0 because the seed carries no shares).

    The N-PORT writer MERGEs onto the same HOLDS edges the seed created, adding `shares`/`method`/
    `confidence`, overwriting `source`, and adding an issuer LEI. This restores the exact seed
    state: HOLDS carry no shares/method/confidence and source='NPORT-P:seed'; issuers carry no LEI.
    """
    names = sorted({h.name for path in (GROWTH, FOCUSED) for h in nport.parse_nport(path).holdings})
    store.run(
        "MATCH (f:Fund)-[r:HOLDS]->(:Company) WHERE f.name IN $funds "
        "REMOVE r.shares, r.method, r.confidence SET r.source = 'NPORT-P:seed'",
        funds=["Demo Growth Fund", "Demo Focused Growth Fund"],
    )
    store.run("MATCH (c:Company) WHERE c.name IN $names REMOVE c.lei", names=names)


def test_write_and_query_both_funds_holds():
    store = _store_or_skip()
    try:
        gf = nport.load_and_write(store, GROWTH)
        ff = nport.load_and_write(store, FOCUSED)
        assert gf.fund_name == "Demo Growth Fund"
        assert ff.fund_name == "Demo Focused Growth Fund"

        rows = store.run(
            "MATCH (f:Fund)-[r:HOLDS]->(c:Company) "
            "WHERE r.method = 'structured_parse' "
            "RETURN f.name AS fund, count(r) AS n, sum(r.weight_pct) AS wt "
            "ORDER BY fund"
        )
        by_fund = {r["fund"]: r for r in rows}
        assert by_fund["Demo Growth Fund"]["n"] == 9
        assert by_fund["Demo Focused Growth Fund"]["n"] == 8
        # weighted HOLDS present and non-trivial
        assert by_fund["Demo Focused Growth Fund"]["wt"] == pytest.approx(70.5)

        # NVIDIA position resolves onto the same Company node the seed supply-chain hangs off
        nvda = store.run(
            "MATCH (f:Fund {name:'Demo Growth Fund'})-[r:HOLDS]->(c:Company {name:'NVIDIA'}) "
            "RETURN r.weight_pct AS w, r.value_usd AS v"
        )
        assert nvda and nvda[0]["w"] == 11.5 and nvda[0]["v"] == 920000000
    finally:
        # Restore the shared graph to its pre-test (seed) state — test isolation (bug #9).
        _restore_seed_holds(store)
        store.close()
