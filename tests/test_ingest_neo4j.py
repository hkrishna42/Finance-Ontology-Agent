"""M1 ingest — integration test against the worktree Neo4j (guarded; self-skips offline).

Runs the full pipeline with a canned extractor + fake resolver against a live database and asserts
the Document/Chunk/MENTIONS provenance and the steward-written semantic edge actually land, with
embeddings and PROVENANCE_FIELDS. Uses synthetic entity names so the shared seed graph stays
pristine; all test nodes are removed afterwards.
"""

from __future__ import annotations

import pytest

from api.config import get_settings
from api.contracts.events import EventType
from api.ingest.pipeline import ingest_document
from api.ingest.sources import source_from_text
from api.providers.base import StructuredResult, Usage
from api.stores.neo4j import Neo4jStore

DOC_ID = "ingest_it_doc"
ALPHA = "Ingest Test Alpha Corp"
BETA = "Ingest Test Beta Foundry"
RISK = "Ingest test supply concentration"

TEXT = (
    f"{ALPHA} depends on {BETA} for critical components used in its products. "
    f"{ALPHA} faces a supply concentration risk from this dependency."
)
_SPAN = f"{ALPHA} depends on {BETA} for critical components used in its products."

_CANNED = {
    "entities": [
        {"label": "Company", "name": ALPHA, "span": _SPAN, "confidence": 0.95},
        {"label": "Company", "name": BETA, "span": _SPAN, "confidence": 0.92},
        {"label": "RiskFactor", "name": RISK, "category": "supply_chain",
         "span": f"{ALPHA} faces a supply concentration risk", "confidence": 0.85},
    ],
    "relations": [
        {"type": "SUPPLIES_TO", "subject": ALPHA, "object": BETA,
         "qualifiers": {"criticality": "critical"}, "span": _SPAN, "confidence": 0.95},
    ],
}


class _Canned:
    def complete_structured(self, *, role, schema, system, messages, max_tokens=2048,
                            cache_system=False) -> StructuredResult:
        return StructuredResult(data=_CANNED, raw="{}", model="fake-canned",
                                usage=Usage(input_tokens=100, output_tokens=30))


class _Resolution:
    resolved = False
    cik = lei = ticker = None


class _Resolver:
    def resolve(self, name, *, ticker=None, enrich_lei=False, conn=None) -> _Resolution:
        return _Resolution()


def _store_or_skip() -> Neo4jStore:
    store = Neo4jStore(get_settings())
    try:
        store.verify_connectivity()
    except Exception:  # noqa: BLE001 - offline CI: skip rather than fail
        store.close()
        pytest.skip("Neo4j not reachable; skipping ingest integration test")
    return store


def _cleanup(store: Neo4jStore) -> None:
    store.run("MATCH (c:Chunk {doc_id:$d}) DETACH DELETE c", d=DOC_ID)
    store.run("MATCH (d:Document {doc_id:$d}) DETACH DELETE d", d=DOC_ID)
    store.run("MATCH (n:Company) WHERE n.name IN $names DETACH DELETE n", names=[ALPHA, BETA])
    store.run("MATCH (r:RiskFactor {title:$t}) DETACH DELETE r", t=RISK)


def test_pipeline_writes_full_provenance_to_live_graph():
    store = _store_or_skip()
    try:
        _cleanup(store)
        events = list(
            ingest_document(
                source_from_text(TEXT, doc_id=DOC_ID, doc_type="10-K", sensitivity="public"),
                store=store,
                provider=_Canned(),
                resolver=_Resolver(),
                queue=False,
            )
        )
        written = next(e for e in events if e.event == EventType.WRITTEN).data
        assert written["edges"] >= 4  # 3 MENTIONS + 1 SUPPLIES_TO
        assert written["rejected"] == 0

        # Document + embedded Chunk landed
        doc = store.run("MATCH (d:Document {doc_id:$d}) RETURN d.doc_type AS t", d=DOC_ID)
        assert doc and doc[0]["t"] == "10-K"
        chunk = store.run(
            "MATCH (c:Chunk {doc_id:$d}) RETURN size(c.embedding) AS n, c.sensitivity AS s",
            d=DOC_ID,
        )
        assert chunk and chunk[0]["n"] == 384 and chunk[0]["s"] == "public"

        # MENTIONS provenance chunk -> Company
        mentions = store.run(
            "MATCH (c:Chunk {doc_id:$d})-[:MENTIONS]->(n:Company {name:$a}) RETURN count(*) AS n",
            d=DOC_ID, a=ALPHA,
        )
        assert mentions[0]["n"] >= 1

        # Steward-written SUPPLIES_TO edge carries provenance + qualifier + bitemporal tags
        edge = store.run(
            "MATCH (:Company {name:$a})-[r:SUPPLIES_TO]->(:Company {name:$b}) "
            "RETURN r.criticality AS crit, r.doc_id AS doc, r.span AS span, "
            "r.extractor_model AS model, r.sensitivity AS sens, r.recorded_at AS ra, "
            "r.confidence AS conf",
            a=ALPHA, b=BETA,
        )
        assert edge, "SUPPLIES_TO edge not found in live graph"
        row = edge[0]
        assert row["crit"] == "critical"
        assert row["doc"] == DOC_ID
        assert row["model"] == "fake-canned"
        assert row["sens"] == "public"
        assert row["ra"] is not None
        assert row["span"].startswith(ALPHA)
    finally:
        _cleanup(store)
        store.close()
