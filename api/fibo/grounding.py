"""Deterministic FIBO grounding (Agent B: Ontologist) — map an ontology entity to a FIBO OWL class.

No LLM: a static label -> default-FIBO-class map + small refinement rules on the entity's category /
attributes (a debt/bond-issuer role refines Company -> CorporateDebtIssuer; real-property type
variants map to the one canonical RealProperty class). Runs in stub mode. Supersedes the placeholder
`api/ontology/fibo_map.py`. The class IRIs come from `api/fibo/tbox.PREFIXES` (real FIBO/Commons IRIs).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import tbox

# Our ontology label -> default FIBO class curie (the per-label grounding; per-instance refinement
# below can pick a more specific class). Also mirrored onto EntitySpec.fibo_class in the schema.
LABEL_FIBO_CURIE: dict[str, str] = {
    "Company": "cmns-org:LegalEntity",
    "Fund": "fibo-sec-sec-pls:CollectiveInvestmentVehicle",
    "Portfolio": "fibo-sec-sec-pls:CollectiveInvestmentVehicle",
    "RealProperty": "fibo-fnd-plc-rp:RealProperty",
    "Loan": "fibo-loan-ln-ln:Loan",
    "Valuation": "fibo-fnd-plc-rp:RealPropertyAppraisal",
    "Lease": "fibo-fnd-agr-ctr:Contract",
}

# ontology_classification_mapping: variant property-type strings -> the canonical RealProperty class.
# Single source of truth reused by MDM survivorship (the "Property Type" rule) so the ontology and the
# golden record agree.
PROPERTY_TYPE_TO_FIBO: dict[str, str] = {
    "office": "fibo-fnd-plc-rp:RealProperty",
    "commercial office": "fibo-fnd-plc-rp:RealProperty",
    "class a office": "fibo-fnd-plc-rp:RealProperty",
    "office collateral": "fibo-fnd-plc-rp:RealProperty",
    "logistics": "fibo-fnd-plc-rp:RealProperty",
    "logistics center": "fibo-fnd-plc-rp:RealProperty",
    "industrial": "fibo-fnd-plc-rp:RealProperty",
}

_ISSUER_HINTS = ("bond issuer", "debt issuer", "corporate debt", "note issuer", "debt capital")


@dataclass(frozen=True)
class FiboGrounding:
    class_iri: str  # "" when ungrounded
    curie: str  # "" when ungrounded
    label_default_curie: str
    refined: bool  # class was refined below the label default (e.g. LegalEntity -> CorporateDebtIssuer)
    grounded: bool


def ground(
    label: str, *, category: str | None = None, attributes: dict[str, Any] | None = None
) -> FiboGrounding:
    """Ground one entity to a FIBO class. Ungrounded labels return `grounded=False` (a valid state)."""
    attrs = attributes or {}
    default = LABEL_FIBO_CURIE.get(label)
    curie = default
    refined = False

    if label == "Company":
        hint = " ".join(
            str(v).lower()
            for v in (category, attrs.get("role"), attrs.get("issuer_type"), attrs.get("kind"))
            if v
        )
        if any(h in hint for h in _ISSUER_HINTS):
            curie, refined = "fibo-sec-dbt-dbt:CorporateDebtIssuer", True

    if label == "RealProperty":
        ptype = str(attrs.get("property_type") or category or "").strip().lower()
        curie = PROPERTY_TYPE_TO_FIBO.get(ptype, curie)

    if not curie:
        return FiboGrounding("", "", "", False, False)
    return FiboGrounding(tbox.iri_for(curie), curie, default or curie, refined, True)
