"""#4 + #2 — GET /impact ImpactBriefing[] projection (offline) + graceful LLM degradation.

Validates the ImpactBriefing shape against web/src/types.ts and the v1→v2 impact numbers, using
the in-memory seed resolver (no Neo4j / no network).
"""

from __future__ import annotations

from _apps_neo4j import neo4j_store_or_skip
from _apps_seed_fixtures import seed_resolver

from api.modules import change_impact as ci

V1 = ci.load_fixture("v1.json")
V2 = ci.load_fixture("v2.json")

GROWTH = "Demo Growth Fund"
FOCUSED = "Demo Focused Growth Fund"


def _keys(d: dict) -> set[str]:
    return set(d.keys())


def test_get_impact_endpoint_live():
    """Seeded HTTP path: GET /impact for the DEMO firm returns 200 ImpactBriefing[] (the fixture)."""
    neo4j_store_or_skip().close()
    from fastapi.testclient import TestClient

    from api.main import app

    # Pin the demo firm: only it shows the illustrative fixture briefing (a non-demo active firm → []).
    r = TestClient(app).get("/impact", params={"firm": "Demo Investment Management"})
    assert r.status_code == 200
    d = r.json()
    assert isinstance(d, list) and d
    assert {"id", "trigger_doc_id", "affected_funds", "stale_sections"} <= set(d[0])
    assert {a["fund"] for a in d[0]["affected_funds"]} == {GROWTH, FOCUSED}


def test_get_impact_non_demo_firm_is_empty():
    """A real (non-demo) firm's change feed is empty — NEVER the demo's NVIDIA fixture briefing.

    Fully offline: a truthy `?firm=` resolves without the registry, and the non-demo branch returns
    `[]` before any graph read — the leak the demo fixture used to cause under every active firm.
    """
    from fastapi.testclient import TestClient

    from api.main import app

    r = TestClient(app).get("/impact", params={"firm": "Some Onboarded Advisers LLC"})
    assert r.status_code == 200
    assert r.json() == []


def test_neo4j_resolver_funds_holding_is_firm_scoped():
    """With a firm set, `funds_holding` scopes to that firm's funds (MANAGED_BY); unscoped otherwise."""
    class _Rec:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def run(self, query: str, **params):
            self.calls.append((query, params))
            return []

    rec = _Rec()
    ci.Neo4jResolver(rec, firm="Acme Advisers").funds_holding(["NVIDIA"])
    q, p = rec.calls[0]
    assert "MANAGED_BY" in q and p["firm"] == "Acme Advisers" and p["names"] == ["NVIDIA"]

    rec2 = _Rec()
    ci.Neo4jResolver(rec2).funds_holding(["NVIDIA"])  # no firm → original, unscoped query
    q2, p2 = rec2.calls[0]
    assert "MANAGED_BY" not in q2 and "firm" not in p2


def test_impact_collection_is_array_of_briefings():
    out = ci.impact_briefings(V1, V2, seed_resolver())
    assert isinstance(out, list) and len(out) == 1
    b = out[0]
    assert _keys(b) == {
        "id", "trigger_doc_id", "trigger_title", "created_at", "rule", "summary",
        "added", "removed", "changed", "affected_funds", "stale_sections",
    }


def test_briefing_trigger_and_created_at():
    b = ci.impact_briefings(V1, V2, seed_resolver())[0]
    assert b["trigger_doc_id"] == "nvda_10k_v2"
    assert b["trigger_title"] == "NVIDIA FY2025 10-K/A (amended)"
    assert b["created_at"] == "2026-07-25T00:00:00Z"
    assert "changed_supplier_to_funds" in b["rule"]
    assert b["summary"]  # deterministic templated summary (no LLM)


def test_fact_triples_shape():
    b = ci.impact_briefings(V1, V2, seed_resolver())[0]
    for group in (b["added"], b["changed"]):
        for t in group:
            assert _keys(t) <= {"subject", "predicate", "object", "detail"}
            assert {"subject", "predicate", "object"} <= _keys(t)
    added = b["added"][0]
    assert (added["subject"], added["predicate"], added["object"]) == ("NVIDIA", "SUPPLIES_TO", "SK Hynix")
    changed = b["changed"][0]
    assert (changed["subject"], changed["predicate"], changed["object"]) == \
        ("NVIDIA", "EXPOSED_TO", "Customer concentration")


def test_affected_funds_shape_both_funds_hops_one():
    b = ci.impact_briefings(V1, V2, seed_resolver())[0]
    funds = {a["fund"]: a for a in b["affected_funds"]}
    assert set(funds) == {GROWTH, FOCUSED}
    for a in b["affected_funds"]:
        assert _keys(a) == {"fund", "reason", "hops"}
        assert a["hops"] == 1
        assert "NVIDIA" in a["reason"] and "HOLDS" in a["reason"]


def test_stale_sections_shape_with_item_and_title():
    b = ci.impact_briefings(V1, V2, seed_resolver())[0]
    secs = {s["form_ref"]: s for s in b["stale_sections"]}
    assert "S000090001:485BPOS" in secs and "S000090002:485BPOS" in secs
    for s in b["stale_sections"]:
        assert _keys(s) == {"form_ref", "item", "title", "reason"}
        assert s["item"] == "principal_risks/concentration"
        assert s["title"] and "Customer concentration" in s["reason"]


def test_empty_diff_yields_briefing_with_no_impact():
    b = ci.impact_briefings(V1, V1, seed_resolver())[0]
    assert b["added"] == [] and b["changed"] == [] and b["removed"] == []
    assert b["affected_funds"] == [] and b["stale_sections"] == []
    assert "no downstream impact" in b["summary"].lower()


# --- #2 graceful degradation --------------------------------------------------------------


class _RaisingProvider:
    def complete(self, **kwargs):
        raise RuntimeError("Your credit balance is too low")


def test_impact_briefing_degrades_gracefully_on_llm_failure():
    diff = ci.diff_fixtures(V1, V2)
    prop = ci.propagate(diff, seed_resolver())
    brief = ci.impact_briefing(diff, prop, provider=_RaisingProvider(), role=ci.Role.IMPACT)
    assert brief["narration_unavailable"] and "credit balance" in brief["narration_unavailable"]
    assert brief["llm_note"] is None
    assert "Affected funds" in brief["narrative"]  # deterministic narrative preserved
