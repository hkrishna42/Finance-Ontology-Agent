"""M6 Change Impact — OFFLINE unit tests for fact-diff + policy-driven propagation.

No Neo4j / no network: propagation runs against an in-memory DictResolver mirroring the seed.
"""

from __future__ import annotations

from _apps_seed_fixtures import seed_resolver

from api.modules import change_impact as ci
from api.providers.base import Role
from api.providers.fake import FakeProvider

V1 = ci.load_fixture("v1.json")
V2 = ci.load_fixture("v2.json")


# --- Policy loads ---------------------------------------------------------------------------


def test_policy_loads_four_rules_hop_limit_two():
    policy = ci.load_policy()
    assert policy.hop_limit == 2
    names = {r.name for r in policy.rules}
    assert names == {
        "changed_issuer_to_funds", "changed_supplier_to_funds",
        "changed_risk_to_disclosure", "changed_metric_to_reports",
    }


# --- Fact-diff ------------------------------------------------------------------------------


def test_diff_v1_v2_added_and_changed():
    diff = ci.diff_fixtures(V1, V2)
    assert diff["counts"] == {"added": 1, "removed": 0, "changed": 1}
    # ADDED: NVIDIA -> SK Hynix (new critical supplier)
    added = diff["added"][0]
    assert added["type"] == "SUPPLIES_TO" and added["subject"] == "NVIDIA"
    assert added["object"] == "SK Hynix"
    # CHANGED: NVIDIA EXPOSED_TO Customer concentration (severity language changed), with spans
    changed = diff["changed"][0]
    assert changed["type"] == "EXPOSED_TO" and changed["object"] == "Customer concentration"
    assert changed["old_span"] and changed["new_span"]
    assert changed["old_value"] != changed["new_value"]


def test_diff_unchanged_supplies_edge_not_in_diff():
    # NVIDIA -> TSMC is present unchanged in both versions
    diff = ci.diff_fixtures(V1, V2)
    all_objs = {i["object"] for i in diff["added"] + diff["removed"] + diff["changed"]}
    assert "Taiwan Semiconductor Manufacturing" not in all_objs


def test_empty_diff_same_version():
    diff = ci.diff_fixtures(V1, V1)
    assert diff["counts"] == {"added": 0, "removed": 0, "changed": 0}


# --- Propagation (acceptance) ---------------------------------------------------------------


def test_v2_impacts_both_funds_and_marks_disclosure_stale():
    diff = ci.diff_fixtures(V1, V2)
    prop = ci.propagate(diff, seed_resolver())

    funds = {f["fund"] for f in prop["affected_funds"]}
    assert funds == {"Demo Growth Fund", "Demo Focused Growth Fund"}

    # >= 1 DisclosureSection marked stale (the customer-concentration sections of both funds)
    assert prop["summary"]["n_stale_sections"] >= 1
    sections = {s["section_key"] for s in prop["stale_sections"]}
    assert "S000090001:485BPOS|conc" in sections  # Growth
    assert "S000090002:485BPOS|conc" in sections  # Focused

    # every stale section carries the target flag + a citation (span)
    for s in prop["stale_sections"]:
        assert s["flag"] == {"label": "DisclosureSection", "prop": "stale", "value": True}
        assert s["covers_risk"] == "Customer concentration"
        assert s["citations"] and s["citations"][0]["span"]

    # every affected fund is cited (which company + span drove it) and carries the HOLDS weight
    for f in prop["affected_funds"]:
        assert "NVIDIA" in f["via"]
        assert f["citations"]
        assert any(h["company"] == "NVIDIA" for h in f["holdings"])


def test_affected_funds_rule_provenance():
    diff = ci.diff_fixtures(V1, V2)
    prop = ci.propagate(diff, seed_resolver())
    for f in prop["affected_funds"]:
        # the added SK Hynix supplier edge drives fund impact via the supplier rule
        assert "changed_supplier_to_funds" in f["rules"]


def test_empty_diff_no_downstream_impact_but_diff_shown():
    diff = ci.diff_fixtures(V1, V1)
    prop = ci.propagate(diff, seed_resolver())
    brief = ci.impact_briefing(diff, prop, provider=FakeProvider(fixture_dir=None), role=Role.IMPACT)
    assert prop["summary"] == {"n_affected_funds": 0, "n_stale_sections": 0, "n_out_of_date_reports": 0}
    assert "no downstream impact" in brief["narrative"].lower()
    assert brief["diff"]["counts"] == {"added": 0, "removed": 0, "changed": 0}  # diff still shown


# --- Narration + SSE event ------------------------------------------------------------------


def test_impact_briefing_stub_narration_is_deterministic():
    diff = ci.diff_fixtures(V1, V2)
    prop = ci.propagate(diff, seed_resolver())
    brief = ci.impact_briefing(diff, prop, provider=FakeProvider(fixture_dir=None), role=Role.IMPACT)
    assert brief["llm_note"] == "[fake:impact]"
    assert "Affected funds" in brief["narrative"]
    assert "stale" in brief["narrative"].lower()


def test_make_impact_event_shape():
    diff = ci.diff_fixtures(V1, V2)
    prop = ci.propagate(diff, seed_resolver())
    ev = ci.make_impact_event("job-x", diff, prop, seq=1)
    sse = ev.to_sse()
    assert sse["event"] == "impact"
    import json
    data = json.loads(sse["data"])["data"]
    assert data["added"] == 1 and data["changed"] == 1
    assert set(data["affected_funds"]) == {
        "Demo Growth Fund", "Demo Focused Growth Fund"
    }
    assert "S000090001:485BPOS|conc" in data["stale_sections"]


def test_supplier_dependents_respects_hop_limit():
    r = seed_resolver()
    # nobody supplies NVIDIA in the seed -> only NVIDIA itself
    assert r.supplier_dependents("NVIDIA", 2) == ["NVIDIA"]
    # TSMC's dependents within 2 hops: the 4 holdings that supply-to it (+ TSMC itself)
    deps = set(r.supplier_dependents("Taiwan Semiconductor Manufacturing", 2))
    assert deps == {
        "Taiwan Semiconductor Manufacturing", "NVIDIA",
        "Advanced Micro Devices", "Apple", "Broadcom",
    }
