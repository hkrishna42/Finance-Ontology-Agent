"""Text-to-Cypher agent (Role.ANALYST) with read-only enforced TWICE and an honest fallback.

Generation: the Analyst LLM is given the ontology `schema_card()` + few-shots and asked for a single
read-only Cypher query (`CYPHER_SCHEMA`). Every candidate is gated twice before it can touch data:

  1. `cypher_guard.is_read_only` — static blocklist (no CREATE/MERGE/DELETE/SET/…, no stacked
     statements, procedure allowlist), and
  2. execution inside a **reader-mode** Neo4j transaction (`session.execute_read`), so even a write
     that somehow passed the static gate is refused by the database.

Up to `max_retries` (3) attempts; on rejection/error the failing query + reason is fed back to the
model. If the graph path still fails, we fall back to vector search over `:Chunk` and label the
answer "answered from text, not graph" — never a silent graph-looking answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..ontology.schema import schema_card
from ..providers.base import Role
from ..tools.cypher_guard import assert_read_only, is_read_only

CYPHER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["cypher"],
    "properties": {
        "cypher": {"type": "string", "description": "a single read-only Cypher query"},
        "rationale": {"type": "string"},
    },
}

FEW_SHOTS = [
    (
        "What does the Growth fund hold?",
        "MATCH (f:Fund {name:'Demo Growth Fund'})-[h:HOLDS]->(c:Company) "
        "RETURN c.name AS company, h.weight_pct AS weight ORDER BY weight DESC",
    ),
    (
        "Who are NVIDIA's critical suppliers?",
        "MATCH (c:Company {name:'NVIDIA'})-[s:SUPPLIES_TO]->(sup:Company) "
        "WHERE s.criticality = 'critical' RETURN sup.name AS supplier",
    ),
    (
        "Which risk factors is NVIDIA exposed to?",
        "MATCH (c:Company {name:'NVIDIA'})-[:EXPOSED_TO]->(r:RiskFactor) RETURN r.title AS risk",
    ),
]

FALLBACK_NOTE = "answered from text, not graph"

VECTOR_CYPHER = (
    "CALL db.index.vector.queryNodes('chunk_embedding', $k, $qv) YIELD node, score "
    "WHERE coalesce(node.sensitivity, 'public') IN $ent "
    "OPTIONAL MATCH (d:Document {doc_id: node.doc_id}) "
    "RETURN node.chunk_id AS chunk_id, node.doc_id AS doc_id, node.text AS text, node.page AS page, "
    "       d.title AS title, coalesce(node.sensitivity,'public') AS sensitivity, score "
    "ORDER BY score DESC"
)


def _analyst_system() -> str:
    return (
        "You translate a user question into ONE read-only Cypher query over the fund knowledge "
        "graph. Use only the labels/relationships in the ontology below. Never write "
        "(no CREATE/MERGE/DELETE/SET/REMOVE). Always RETURN named columns and add a LIMIT when a "
        "question is open-ended.\n\n" + schema_card()
    )


# --- reader-mode execution (the second read-only enforcement) ------------------------------


def _reader_run(store: Any, cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Execute `cypher` in a READ transaction; the DB refuses any write clause that slipped through."""
    def work(tx: Any) -> list[dict[str, Any]]:
        result = tx.run(cypher, **params)
        return [record.data() for record in result]

    with store.driver.session() as session:
        return session.execute_read(work)


def read_only_query(
    store: Any, cypher: str, params: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Guard #1 (static) + Guard #2 (reader tx). Raises `ValueError` if the query is not read-only."""
    assert_read_only(cypher)
    return _reader_run(store, cypher, params or {})


# --- result model --------------------------------------------------------------------------


@dataclass
class CypherAttempt:
    cypher: str
    ok: bool
    error: str | None = None


@dataclass
class CypherResult:
    ok: bool
    source: str  # "graph" | "text_vector"
    cypher: str | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)
    attempts: list[CypherAttempt] = field(default_factory=list)
    chunks: list[dict[str, Any]] = field(default_factory=list)
    note: str | None = None
    provider_error: str | None = None  # set when the Analyst LLM call itself failed (e.g. no credits)


class CypherAgent:
    """Generate + safely run Cypher, with a labelled vector fallback."""

    def __init__(
        self,
        *,
        provider: Any = None,
        settings: Any = None,
        embedder: Any = None,
        max_retries: int = 3,
    ) -> None:
        self._provider = provider
        self._embedder = embedder
        self.settings = settings
        self.max_retries = max_retries

    @property
    def provider(self) -> Any:
        if self._provider is None:
            from ..config import get_settings
            from ..providers.factory import get_llm_provider

            self._provider = get_llm_provider(self.settings or get_settings())
        return self._provider

    @property
    def embedder(self) -> Any:
        if self._embedder is None:
            from ..config import get_settings
            from ..providers.embeddings import get_embedder

            self._embedder = get_embedder(self.settings or get_settings())
        return self._embedder

    # -- generation -----------------------------------------------------------------------

    def _messages(self, question: str) -> list[dict[str, str]]:
        shots = "\n".join(f"Q: {q}\nCypher: {c}" for q, c in FEW_SHOTS)
        return [
            {"role": "user", "content": f"{shots}\n\nQ: {question}\nCypher:"},
        ]

    def generate_and_run(self, store: Any, question: str) -> CypherResult:
        """Try to answer from the graph: generate → gate twice → execute, with retries."""
        system = _analyst_system()
        messages = self._messages(question)
        attempts: list[CypherAttempt] = []

        for _ in range(self.max_retries):
            try:
                result = self.provider.complete_structured(
                    role=Role.ANALYST,
                    schema=CYPHER_SCHEMA,
                    system=system,
                    messages=messages,
                    cache_system=True,
                )
            except Exception as exc:  # noqa: BLE001 - provider down / no credits → degrade, don't 500
                reason = str(exc)[:200]
                attempts.append(CypherAttempt("", ok=False, error=f"provider_error: {reason}"))
                return CypherResult(
                    ok=False, source="graph", attempts=attempts, provider_error=reason
                )
            cypher = str((result.data or {}).get("cypher") or "").strip()

            if not is_read_only(cypher):
                attempts.append(CypherAttempt(cypher, ok=False, error="rejected_not_read_only"))
                messages = messages + [
                    {"role": "assistant", "content": cypher or "(empty)"},
                    {
                        "role": "user",
                        "content": "That was empty or not read-only. Return ONE read-only "
                        "MATCH ... RETURN query using only ontology labels.",
                    },
                ]
                continue

            try:
                rows = _reader_run(store, cypher, {})
            except Exception as exc:  # noqa: BLE001 - feed the DB error back to the model
                attempts.append(CypherAttempt(cypher, ok=False, error=str(exc)[:200]))
                messages = messages + [
                    {"role": "assistant", "content": cypher},
                    {"role": "user", "content": f"That query failed: {str(exc)[:200]}. Fix it."},
                ]
                continue

            attempts.append(CypherAttempt(cypher, ok=True))
            return CypherResult(ok=True, source="graph", cypher=cypher, rows=rows, attempts=attempts)

        return CypherResult(ok=False, source="graph", attempts=attempts)

    # -- vector fallback ------------------------------------------------------------------

    def vector_fallback(
        self, store: Any, question: str, *, entitlements: list[str], k: int = 5
    ) -> CypherResult:
        """Answer from :Chunk text via the vector index, entitlement-filtered + honestly labelled."""
        qv = self.embedder.embed([question])[0]
        try:
            chunks = _reader_run(
                store, VECTOR_CYPHER, {"k": k, "qv": [float(x) for x in qv], "ent": entitlements}
            )
        except Exception:  # noqa: BLE001 - fallback must not raise
            chunks = []
        return CypherResult(
            ok=bool(chunks),
            source="text_vector",
            chunks=chunks,
            note=FALLBACK_NOTE,
        )

    def answer(self, store: Any, question: str, *, entitlements: list[str]) -> CypherResult:
        """Graph-first; on failure, the labelled vector fallback."""
        graph = self.generate_and_run(store, question)
        if graph.ok:
            return graph
        fallback = self.vector_fallback(store, question, entitlements=entitlements)
        fallback.attempts = graph.attempts
        fallback.provider_error = graph.provider_error
        return fallback
