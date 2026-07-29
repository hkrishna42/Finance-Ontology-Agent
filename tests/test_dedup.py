"""Canonical-identity dedup — variant Company mentions snap onto one node (offline).

Tests `api/ingest/pipeline._canonical_company_names` directly with a fake store, so no Neo4j / LLM.
"""

from __future__ import annotations

from api.ingest.pipeline import _canonical_company_names
from api.ontology.schema import neo4j_ddl
from api.resolution.resolver import Resolution


class _FakeStore:
    """Returns an existing Company name keyed by CIK / norm; else no match."""

    def __init__(self, by_cik: dict[str, str] | None = None, by_norm: dict[str, str] | None = None):
        self.by_cik = by_cik or {}
        self.by_norm = by_norm or {}

    def run(self, query: str, **params):
        if "cik" in params and params["cik"] in self.by_cik:
            return [{"name": self.by_cik[params["cik"]]}]
        if "norm" in params and params["norm"] in self.by_norm:
            return [{"name": self.by_norm[params["norm"]]}]
        return []


def _res(mention: str, cik: str, norm: str = "nvidia") -> Resolution:
    return Resolution(mention=mention, normalized=norm, ticker=None, status="resolved",
                      method="alias", cik=cik, lei=None, title=None, confidence=1.0)


def test_variant_snaps_onto_existing_cik_node():
    # the seed's "NVIDIA" node (cik 0001045810) already exists; a new "NVIDIA Corporation" mention
    # resolves to the same CIK -> it must reuse the existing node, not create a duplicate.
    store = _FakeStore(by_cik={"0001045810": "NVIDIA"})
    canon = _canonical_company_names(store, {"NVIDIA Corporation": _res("NVIDIA Corporation", "0001045810")})
    assert canon["NVIDIA Corporation"] == "NVIDIA"


def test_within_doc_variants_collapse_to_one():
    # two mentions in one document resolving to the same CIK collapse to a single canonical node
    store = _FakeStore()  # nothing pre-existing
    rmap = {
        "Acme Corporation": _res("Acme Corporation", "0009999999", norm="acme"),
        "Acme Corp": _res("Acme Corp", "0009999999", norm="acme"),
    }
    canon = _canonical_company_names(store, rmap)
    assert len(set(canon.values())) == 1
    assert canon["Acme Corporation"] == "Acme Corp"  # the shortest mention wins as canonical


def test_unresolved_distinct_names_are_not_merged():
    store = _FakeStore()
    rmap = {
        "Globex": Resolution("Globex", "globex", None, "provisional", "provisional",
                             None, None, None, 0.3),
        "Initech": Resolution("Initech", "initech", None, "provisional", "provisional",
                              None, None, None, 0.3),
    }
    canon = _canonical_company_names(store, rmap)
    assert canon["Globex"] == "Globex" and canon["Initech"] == "Initech"  # kept distinct


def test_ddl_has_company_norm_index():
    ddl = "\n".join(neo4j_ddl())
    assert "company_norm" in ddl and "c.norm" in ddl
