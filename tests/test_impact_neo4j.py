"""M6 Change Impact — integration tests against a live seeded Neo4j.

Skips when no seeded graph is reachable. Validates the propagation (funds + stale sections),
the Neo4jResolver traversals, SUPERSEDES versioning, and flag persistence (with cleanup).
"""

from __future__ import annotations

from _apps_neo4j import neo4j_store_or_skip

from api.config import Settings
from api.modules import change_impact as ci
from api.providers.base import Role
from api.providers.factory import get_llm_provider

V1 = ci.load_fixture("v1.json")
V2 = ci.load_fixture("v2.json")


def test_neo4j_resolver_supplier_dependents():
    store = neo4j_store_or_skip()
    try:
        r = ci.Neo4jResolver(store)
        assert r.supplier_dependents("NVIDIA", 2) == ["NVIDIA"]  # nobody supplies NVIDIA
        deps = set(r.supplier_dependents("Taiwan Semiconductor Manufacturing", 2))
        assert {"NVIDIA", "Advanced Micro Devices", "Apple", "Broadcom",
                "Taiwan Semiconductor Manufacturing"} == deps
    finally:
        store.close()


def test_v2_propagation_live_both_funds_and_stale_section():
    store = neo4j_store_or_skip()
    try:
        resolver = ci.Neo4jResolver(store)
        diff = ci.diff_fixtures(V1, V2)
        prop = ci.propagate(diff, resolver)
        funds = {f["fund"] for f in prop["affected_funds"]}
        assert funds == {"Demo Growth Fund", "Demo Focused Growth Fund"}
        assert prop["summary"]["n_stale_sections"] >= 1
        sections = {s["section_key"] for s in prop["stale_sections"]}
        assert "S000090001:485BPOS|conc" in sections
        # narrated briefing (stub provider) with numbers preserved
        brief = ci.impact_briefing(diff, prop, provider=get_llm_provider(Settings()), role=Role.IMPACT)
        assert "Affected funds" in brief["narrative"]
    finally:
        store.close()


def test_empty_diff_live_no_impact():
    store = neo4j_store_or_skip()
    try:
        resolver = ci.Neo4jResolver(store)
        diff = ci.diff_fixtures(V1, V1)
        prop = ci.propagate(diff, resolver)
        assert prop["summary"]["n_affected_funds"] == 0
        assert prop["summary"]["n_stale_sections"] == 0
    finally:
        store.close()


def test_supersedes_versioning_and_flag_persistence_with_cleanup():
    store = neo4j_store_or_skip()
    try:
        # SUPERSEDES: v2 document supersedes v1 document
        ci.record_supersedes(store, V2["document"], V2["supersedes"])
        rows = store.run(
            "MATCH (nd:Document {doc_id:$new})-[:SUPERSEDES]->(od:Document {doc_id:$old}) "
            "RETURN nd.doc_id AS nd, od.doc_id AS od",
            new=V2["document"]["doc_id"], old=V2["supersedes"],
        )
        assert rows and rows[0]["od"] == "nvda_10k"

        # apply_flags persists stale:true onto the disclosure sections
        diff = ci.diff_fixtures(V1, V2)
        prop = ci.propagate(diff, ci.Neo4jResolver(store))
        applied = ci.apply_flags(store, prop)
        assert applied["sections_flagged"] >= 1
        stale = store.run(
            "MATCH (ds:DisclosureSection {key:'S000090001:485BPOS|conc'}) RETURN ds.stale AS stale"
        )
        assert stale[0]["stale"] is True
    finally:
        # cleanup: remove the flag + the synthetic v2 document so the shared graph is pristine
        store.run("MATCH (ds:DisclosureSection) REMOVE ds.stale")
        store.run("MATCH (nd:Document {doc_id:$new}) DETACH DELETE nd", new=V2["document"]["doc_id"])
        store.close()
