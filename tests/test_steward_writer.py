"""M2 / steward — GraphWriter persistence (guarded: needs the worktree Neo4j).

Proves the M2 acceptance "a planted bad triple is rejected AND viewable": the bad-domain triple is
rejected by review and persisted as a queryable :RejectedFact, while a valid triple is written as an
edge carrying PROVENANCE_FIELDS + bitemporal tags. All test nodes are cleaned up afterwards.
"""

from __future__ import annotations

import pytest

from api.config import get_settings
from api.providers.fake import FakeProvider
from api.steward.steward import CandidateTriple, GraphWriter, Steward, existing_from_store
from api.stores.neo4j import Neo4jStore

SUBJ = "Steward Test Alpha"
OBJ = "Steward Test Beta"
BAD_FUND = "Steward Test Fund"


def _store_or_skip() -> Neo4jStore:
    store = Neo4jStore(get_settings())
    try:
        store.verify_connectivity()
    except Exception:  # noqa: BLE001 - offline CI: skip rather than fail
        store.close()
        pytest.skip("Neo4j not reachable; skipping steward writer integration test")
    return store


def _triple(sl, sk, rt, ol, ok, *, conf=1.0, span="span", doc="d1", **qual) -> CandidateTriple:
    return CandidateTriple(
        subject_label=sl,
        subject_key=sk,
        rel_type=rt,
        object_label=ol,
        object_key=ok,
        qualifiers=qual,
        provenance={
            "span": span,
            "doc_id": doc,
            "as_of": "2026-05-31",
            "confidence": conf,
            "sensitivity": "public",
            "extractor_model": "test",
        },
        confidence=conf,
    )


def _cleanup(store: Neo4jStore) -> None:
    store.run(
        "MATCH (n:Company) WHERE n.name IN $names DETACH DELETE n",
        names=[SUBJ, OBJ],
    )
    store.run("MATCH (f:Fund {name: $n}) DETACH DELETE f", n=BAD_FUND)
    store.run("MATCH (rf:RejectedFact) WHERE rf.subject_key = $n DETACH DELETE rf", n=BAD_FUND)


def test_writer_persists_edges_rejected_and_reconciliation():
    store = _store_or_skip()
    try:
        _cleanup(store)
        steward = Steward(provider=FakeProvider())
        writer = GraphWriter(store)

        # 1) valid edge + a planted bad-domain triple (Fund cannot be a SUPPLIES_TO subject)
        triples = [
            _triple("Company", SUBJ, "SUPPLIES_TO", "Company", OBJ, doc="da", criticality="critical"),
            _triple("Fund", BAD_FUND, "SUPPLIES_TO", "Company", OBJ, doc="db"),
        ]
        result = steward.review(triples)
        counts = writer.write(result)
        assert counts == {"edges": 1, "rejected": 1}

        # accepted edge carries provenance + bitemporal + corroboration
        edge = store.run(
            "MATCH (:Company {name:$s})-[r:SUPPLIES_TO]->(:Company {name:$o}) "
            "RETURN r.criticality AS crit, r.doc_id AS doc, r.confidence AS conf, "
            "r.sensitivity AS sens, r.valid_from AS vf, r.recorded_at AS ra, "
            "r.corroboration AS corr, r.conflict AS conflict, r.doc_ids AS doc_ids",
            s=SUBJ,
            o=OBJ,
        )[0]
        assert edge["crit"] == "critical"
        assert edge["doc"] == "da"
        assert edge["sens"] == "public"
        assert edge["vf"] == "2026-05-31"
        assert edge["ra"] is not None
        assert edge["corr"] == 1
        assert edge["conflict"] is False
        assert edge["doc_ids"] == ["da"]

        # planted bad triple is rejected AND viewable as a :RejectedFact
        rej = store.run(
            "MATCH (rf:RejectedFact {subject_key:$n}) "
            "RETURN rf.reason AS reason, rf.rel_type AS rt, rf.object_key AS ok",
            n=BAD_FUND,
        )
        assert len(rej) == 1
        assert rej[0]["reason"].startswith("bad_domain")
        assert rej[0]["rt"] == "SUPPLIES_TO"

        # 2) re-run the SAME valid triple, deduped against the live graph → corroboration bumps
        again = _triple("Company", SUBJ, "SUPPLIES_TO", "Company", OBJ, doc="dc", criticality="critical")
        existing = existing_from_store(store, [again])
        result2 = steward.review([again], existing=existing)
        assert result2.decisions[0].action == "corroborate"
        writer.write(result2)
        corr = store.run(
            "MATCH (:Company {name:$s})-[r:SUPPLIES_TO]->(:Company {name:$o}) "
            "RETURN r.corroboration AS corr, r.doc_ids AS doc_ids",
            s=SUBJ,
            o=OBJ,
        )[0]
        assert corr["corr"] == 2
        assert set(corr["doc_ids"]) == {"da", "dc"}
    finally:
        _cleanup(store)
        store.close()
