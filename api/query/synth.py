"""Synthesizer (Role.SYNTHESIZER) — grounded, cited answers with an entitlement wall.

Two responsibilities:
  * **Entitlement filter** — `gather_evidence_chunks` pulls the `:Chunk`s that mention the question's
    entities and applies the sensitivity predicate IN CYPHER (`coalesce(c.sensitivity,'public') IN
    $ent`). Chunks outside the caller's scope are counted into `withheld_count`, never returned.
  * **Answer composition** — the answer is built ONLY from the evidence passed in (analytics rows,
    graph rows, visible chunks); it is never drawn from the model's parametric memory. Every claim
    carries a citation. The Role.SYNTHESIZER LLM may phrase the prose (cassette-ready), but the
    deterministic, evidence-grounded answer is what stub mode returns.
"""

from __future__ import annotations

from typing import Any

from ..providers.base import Role
from ..tools.analytics import AnalyticsResult
from .cypher_agent import CypherResult

SYNTH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
}

SYNTH_SYSTEM = (
    "You are a financial-analysis synthesizer. Write a concise answer to the question using ONLY the "
    "supplied evidence (graph rows and document snippets). Cite the sources you use. If evidence is "
    "insufficient, say so. Never introduce facts that are not in the evidence."
)

# Entitlement filter lives in Cypher: the `visible` flag IS the sensitivity predicate.
# `$firm`/`$firm_companies` firm-scope evidence the same way `VECTOR_CYPHER` does: a chunk qualifies
# if it MENTIONS one of the firm's held companies OR belongs to a Document stamped `d.firm = $firm`
# (the provenance branch surfaces the firm's own fund-prospectus chunks, which mention the Fund, not
# a company). Both params NULL (no active firm) keeps the query global — today's behaviour, unchanged.
EVIDENCE_CHUNKS_CYPHER = (
    "MATCH (c:Chunk)-[:MENTIONS]->(e) "
    "WHERE ((e:Company AND e.name IN $names) OR (e:RiskFactor AND e.title IN $names)) "
    "  AND (($firm IS NULL AND $firm_companies IS NULL) "
    "       OR ($firm_companies IS NOT NULL AND "
    "           EXISTS { MATCH (c)-[:MENTIONS]->(x) WHERE x.name IN $firm_companies }) "
    "       OR ($firm IS NOT NULL AND "
    "           EXISTS { MATCH (d:Document {doc_id: c.doc_id}) WHERE d.firm = $firm })) "
    "WITH DISTINCT c, coalesce(c.sensitivity, 'public') AS sens "
    "OPTIONAL MATCH (d:Document {doc_id: c.doc_id}) "
    "RETURN c.chunk_id AS chunk_id, c.doc_id AS doc_id, c.text AS text, c.page AS page, "
    "       d.title AS title, sens AS sensitivity, (sens IN $ent) AS visible "
    "ORDER BY chunk_id"
)


def gather_evidence_chunks(
    store: Any,
    names: list[str],
    entitlements: list[str],
    firm: str | None = None,
    firm_companies: list[str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Return (visible chunks, withheld_count) for chunks mentioning any of `names`.

    The entitlement predicate is evaluated in Cypher; withheld chunks are counted, not returned.
    `firm`/`firm_companies` (when non-null) additionally firm-scope the chunks by held-company mention
    or Document provenance (see `EVIDENCE_CHUNKS_CYPHER`).
    """
    if not names:
        return [], 0
    rows = store.run(
        EVIDENCE_CHUNKS_CYPHER,
        names=names,
        ent=entitlements,
        firm=firm,
        firm_companies=firm_companies,
    )
    visible = [r for r in rows if r.get("visible")]
    withheld = sum(1 for r in rows if not r.get("visible"))
    return visible, withheld


def _snippet(text: str | None, limit: int = 200) -> str:
    if not text:
        return ""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Synthesizer:
    """Compose a cited answer from evidence; enforce grounding + the entitlement wall."""

    def __init__(self, *, provider: Any = None, settings: Any = None) -> None:
        self._provider = provider
        self.settings = settings

    @property
    def provider(self) -> Any:
        if self._provider is None:
            from ..config import get_settings
            from ..providers.factory import get_llm_provider

            self._provider = get_llm_provider(self.settings or get_settings())
        return self._provider

    # -- deterministic, grounded composition ----------------------------------------------

    def _compose_answer(
        self,
        plan_fund: str | None,
        analytics: AnalyticsResult | None,
        cres: CypherResult | None,
        chunks: list[dict[str, Any]],
        withheld: int,
    ) -> str:
        parts: list[str] = []
        if analytics is not None and analytics.rows:
            parts.append(_analytics_sentence(plan_fund, analytics))
        elif cres is not None and cres.source == "graph" and cres.rows:
            parts.append(f"The graph returned {len(cres.rows)} row(s) for this query.")
        elif cres is not None and cres.source == "text_vector":
            parts.append(f"No structured graph answer; {cres.note}.")

        if chunks:
            docs = sorted({c["doc_id"] for c in chunks})
            parts.append("Supporting sources: " + ", ".join(docs) + ".")
        if withheld:
            parts.append(
                f"{withheld} source(s) withheld due to your entitlement scope."
            )
        if not parts:
            parts.append("No matching graph or document evidence was found.")
        return " ".join(parts)

    def _citations(
        self, analytics: AnalyticsResult | None, cres: CypherResult | None, chunks: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        cites: list[dict[str, Any]] = []
        if analytics is not None:
            cites.append(
                {"source": "analytics", "tool": analytics.tool, "cypher": analytics.cypher}
            )
        if cres is not None and cres.source == "graph" and cres.cypher:
            cites.append({"source": "graph", "cypher": cres.cypher})
        # chunks from either the wall gather or the vector fallback
        vector_chunks = cres.chunks if (cres is not None and cres.source == "text_vector") else []
        for c in [*chunks, *vector_chunks]:
            cites.append(
                {
                    "source": "document",
                    "doc_id": c.get("doc_id"),
                    "chunk_id": c.get("chunk_id"),
                    "sensitivity": c.get("sensitivity", "public"),
                    "snippet": _snippet(c.get("text")),
                }
            )
        return cites

    def synthesize(
        self,
        question: str,
        *,
        plan_fund: str | None = None,
        analytics: AnalyticsResult | None = None,
        cres: CypherResult | None = None,
        chunks: list[dict[str, Any]] | None = None,
        withheld_count: int = 0,
    ) -> dict[str, Any]:
        chunks = chunks or []
        answer = self._compose_answer(plan_fund, analytics, cres, chunks, withheld_count)
        citations = self._citations(analytics, cres, chunks)

        # Optional LLM phrasing (cassette-ready). Only adopted if it returns non-empty prose;
        # stub mode returns "" so the deterministic, grounded answer stands. If the provider FAILS
        # (e.g. zero credits), we keep the deterministic answer and surface `narration_unavailable`.
        narration_unavailable: str | None = None
        try:
            evidence_blob = _evidence_blob(answer, citations)
            result = self.provider.complete_structured(
                role=Role.SYNTHESIZER,
                schema=SYNTH_SCHEMA,
                system=SYNTH_SYSTEM,
                messages=[{"role": "user", "content": f"Q: {question}\n\nEVIDENCE:\n{evidence_blob}"}],
            )
            llm_answer = str((result.data or {}).get("answer") or "").strip()
            if llm_answer:
                answer = llm_answer
        except Exception as exc:  # noqa: BLE001 - synthesis must not fail on provider hiccups
            narration_unavailable = str(exc)[:200]

        return {
            "answer": answer,
            "citations": citations,
            "withheld_count": withheld_count,
            "narration_unavailable": narration_unavailable,
        }


def _analytics_sentence(fund: str | None, a: AnalyticsResult) -> str:
    if not a.rows:
        return f"{a.tool} found nothing for this scope."
    scope = fund or "the portfolio"
    if a.tool == "shared_suppliers":
        top = a.rows[0]
        holdings = ", ".join(sorted(top["holdings"]))
        s = (
            f"Within {scope}, {top['n']} holding(s) ({holdings}) share the critical "
            f"supplier {top['supplier']}."
        )
        if len(a.rows) > 1:
            s += f" ({len(a.rows)} shared suppliers in total.)"
        return s
    if a.tool == "second_order_chokepoints":
        top = a.rows[0]
        return (
            f"{scope.capitalize()} is exposed to second-order chokepoint {top['choke']}, reachable "
            f"from {top['reachable']} holding(s) via {', '.join(sorted(top['via']))}."
        )
    if a.tool == "coverage_gap":
        risks = ", ".join(sorted(r["uncovered_risk"] for r in a.rows))
        issuers = sorted({i for r in a.rows for i in r["exposed_issuers"]})
        return (
            f"Disclosure coverage gap in {scope}: {len(a.rows)} risk factor(s) ({risks}) are carried "
            f"by held issuers ({', '.join(issuers)}) but covered by no prospectus section."
        )
    if a.tool == "concentration_summary":
        summ = a.summary
        return (
            f"{fund or 'The fund'} holds {summ.get('n_holdings')} positions "
            f"(HHI {summ.get('hhi')}, top-5 weight {summ.get('top5_weight_pct')}%)."
        )
    if a.tool == "exposure_path":
        row = a.rows[0]
        return f"Exposure path: {' → '.join(row['path'])} ({row['hops']} hop(s))."
    if a.tool == "board_and_alumni_links":
        names = ", ".join(sorted(r["person"] for r in a.rows))
        return f"Board/alumni links run through: {names}."
    return f"{a.tool} returned {len(a.rows)} row(s)."


def _evidence_blob(answer: str, citations: list[dict[str, Any]]) -> str:
    lines = [answer, ""]
    for c in citations:
        if c["source"] == "document":
            lines.append(f"- [{c.get('sensitivity')}] {c.get('doc_id')}: {c.get('snippet')}")
        else:
            lines.append(f"- {c['source']}: {c.get('tool') or c.get('cypher')}")
    return "\n".join(lines)
