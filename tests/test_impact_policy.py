"""Impact-policy tests: the M6 policy YAML loads and declares the 4 expected rules."""

from __future__ import annotations

from pathlib import Path

import yaml

POLICY_PATH = Path(__file__).resolve().parents[1] / "api" / "ontology" / "impact_policy.yaml"

EXPECTED_RULES = {
    "changed_issuer_to_funds",
    "changed_supplier_to_funds",
    "changed_risk_to_disclosure",
    "changed_metric_to_reports",
}


def test_impact_policy_loads():
    data = yaml.safe_load(POLICY_PATH.read_text())
    assert isinstance(data, dict)
    assert "rules" in data
    assert isinstance(data["rules"], list)


def test_impact_policy_has_expected_rule_names():
    data = yaml.safe_load(POLICY_PATH.read_text())
    names = {rule["name"] for rule in data["rules"]}
    assert names == EXPECTED_RULES


def test_impact_policy_hop_limit_present():
    data = yaml.safe_load(POLICY_PATH.read_text())
    assert data.get("hop_limit") == 2
