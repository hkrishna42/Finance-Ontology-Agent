"""Hero-query invariants — the golden, validated demo facts every future workstream must keep green.

These run against the *seeded* demo graph (see conftest: skips if Neo4j is down or unseeded). Each
test encodes a fact the live demo depends on; a regression in the seed, the loader, the embedder,
or (later) the query pipeline flips one of these red. Values are pinned to exact expected sets /
numbers so silent drift is caught, not just "non-empty".

Ground truth captured from a fresh `make apply-ddl && make seed` on 2026-07-27.
"""

from __future__ import annotations

GROWTH = "Demo Growth Fund"
FOCUSED = "Demo Focused Growth Fund"
TSMC = "Taiwan Semiconductor Manufacturing"
ASML = "ASML Holding"

# Canonical company names as they appear in the seed (AMD is spelled out).
TSMC_CRITICAL_SUPPLIERS = {"NVIDIA", "Advanced Micro Devices", "Apple", "Broadcom"}
ASML_REACHERS = {TSMC, "NVIDIA", "Advanced Micro Devices", "Apple", "Broadcom"}


def test_tsmc_is_shared_critical_supplier_of_exactly_4_growth_holdings(graph):
    """Growth: TSMC is a *critical* supplier shared by exactly 4 holdings (NVDA, AMD, AAPL, AVGO)."""
    rows = graph.run(
        """
        MATCH (gf:Fund {name:$growth})-[:HOLDS]->(sup:Company)
              -[s:SUPPLIES_TO]->(t:Company {name:$tsmc})
        WHERE s.criticality = 'critical'
        RETURN sup.name AS supplier
        """,
        growth=GROWTH,
        tsmc=TSMC,
    )
    suppliers = {r["supplier"] for r in rows}
    # Exactly 4, all distinct, exactly the validated set — and each really is a Growth holding.
    assert suppliers == TSMC_CRITICAL_SUPPLIERS, suppliers
    assert len(rows) == 4, f"expected 4 distinct critical suppliers, got {len(rows)}"


def test_no_extra_critical_suppliers_into_tsmc(graph):
    """Guard the other direction: nothing outside the 4 critically supplies TSMC in the seed."""
    all_suppliers = {
        r["s"]
        for r in graph.run(
            "MATCH (a:Company)-[:SUPPLIES_TO {criticality:'critical'}]->(:Company {name:$t}) "
            "RETURN a.name AS s",
            t=TSMC,
        )
    }
    assert all_suppliers == TSMC_CRITICAL_SUPPLIERS, all_suppliers


def test_asml_reachable_within_2_hops_from_5_growth_holdings(graph):
    """ASML is a second-order chokepoint: reachable in <=2 SUPPLIES_TO hops from 5 Growth holdings."""
    rows = graph.run(
        """
        MATCH (gf:Fund {name:$growth})-[:HOLDS]->(h:Company)
        MATCH (asml:Company {name:$asml})
        WHERE (h)-[:SUPPLIES_TO*1..2]->(asml)
        RETURN DISTINCT h.name AS holding
        """,
        growth=GROWTH,
        asml=ASML,
    )
    reachers = {r["holding"] for r in rows}
    assert reachers == ASML_REACHERS, reachers
    assert len(reachers) == 5


def test_asml_is_not_itself_held(graph):
    """ASML must NOT be a holding of either fund (it surfaces only via the supply chain)."""
    held_by = graph.run(
        "MATCH (f:Fund)-[:HOLDS]->(:Company {name:$asml}) RETURN f.name AS fund", asml=ASML
    )
    assert held_by == [], f"ASML unexpectedly held: {held_by}"


def test_focused_hhi_proxy_exceeds_growth(graph):
    """Concentration: Focused Growth's HHI-proxy (sum weight_pct^2) strictly exceeds Growth's."""
    rows = graph.run(
        "MATCH (f:Fund)-[r:HOLDS]->(:Company) "
        "RETURN f.name AS fund, sum(r.weight_pct * r.weight_pct) AS hhi"
    )
    hhi = {r["fund"]: r["hhi"] for r in rows}
    assert hhi[FOCUSED] > hhi[GROWTH], hhi
    # Pinned regression anchors (see ground-truth capture).
    assert hhi[FOCUSED] == 712.75, hhi[FOCUSED]
    assert hhi[GROWTH] == 412.5, hhi[GROWTH]


def test_growth_coverage_gap_is_supply_chain_and_geopolitical(graph):
    """The Reg-Assistant gap: exposed risk categories with no matching Growth DisclosureSection.

    Gap = categories the fund's holdings are EXPOSED_TO, minus categories COVERED by a
    DisclosureSection whose form_ref starts with the fund's series_id. Validated = {supply_chain,
    geopolitical}.
    """
    gap = graph.value(
        """
        MATCH (gf:Fund {name:$growth})-[:HOLDS]->(:Company)-[:EXPOSED_TO]->(rf:RiskFactor)
        WITH gf, collect(DISTINCT rf.category) AS exposed
        OPTIONAL MATCH (ds:DisclosureSection)-[:COVERS]->(crf:RiskFactor)
          WHERE ds.form_ref STARTS WITH gf.series_id
        WITH exposed, collect(DISTINCT crf.category) AS covered
        RETURN [c IN exposed WHERE NOT c IN covered] AS gap
        """,
        growth=GROWTH,
    )
    assert set(gap) == {"supply_chain", "geopolitical"}, gap


def test_growth_series_id_prefixes_its_disclosure_forms(graph):
    """The gap query hinges on form_ref STARTS WITH series_id — assert that link actually holds."""
    series_id = graph.value(
        "MATCH (f:Fund {name:$growth}) RETURN f.series_id AS s", growth=GROWTH
    )
    assert series_id == "S000090001", series_id
    covered_forms = {
        r["f"]
        for r in graph.run(
            "MATCH (ds:DisclosureSection)-[:COVERS]->(:RiskFactor) "
            "WHERE ds.form_ref STARTS WITH $sid RETURN DISTINCT ds.form_ref AS f",
            sid=series_id,
        )
    }
    assert covered_forms == {"S000090001:485BPOS"}, covered_forms


def test_board_interlock_ada_directs_nvidia_and_microsoft(graph):
    """Board interlock: Ada Interlock is DIRECTOR_OF both NVIDIA and Microsoft."""
    companies = {
        r["c"]
        for r in graph.run(
            "MATCH (:Person {name:'Ada Interlock'})-[:DIRECTOR_OF]->(c:Company) "
            "RETURN c.name AS c"
        )
    }
    assert companies == {"NVIDIA", "Microsoft"}, companies


def test_exactly_four_chunks_have_embeddings(graph):
    """Exactly 4 :Chunk carry an embedding vector, each of the ontology dimension (384)."""
    total, with_emb = graph.run(
        "MATCH (c:Chunk) RETURN count(c) AS total, count(c.embedding) AS emb"
    )[0].values()
    assert total == 4, total
    assert with_emb == 4, with_emb
    dims = {
        r["d"]
        for r in graph.run(
            "MATCH (c:Chunk) WHERE c.embedding IS NOT NULL RETURN size(c.embedding) AS d"
        )
    }
    assert dims == {384}, dims


def test_exactly_one_internal_chunk(graph):
    """Exactly 1 :Chunk is sensitivity='internal' (the synthetic analyst note behind the wall)."""
    n = graph.value("MATCH (c:Chunk) WHERE c.sensitivity = 'internal' RETURN count(c) AS n")
    assert n == 1, n
    internal_id = graph.value(
        "MATCH (c:Chunk) WHERE c.sensitivity = 'internal' RETURN c.chunk_id AS id"
    )
    assert internal_id == "internal_note_c1", internal_id
