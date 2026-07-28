"""M5 Risk Lens — OFFLINE unit tests over the seed-mirrored slice (no Neo4j, no network).

Every assertion is a hand-checked acceptance number from the shared demo graph.
"""

from __future__ import annotations

from _apps_seed_fixtures import focused_slice, growth_slice

from api.modules import risk_lens as rl
from api.providers.base import Role
from api.providers.fake import FakeProvider

# --- Metric 1: weighted supplier-dependency concentration + HHI ----------------------------


def test_supplier_concentration_growth_tsmc_shared_by_four():
    res = rl.supplier_concentration(growth_slice())
    top = res["table"][0]
    assert top["supplier"] == "Taiwan Semiconductor Manufacturing"
    assert top["score"] == 27.5  # 11.5(NVDA)+3.5(AMD)+7.5(AAPL)+5.0(AVGO), all critical x1.0
    assert top["n_holdings"] == 4
    assert set(top["dependent_holdings"]) == {
        "NVIDIA", "Advanced Micro Devices", "Apple", "Broadcom"
    }
    # ASML appears via TSMC (a Growth holding) supplying it.
    asml = [r for r in res["table"] if r["supplier"] == "ASML Holding"][0]
    assert asml["score"] == 2.5
    # citations carry the verbatim spans
    assert any("TSMC" in (c.get("span") or "") for c in res["citations"])


def test_supplier_concentration_focused_tsmc_higher_score():
    res = rl.supplier_concentration(focused_slice())
    top = res["table"][0]
    assert top["supplier"] == "Taiwan Semiconductor Manufacturing"
    assert top["score"] == 37.5  # 16(NVDA)+5(AMD)+9(AAPL)+7.5(AVGO)


def test_hhi_focused_greater_than_growth():
    g = rl.portfolio_hhi(growth_slice())
    f = rl.portfolio_hhi(focused_slice())
    assert g == 412.5
    assert f == 712.75
    assert f > g


def test_criticality_multiplier_contract():
    assert rl.CRITICALITY_MULTIPLIER == {"critical": 1.0, "named": 0.5, "mentioned": 0.2}


# --- Metric 2: shared-critical-supplier clusters -------------------------------------------


def test_shared_supplier_clusters_growth():
    res = rl.shared_supplier_clusters(growth_slice())
    assert len(res["clusters"]) == 1
    cluster = res["clusters"][0]
    assert cluster["supplier"] == "Taiwan Semiconductor Manufacturing"
    assert cluster["n_holdings"] == 4
    assert cluster["citations"]


# --- Metric 3: second-order chokepoints ----------------------------------------------------


def test_second_order_chokepoint_is_asml():
    res = rl.second_order_chokepoints(growth_slice(), k=2)
    nodes = {c["node"] for c in res["chokepoints"]}
    assert "ASML Holding" in nodes
    # TSMC is a *first*-order supplier of the holdings, not a second-order chokepoint.
    assert "Taiwan Semiconductor Manufacturing" not in nodes
    asml = [c for c in res["chokepoints"] if c["node"] == "ASML Holding"][0]
    # reached via a 2-hop path from the 4 holdings (plus TSMC at 1 hop, itself a holding)
    two_hop = {r["holding"] for r in asml["reachers"] if r["hops"] == 2}
    assert two_hop == {"NVIDIA", "Advanced Micro Devices", "Apple", "Broadcom"}


def test_second_order_chokepoint_focused_asml_four_holdings():
    res = rl.second_order_chokepoints(focused_slice(), k=2)
    asml = [c for c in res["chokepoints"] if c["node"] == "ASML Holding"][0]
    # Focused does not hold TSMC, so ASML is reached only via 2-hop paths from 4 holdings.
    assert asml["n_holdings"] == 4
    assert all(r["hops"] == 2 for r in asml["reachers"])


# --- Metric 4: risk-factor heatmap ---------------------------------------------------------


def test_heatmap_supply_chain_is_top_category_growth():
    res = rl.risk_heatmap(growth_slice())
    assert res["top_category"] == "supply_chain"
    top = res["heatmap"][0]
    assert top["category"] == "supply_chain"
    assert top["mass"] == 30.0  # NVDA 11.5 + AAPL 7.5 + AVGO 5.0 + AMD 3.5 + TSMC 2.5
    assert top["n_holdings"] == 5


def test_heatmap_supply_chain_is_top_category_focused():
    res = rl.risk_heatmap(focused_slice())
    assert res["top_category"] == "supply_chain"
    assert res["heatmap"][0]["mass"] == 37.5  # NVDA16 + AAPL9 + AVGO7.5 + AMD5


def test_heatmap_all_categories_present_growth():
    res = rl.risk_heatmap(growth_slice())
    masses = {r["category"]: r["mass"] for r in res["heatmap"]}
    assert masses == {
        "supply_chain": 30.0, "regulatory": 16.5, "technology": 15.0,
        "geopolitical": 14.0, "customer_concentration": 11.5,
    }


# --- Metric 5: single-source flags ---------------------------------------------------------


def test_single_source_flags_growth_all_foundry_deps():
    res = rl.single_source_flags(growth_slice())
    flagged = {(f["holding"], f["sole_supplier"]) for f in res["flags"]}
    # every critical foundry dependency in the seed is single-sourced
    assert ("NVIDIA", "Taiwan Semiconductor Manufacturing") in flagged
    assert ("Apple", "Taiwan Semiconductor Manufacturing") in flagged
    assert ("Taiwan Semiconductor Manufacturing", "ASML Holding") in flagged
    assert all(f["citations"] for f in res["flags"])


# --- Metric 6: coverage & staleness --------------------------------------------------------


def test_coverage_growth_shows_uncovered_weight_honestly():
    res = rl.coverage(growth_slice())
    assert res["total_weight"] == 56.0
    assert res["covered_weight"] == 14.0  # NVIDIA 11.5 + TSMC 2.5 (only these are MENTIONS-ed)
    assert res["covered_pct"] == 25.0
    assert res["uncovered_pct"] == 75.0
    uncovered = {r["company"] for r in res["uncovered"]}
    assert "Microsoft" in uncovered and "Apple" in uncovered
    covered = {r["company"] for r in res["covered"]}
    assert covered == {"NVIDIA", "Taiwan Semiconductor Manufacturing"}


def test_coverage_staleness_flags_old_sources():
    # as_of far in the future => the 10-K (2026-02-26) is older than STALE_AFTER_DAYS
    res = rl.coverage(growth_slice(), as_of="2027-06-01")
    stale_companies = {r["company"] for r in res["stale_sources"]}
    assert "Taiwan Semiconductor Manufacturing" in stale_companies


# --- Full report + cross-fund comparison ---------------------------------------------------


def test_report_from_slice_has_all_metrics():
    rep = rl.report_from_slice(growth_slice())
    for key in ("supplier_concentration", "shared_supplier_clusters",
                "second_order_chokepoints", "risk_heatmap", "single_source_flags", "coverage"):
        assert key in rep
    assert rep["hhi"] == 412.5


def test_compare_funds_focused_more_concentrated():
    cmp = rl.comparison_from_reports(
        [rl.report_from_slice(growth_slice()), rl.report_from_slice(focused_slice())]
    )
    assert cmp["more_concentrated_fund"] == "Demo Focused Growth Fund"
    for row in cmp["funds"]:
        assert row["top_risk_category"] == "supply_chain"
    # firm-average baseline: each fund carries its delta vs the mean HHI (N-fund generalisation)
    deltas = {row["fund"]: row["hhi_delta_vs_avg"] for row in cmp["funds"]}
    assert deltas["Demo Focused Growth Fund"] > 0 > deltas["Demo Growth Fund"]
    assert cmp["avg_hhi"] == 562.625  # (412.5 + 712.75) / 2


# --- Narration (LLM only narrates; numbers are deterministic) -------------------------------


def test_narrate_uses_deterministic_numbers_in_stub():
    rep = rl.report_from_slice(growth_slice())
    out = rl.narrate(FakeProvider(fixture_dir=None), rep, role=Role.SYNTHESIZER)
    # stub returns the canned token; our narrative still carries the real numbers
    assert out["llm_note"] == "[fake:synthesizer]"
    assert "412.5" in out["narrative"]
    assert "75.0% remains uncovered" in out["narrative"]


def test_explain_cypher_present_for_every_metric():
    rep = rl.report_from_slice(growth_slice())
    assert rep["supplier_concentration"]["cypher"].strip().startswith("MATCH")
    assert rep["risk_heatmap"]["cypher"].strip().startswith("MATCH")
    assert rep["coverage"]["cypher"].strip().startswith("MATCH")
