"""Load the vendored FIBO OWL slice, run OWL-RL reasoning, and expose class lookups.

Genuine OWL, offline: real FIBO / OMG-Commons class IRIs + `rdfs:subClassOf` / `owl:disjointWith`
axioms (`vendor/firm_fibo_slice.ttl`) parsed with `rdflib` and materialized with `owlrl`'s OWL-RL
deductive closure — subclass transitivity, class membership, disjointness. No network, no Java.

The slice is import-free so it reasons in milliseconds; the reasoned TBox is cached. A fuller vendored
FIBO (module `owl:imports` closure + a committed reasoned snapshot) can drop in behind this same API.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

from rdflib import OWL, RDF, RDFS, Graph, URIRef

_SLICE = Path(__file__).parent / "vendor" / "firm_fibo_slice.ttl"

# Display prefixes for the FIBO / OMG-Commons IRIs we ground to (curie <-> IRI). Longest-base-first
# so `curie_for` picks the most specific namespace.
PREFIXES: dict[str, str] = {
    "cmns-org": "https://www.omg.org/spec/Commons/Organizations/",
    "fibo-fnd-oac-own": "https://spec.edmcouncil.org/fibo/ontology/FND/OwnershipAndControl/Ownership/",
    "fibo-fnd-plc-rp": "https://spec.edmcouncil.org/fibo/ontology/FND/Places/RealProperty/",
    "fibo-fnd-agr-ctr": "https://spec.edmcouncil.org/fibo/ontology/FND/Agreements/Contracts/",
    "fibo-sec-sec-pls": "https://spec.edmcouncil.org/fibo/ontology/SEC/Securities/Pools/",
    "fibo-sec-dbt-dbt": "https://spec.edmcouncil.org/fibo/ontology/SEC/Debt/DebtInstruments/",
    "fibo-fbc-fi-fi": "https://spec.edmcouncil.org/fibo/ontology/FBC/FinancialInstruments/FinancialInstruments/",
    "fibo-loan-ln-ln": "https://spec.edmcouncil.org/fibo/ontology/LOAN/LoansGeneral/Loans/",
}
_IRI_TO_PREFIX = sorted(((iri, pfx) for pfx, iri in PREFIXES.items()), key=lambda kv: -len(kv[0]))
# Namespaces whose classes are "ours" (surfaced in /fibo/classes); filters out owl:Thing etc.
_OUR_BASES = tuple(PREFIXES.values())


def iri_for(curie: str) -> str:
    """`fibo-loan-ln-ln:Loan` -> the full IRI. Returns the input unchanged if the prefix is unknown."""
    pfx, sep, local = curie.partition(":")
    base = PREFIXES.get(pfx)
    return f"{base}{local}" if (base and sep) else curie


def curie_for(iri: str) -> str:
    """The full IRI -> a display curie (`cmns-org:LegalEntity`), or the IRI if no prefix matches."""
    for base, pfx in _IRI_TO_PREFIX:
        if iri.startswith(base):
            return f"{pfx}:{iri[len(base):]}"
    return iri


@functools.lru_cache(maxsize=1)
def _raw_tbox() -> Graph:
    """The parsed-but-un-reasoned TBox. Cached; callers must not mutate — use `tbox_copy()`."""
    g = Graph()
    g.parse(_SLICE, format="turtle")
    return g


@functools.lru_cache(maxsize=1)
def load_tbox() -> Graph:
    """The reasoned TBox (OWL-RL deductive closure). Cached; treat as read-only."""
    from owlrl import DeductiveClosure, OWLRL_Semantics

    g = tbox_copy()
    DeductiveClosure(OWLRL_Semantics).expand(g)
    return g


def tbox_copy() -> Graph:
    """A fresh, un-reasoned copy of the TBox — for materializing an ABox before reasoning."""
    g = Graph()
    for triple in _raw_tbox():
        g.add(triple)
    return g


def all_classes() -> list[dict[str, Any]]:
    """Our FIBO classes with label + (curie'd) parents — the /fibo/classes payload."""
    g = load_tbox()
    out: list[dict[str, Any]] = []
    for c in set(g.subjects(RDF.type, OWL.Class)):
        if not isinstance(c, URIRef) or not str(c).startswith(_OUR_BASES):
            continue
        label = g.value(c, RDFS.label)
        parents = sorted(
            curie_for(str(p))
            for p in g.objects(c, RDFS.subClassOf)
            if isinstance(p, URIRef) and p != c and str(p).startswith(_OUR_BASES)
        )
        out.append({
            "iri": str(c),
            "curie": curie_for(str(c)),
            "label": str(label) if label else None,
            "parents": parents,
        })
    out.sort(key=lambda d: d["curie"])
    return out
