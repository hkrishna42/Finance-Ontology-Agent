"""#3 + #2 — GET /risk RiskData projection (offline) + graceful LLM degradation.

Validates the RiskData shape against web/src/types.ts field-by-field (the UI does a structural
check) and asserts the live numbers. No Neo4j / no network (uses the seed-mirrored runner).
"""

from __future__ import annotations

from _apps_neo4j import neo4j_store_or_skip
from _apps_seed_fixtures import FOCUSED_FUND, GROWTH_FUND, seed_runner

from api.modules import risk_lens as rl
from api.providers.base import Role


def _keys(d: dict) -> set[str]:
    return set(d.keys())


def test_get_risk_endpoint_live():
    """Seeded HTTP path: GET /risk on the wired app returns 200 RiskData."""
    neo4j_store_or_skip().close()
    from fastapi.testclient import TestClient

    from api.main import app

    r = TestClient(app).get("/risk")
    assert r.status_code == 200
    d = r.json()
    assert _keys(d) == {"concentration", "hhi", "heatmap", "single_source"}
    assert len(d["hhi"]) == 2 and d["concentration"]
    assert d["heatmap"]["companies"] and d["single_source"]


def test_risk_collection_top_level_shape():
    data = rl.risk_collection(seed_runner(), funds=(GROWTH_FUND, FOCUSED_FUND))
    assert _keys(data) == {"concentration", "hhi", "heatmap", "single_source"}


def test_concentration_rows_match_ConcentrationRow():
    data = rl.risk_collection(seed_runner(), funds=(GROWTH_FUND, FOCUSED_FUND))
    rows = data["concentration"]
    # 9 Growth + 8 Focused holdings, sorted by weight desc (Focused NVIDIA 16.0 first)
    assert len(rows) == 17
    assert rows[0] == {"fund": FOCUSED_FUND, "issuer": "NVIDIA", "ticker": "NVDA",
                       "weight_pct": 16.0, "value_usd": 480000000}
    for r in rows:
        assert _keys(r) == {"fund", "issuer", "ticker", "weight_pct", "value_usd"}
        assert isinstance(r["value_usd"], int)
    weights = [r["weight_pct"] for r in rows]
    assert weights == sorted(weights, reverse=True)


def test_hhi_rows_match_HHIRow_with_explain():
    data = rl.risk_collection(seed_runner(), funds=(GROWTH_FUND, FOCUSED_FUND))
    by_fund = {r["fund"]: r for r in data["hhi"]}
    for r in data["hhi"]:
        assert _keys(r) == {"fund", "hhi", "top_weight_pct", "interpretation", "explain"}
        assert _keys(r["explain"]) == {"cypher", "edges", "note"}
        assert r["explain"]["edges"] and _keys(r["explain"]["edges"][0]) == {"from", "rel", "to", "qualifiers"}
    g, f = by_fund[GROWTH_FUND], by_fund[FOCUSED_FUND]
    assert g["hhi"] == 412 and g["top_weight_pct"] == 11.5   # rounded from 412.5
    assert f["hhi"] == 713 and f["top_weight_pct"] == 16.0   # rounded from 712.75
    assert "concentrated" in f["interpretation"].lower()


def test_heatmap_matches_HeatmapData():
    data = rl.risk_collection(seed_runner(), funds=(GROWTH_FUND, FOCUSED_FUND))
    hm = data["heatmap"]
    assert _keys(hm) == {"companies", "categories", "cells"}
    assert "supply_chain" in hm["categories"]
    assert set(hm["companies"]) == {
        "NVIDIA", "Advanced Micro Devices", "Apple", "Broadcom",
        "Taiwan Semiconductor Manufacturing", "Microsoft",
    }
    for cell in hm["cells"]:
        assert _keys(cell) <= {"company", "category", "severity", "severity_language"}
        assert {"company", "category", "severity"} <= _keys(cell)
        assert 0 <= cell["severity"] <= 3
    nvda_supply = [c for c in hm["cells"] if c["company"] == "NVIDIA" and c["category"] == "supply_chain"][0]
    assert nvda_supply["severity"] == 3  # "substantial dependence..."
    amd_supply = [c for c in hm["cells"] if c["company"] == "Advanced Micro Devices" and c["category"] == "supply_chain"][0]
    assert amd_supply["severity"] == 2  # "we rely on TSMC"


def test_single_source_matches_SingleSourceFlag():
    data = rl.risk_collection(seed_runner(), funds=(GROWTH_FUND, FOCUSED_FUND))
    flags = {f["supplier"]: f for f in data["single_source"]}
    for f in data["single_source"]:
        assert _keys(f) == {"supplier", "criticality", "dependents", "exposed_funds",
                            "aggregate_weight_pct", "explain"}
    tsmc = flags["Taiwan Semiconductor Manufacturing"]
    assert tsmc["criticality"] == "critical"
    assert set(tsmc["dependents"]) == {"NVIDIA", "Advanced Micro Devices", "Apple", "Broadcom"}
    assert set(tsmc["exposed_funds"]) == {GROWTH_FUND, FOCUSED_FUND}
    assert tsmc["aggregate_weight_pct"] == 37.5  # worst-case single fund (Focused)
    assert flags["ASML Holding"]["dependents"] == ["Taiwan Semiconductor Manufacturing"]
    assert flags["ASML Holding"]["exposed_funds"] == [GROWTH_FUND]


# --- N-fund generalization: default None -> all graph funds, and N in {0, 1, 3} ---------------


class _NFundRunner:
    """Offline `.run()` over synthetic funds: maps ALL_FUNDS_CYPHER + the per-fund slice queries.

    Holdings only (no supplies/exposures/provenance) — enough to drive concentration + HHI and to
    prove risk_collection projects any N funds without positional `rows[0]`/`rows[1]` indexing.
    """

    def __init__(self, holdings_by_fund: dict[str, list[tuple[str, float, int]]]):
        self._h = holdings_by_fund

    def run(self, query: str, **params):
        if query == rl.ALL_FUNDS_CYPHER:
            return [{"name": name} for name in sorted(self._h)]  # ORDER BY name
        if query == rl.CYPHER["holdings"]:
            return [
                {"company": c, "ticker": None, "cik": None, "weight_pct": w,
                 "value_usd": v, "as_of": "2026-05-31", "source": "test"}
                for (c, w, v) in self._h.get(params.get("fund"), [])
            ]
        if query in (rl.CYPHER["supplies"], rl.CYPHER["exposures"], rl.CYPHER["provenance"]):
            return []
        raise KeyError(f"_NFundRunner: unmapped query:\n{query}")


def _seed_runner_all_funds():
    """seed_runner extended to answer ALL_FUNDS_CYPHER with the two demo funds (ORDER BY name)."""
    base = seed_runner()

    class _R:
        def run(self, query: str, **params):
            if query == rl.ALL_FUNDS_CYPHER:
                return [{"name": FOCUSED_FUND}, {"name": GROWTH_FUND}]
            return base.run(query, **params)

    return _R()


def test_risk_collection_default_none_resolves_all_graph_funds():
    """Backward-compat: risk_collection(store) with no funds projects every Fund node."""
    default = rl.risk_collection(_seed_runner_all_funds())
    explicit = rl.risk_collection(seed_runner(), funds=(GROWTH_FUND, FOCUSED_FUND))
    assert _keys(default) == {"concentration", "hhi", "heatmap", "single_source"}
    assert {r["fund"] for r in default["hhi"]} == {GROWTH_FUND, FOCUSED_FUND}
    # identical projection regardless of how the two funds were resolved (order-independent compare)
    assert {r["fund"]: r for r in default["hhi"]} == {r["fund"]: r for r in explicit["hhi"]}
    assert default["concentration"] == explicit["concentration"]
    assert default["heatmap"] == explicit["heatmap"]


def test_risk_collection_one_fund_no_indexerror():
    """N == 1: single fund, no firm-average clause, no IndexError."""
    data = rl.risk_collection(_NFundRunner({"Solo Fund": [("Acme", 40.0, 400)]}))
    assert len(data["hhi"]) == 1
    row = data["hhi"][0]
    assert row["fund"] == "Solo Fund"
    assert row["hhi"] == round(40.0 ** 2)  # 1600
    # a single fund has no peer, so no cross-fund "firm average" comparison is appended
    assert "firm average" not in row["interpretation"]
    assert data["heatmap"] == {"companies": [], "categories": [], "cells": []}
    assert data["single_source"] == []


def test_risk_collection_three_funds_no_indexerror():
    """N == 3: three funds project three HHI rows, each compared vs the firm average."""
    data = rl.risk_collection(_NFundRunner({
        "Fund A": [("Acme", 30.0, 300), ("Beta", 10.0, 100)],
        "Fund B": [("Gamma", 20.0, 200), ("Delta", 20.0, 200)],
        "Fund C": [("Eps", 50.0, 500)],
    }))
    assert len(data["hhi"]) == 3
    assert {r["fund"] for r in data["hhi"]} == {"Fund A", "Fund B", "Fund C"}
    assert len(data["concentration"]) == 5  # 2 + 2 + 1 holdings
    # the most concentrated fund (a single 50% position) reads above the firm average
    fund_c = {r["fund"]: r for r in data["hhi"]}["Fund C"]
    assert fund_c["hhi"] == round(50.0 ** 2)  # 2500
    assert "more concentrated than the firm average" in fund_c["interpretation"]


def test_risk_collection_zero_funds_empty_projection():
    """N == 0: an empty graph yields an all-empty projection (200-able), not an IndexError."""
    data = rl.risk_collection(_NFundRunner({}))
    assert data == {"concentration": [], "hhi": [],
                    "heatmap": {"companies": [], "categories": [], "cells": []},
                    "single_source": []}


# --- #2 graceful degradation --------------------------------------------------------------


class _RaisingProvider:
    def complete(self, **kwargs):
        raise RuntimeError("Your credit balance is too low")


def test_narrate_degrades_gracefully_on_llm_failure():
    rep = rl.report_from_slice(rl.fetch_slice(seed_runner(), GROWTH_FUND))
    out = rl.narrate(_RaisingProvider(), rep, role=Role.SYNTHESIZER)
    # numbers still returned; narration omitted with an honest reason (never a raise)
    assert out["narration_unavailable"] and "credit balance" in out["narration_unavailable"]
    assert out["llm_note"] is None
    assert "412.5" in out["narrative"]  # deterministic narrative preserved


def test_narrate_stub_has_no_unavailable_flag():
    from api.providers.fake import FakeProvider

    rep = rl.report_from_slice(rl.fetch_slice(seed_runner(), GROWTH_FUND))
    out = rl.narrate(FakeProvider(fixture_dir=None), rep)
    assert out["narration_unavailable"] is None
