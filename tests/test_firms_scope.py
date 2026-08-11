"""Offline unit tests for the firm-scoping gate (``api.firms.scope``).

Covers ``is_demo_scope`` — the single predicate every panel uses to decide "keep the demo seed +
committed demo fixtures" vs "scope to a real firm (and its legitimately-empty projection)" — and its
interplay with ``resolve_firm`` / the ``ALL_SCOPE`` sentinel. Pure: no SQLite, no Neo4j, no network.
"""

from __future__ import annotations

from api.firms import scope
from api.firms.scope import ALL_SCOPE, DEMO_FIRM_NAME, is_demo_scope, resolve_firm


def test_demo_firm_name_is_the_fictional_demo():
    assert DEMO_FIRM_NAME == "Demo Investment Management"


def test_is_demo_scope_true_for_none_and_demo_firm():
    # Unscoped (no active firm / fresh install / "All data" → None) keeps the demo experience.
    assert is_demo_scope(None) is True
    # The fictional demo firm keeps the seed + committed NVIDIA/TSMC fixtures.
    assert is_demo_scope(DEMO_FIRM_NAME) is True


def test_is_demo_scope_false_for_a_real_firm():
    assert is_demo_scope("Acme Global Real Estate Fund") is False
    assert is_demo_scope("Onboarded Advisors LLC") is False


def test_all_scope_sentinel_resolves_to_demo_scope():
    # The UI's "All data" scope sends ?firm=__all__; resolve_firm maps it to None → demo scope,
    # so the unscoped seed/whole-corpus projection is shown (never a real firm's empty state).
    assert resolve_firm(ALL_SCOPE) is None
    assert is_demo_scope(resolve_firm(ALL_SCOPE)) is True


def test_explicit_real_firm_is_not_demo_scope():
    # An explicit ?firm=<real> wins over the (absent) active firm and is emphatically NOT demo scope.
    resolved = scope.resolve_firm("Acme Global Real Estate Fund")
    assert resolved == "Acme Global Real Estate Fund"
    assert is_demo_scope(resolved) is False
