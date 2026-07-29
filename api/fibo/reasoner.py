"""Agent C (Validator) — materialize a FIBO ABox over the reasoned TBox and check OWL consistency.

Types each grounded entity as an instance of its FIBO class, runs the OWL-RL closure over TBox+ABox,
then flags violations — an individual entailed into two `owl:disjointWith` classes — via SPARQL over
the reasoned graph. Genuine OWL reasoning, fully offline. This drives the mockup's "reasoning valid /
violations" status.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rdflib import RDF, Namespace, URIRef

from . import tbox

_ABOX = Namespace("urn:fibo-abox:")
_OWL = Namespace("http://www.w3.org/2002/07/owl#")

# An individual entailed into two disjoint classes = an OWL inconsistency. The UNION catches the
# `owl:disjointWith` axiom in whichever direction it was declared (it is symmetric); the STR filter
# collapses the symmetric (c1,c2)/(c2,c1) rows to one.
_VIOLATION_SPARQL = """
SELECT DISTINCT ?x ?c1 ?c2 WHERE {
  ?x a ?c1, ?c2 .
  { ?c1 owl:disjointWith ?c2 } UNION { ?c2 owl:disjointWith ?c1 }
  FILTER(STR(?c1) < STR(?c2))
}"""


@dataclass
class ReasoningResult:
    valid: bool
    checked: int
    violations: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"valid": self.valid, "checked": self.checked, "violations": self.violations}


def _slug(value: Any) -> str:
    return "".join(c if c.isalnum() else "_" for c in str(value))[:96] or "x"


def validate(instances: list[dict[str, Any]]) -> ReasoningResult:
    """`instances`: `[{id, class_iri}]`. Materialize + reason over the FIBO TBox; report violations."""
    from owlrl import DeductiveClosure, OWLRL_Semantics

    g = tbox.tbox_copy()
    checked = 0
    for inst in instances:
        cls = inst.get("class_iri")
        iid = inst.get("id")
        if not cls or not iid:
            continue
        g.add((_ABOX[_slug(iid)], RDF.type, URIRef(str(cls))))
        checked += 1

    DeductiveClosure(OWLRL_Semantics).expand(g)
    rows = g.query(_VIOLATION_SPARQL, initNs={"owl": _OWL})
    violations = [
        {
            "instance": str(r[0]).replace(str(_ABOX), ""),
            "class1": tbox.curie_for(str(r[1])),
            "class2": tbox.curie_for(str(r[2])),
        }
        for r in rows
    ]
    return ReasoningResult(valid=not violations, checked=checked, violations=violations)
