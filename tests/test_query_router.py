"""M3 — Router: intent classification, analytics-tool selection, entity/fund detection, entitlements.

Fully offline: an explicit Vocab + FakeProvider (stub Role.ROUTER).
"""

from __future__ import annotations

import pytest

from api.providers.fake import FakeProvider
from api.query.router import Router, Vocab, entitlement_scope

GROWTH = "Demo Growth Fund"
FOCUSED = "Demo Focused Growth Fund"

VOCAB = Vocab(
    companies={
        "nvidia": "NVIDIA",
        "nvda": "NVIDIA",
        "advanced micro devices": "Advanced Micro Devices",
        "amd": "Advanced Micro Devices",
        "asml holding": "ASML Holding",
        "taiwan semiconductor manufacturing": "Taiwan Semiconductor Manufacturing",
        "microsoft": "Microsoft",
    },
    funds=[GROWTH, FOCUSED],
)


@pytest.fixture()
def router():
    return Router(provider=FakeProvider())


def test_entitlement_scope():
    assert entitlement_scope(None) == ["public"]
    assert entitlement_scope([]) == ["public"]
    assert entitlement_scope(["internal"]) == ["internal", "public"]
    assert entitlement_scope(["public", "internal"]) == ["internal", "public"]


def test_shared_supplier_routes_to_analytics(router):
    plan = router.route(
        "Which suppliers are shared across the Demo Growth Fund holdings?", vocab=VOCAB
    )
    assert plan.tool == "shared_suppliers"
    assert plan.intent == "relational"
    assert plan.fund == GROWTH
    assert plan.entitlements == ["public"]


def test_focused_fund_is_distinguished_from_growth(router):
    plan = router.route("How concentrated is the Demo Focused Growth Fund?", vocab=VOCAB)
    assert plan.fund == FOCUSED
    assert plan.tool == "concentration_summary"
    assert plan.intent == "aggregate"


def test_chokepoint_routes(router):
    plan = router.route("What second-order chokepoints does the Demo Growth Fund face?", vocab=VOCAB)
    assert plan.tool == "second_order_chokepoints"
    assert plan.fund == GROWTH


def test_board_routes_and_detects_company(router):
    plan = router.route("Show board interlocks and alumni links for NVIDIA", vocab=VOCAB)
    assert plan.tool == "board_and_alumni_links"
    assert plan.tool_args["company"] == "NVIDIA"


def test_exposure_path_preserves_direction(router):
    plan = router.route(
        "What is the exposure path from NVIDIA to ASML Holding?", vocab=VOCAB
    )
    assert plan.tool == "exposure_path"
    assert plan.tool_args == {"a": "NVIDIA", "b": "ASML Holding"}


def test_lookup_intent(router):
    plan = router.route("What is NVIDIA's ticker?", vocab=VOCAB)
    assert plan.intent == "lookup"
    assert plan.tool is None


def test_missing_fund_keeps_fund_optional_tool(router):
    # shared_suppliers is fund-optional (NULL → across all funds) → the hero question still routes,
    # even without a fund named (this is what makes it answer live).
    plan = router.route("Which holdings share a critical supplier?", vocab=VOCAB)
    assert plan.tool == "shared_suppliers"
    assert plan.tool_args == {"fund": None}


def test_coverage_gap_routes(router):
    plan = router.route(
        "Is the funds' foundry risk disclosed in their prospectus?", vocab=VOCAB
    )
    assert plan.tool == "coverage_gap"


def test_pre_router_skips_llm_for_tool_questions(router):
    # Deterministic pre-router: a tool-matched question makes NO LLM call (llm_intent stays None),
    # which is what keeps tool answers alive under zero credits.
    tool_plan = router.route("Which holdings share a critical supplier?", vocab=VOCAB)
    assert tool_plan.tool == "shared_suppliers"
    assert tool_plan.llm_intent is None

    # An ambiguous (no-tool) question does consult the Role.ROUTER LLM (FakeProvider → 'lookup').
    ambiguous = router.route("Tell me something interesting", vocab=VOCAB)
    assert ambiguous.tool is None
    assert ambiguous.llm_intent == "lookup"
