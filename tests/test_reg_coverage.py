"""M7(b) — principal-risks coverage check: OFFLINE unit tests over seed-mirrored rows."""

from __future__ import annotations

from _apps_seed_fixtures import (
    FOCUSED_FUND,
    FUND_SERIES,
    GROWTH_FUND,
    covered_rows,
    exposed_rows,
)

from api.modules.reg_reports import coverage as cov


def test_growth_gap_is_supply_chain_and_geopolitical():
    res = cov.compute_coverage(exposed_rows(GROWTH_FUND), covered_rows(), FUND_SERIES[GROWTH_FUND])
    assert res["gap_categories"] == ["geopolitical", "supply_chain"]
    titles = {g["title"] for g in res["gaps"]}
    assert titles == {"Export controls and geopolitics", "Advanced foundry / supply concentration"}


def test_growth_gaps_are_cited_by_exposing_holdings():
    res = cov.compute_coverage(exposed_rows(GROWTH_FUND), covered_rows(), FUND_SERIES[GROWTH_FUND])
    supply = [g for g in res["gaps"] if g["category"] == "supply_chain"][0]
    exposed_companies = {e["company"] for e in supply["exposed_by"]}
    # NVIDIA, AMD, Apple, Broadcom, TSMC are all exposed to the uncovered supply-chain risk
    assert {"NVIDIA", "Advanced Micro Devices", "Apple", "Broadcom",
            "Taiwan Semiconductor Manufacturing"} == exposed_companies
    assert all(e.get("severity") for e in supply["exposed_by"])


def test_growth_overcoverage_empty():
    res = cov.compute_coverage(exposed_rows(GROWTH_FUND), covered_rows(), FUND_SERIES[GROWTH_FUND])
    # every Growth disclosure section maps to a risk the fund is actually exposed to
    assert res["overcoverage"] == []


def test_focused_gap_includes_regulatory():
    res = cov.compute_coverage(exposed_rows(FOCUSED_FUND), covered_rows(), FUND_SERIES[FOCUSED_FUND])
    # Focused covers only technology + customer_concentration -> reg/supply_chain/geo uncovered
    assert set(res["gap_categories"]) == {"geopolitical", "regulatory", "supply_chain"}


def test_base_query_string_is_the_validated_one():
    assert "NOT EXISTS" in cov.COVERAGE_GAP_CYPHER
    assert "ds.form_ref STARTS WITH f.series_id" in cov.COVERAGE_GAP_CYPHER
