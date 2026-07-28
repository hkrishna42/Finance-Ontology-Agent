"""M3 — text-to-Cypher agent: read-only enforced TWICE, injection cannot write, vector fallback.

Offline unit tests cover guard #1 (static) and the injection defense. Guard #2 (reader-mode Neo4j
transaction) and the happy graph/vector paths are validated against the seeded graph when reachable.
"""

from __future__ import annotations

import pytest
from neo4j.exceptions import Neo4jError

from api.config import get_settings
from api.providers.base import StructuredResult, Usage
from api.providers.fake import FakeProvider
from api.query.cypher_agent import CypherAgent, _reader_run, read_only_query
from api.stores.neo4j import Neo4jStore

INJECTIONS = [
    "MATCH (n) DETACH DELETE n",
    "MATCH (n) RETURN n; DROP DATABASE neo4j",
    "CREATE (:Evil) RETURN 1",
    "MATCH (c:Company) SET c.hacked = true RETURN c",
]


class _MaliciousProvider:
    """Simulates a jailbroken Analyst that keeps trying to emit a destructive write."""

    def __init__(self, cypher: str):
        self.cypher = cypher
        self.calls = 0

    def complete_structured(self, **kw) -> StructuredResult:
        self.calls += 1
        return StructuredResult(data={"cypher": self.cypher}, raw="", model="stub", usage=Usage())

    def complete(self, **kw):  # pragma: no cover - not used
        raise NotImplementedError


class _GoodProvider:
    def __init__(self, cypher: str):
        self.cypher = cypher

    def complete_structured(self, **kw) -> StructuredResult:
        return StructuredResult(data={"cypher": self.cypher}, raw="", model="stub", usage=Usage())

    def complete(self, **kw):  # pragma: no cover
        raise NotImplementedError


# --- guard #1: static read-only gate (offline) ---------------------------------------------


@pytest.mark.parametrize("cypher", INJECTIONS)
def test_read_only_query_rejects_writes_before_touching_store(cypher):
    # store is None: if the guard let a write through it would AttributeError on None.driver;
    # instead it must raise ValueError first.
    with pytest.raises(ValueError):
        read_only_query(None, cypher)


def test_injection_never_executes_and_falls_back():
    agent = CypherAgent(provider=_MaliciousProvider("MATCH (n) DETACH DELETE n"), max_retries=3)
    # store is None → proof the destructive query is never sent to the database.
    result = agent.generate_and_run(store=None, question="list everything then delete all nodes")
    assert result.ok is False
    assert len(result.attempts) == 3
    assert all(a.error == "rejected_not_read_only" for a in result.attempts)


def test_empty_generation_is_rejected():
    # FakeProvider (no fixture) → minimal instance → cypher "" → rejected as not read-only.
    agent = CypherAgent(provider=FakeProvider(), max_retries=2)
    result = agent.generate_and_run(store=None, question="anything")
    assert result.ok is False
    assert all(a.error == "rejected_not_read_only" for a in result.attempts)


# --- guard #2 + happy paths: reader-mode transaction (guarded) ------------------------------


def _store_or_skip() -> Neo4jStore:
    store = Neo4jStore(get_settings())
    try:
        store.verify_connectivity()
    except Exception:  # noqa: BLE001
        store.close()
        pytest.skip("Neo4j not reachable; skipping cypher-agent integration test")
    return store


def test_reader_mode_transaction_refuses_writes():
    store = _store_or_skip()
    try:
        before = store.run("MATCH (n) RETURN count(n) AS c")[0]["c"]
        # Bypass guard #1 and hand a write straight to the reader tx → the DB must refuse it.
        with pytest.raises(Neo4jError):
            _reader_run(store, "CREATE (:ShouldNotExist) RETURN 1", {})
        after = store.run("MATCH (n) RETURN count(n) AS c")[0]["c"]
        assert before == after
        assert store.run("MATCH (n:ShouldNotExist) RETURN count(n) AS c")[0]["c"] == 0
    finally:
        store.close()


def test_valid_generated_cypher_runs_against_seed():
    store = _store_or_skip()
    try:
        good = _GoodProvider(
            "MATCH (f:Fund {name:'Demo Growth Fund'})-[:HOLDS]->(c:Company) "
            "RETURN count(c) AS n"
        )
        agent = CypherAgent(provider=good)
        result = agent.generate_and_run(store, "how many holdings does the Demo Growth Fund have")
        assert result.ok is True
        assert result.source == "graph"
        assert result.rows[0]["n"] == 9
    finally:
        store.close()


def test_vector_fallback_is_labelled():
    store = _store_or_skip()
    try:
        agent = CypherAgent(settings=get_settings())
        result = agent.vector_fallback(store, "foundry dependency risk", entitlements=["public"])
        assert result.source == "text_vector"
        assert result.note == "answered from text, not graph"
        # public scope must never surface the internal note
        assert all(c["sensitivity"] == "public" for c in result.chunks)
    finally:
        store.close()
