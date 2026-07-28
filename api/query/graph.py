"""QueryGraph — orchestrate Router → (analytics | text-to-Cypher + vector) → Synthesizer, and
serialize to the UI contract (web/src/types.ts :: QueryResponse).

`answer(question, entitlements, mode)` returns a `QueryAnswer` whose `as_dict()` is exactly the
`QueryResponse` the UI expects: `{question, mode, answer, citations[], graph_paths[], plan, cypher?,
withheld_count, vector_fragments?, vector_answer?}` (plus `source` and, when the LLM narration
failed, `narration_unavailable` — extra fields the UI safely ignores).

Modes: UI `graph` / `side_by_side`; legacy `auto` / `graph_only` / `vector_only` still accepted.
Everything is graph-first with a validated analytics tool preferred; provider failures degrade
gracefully (never a 500).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..tools import analytics as A
from .cypher_agent import CypherAgent, CypherResult
from .router import Router, RouterPlan, Vocab, load_graph_vocab
from .synth import Synthesizer, gather_evidence_chunks


@dataclass
class QueryAnswer:
    question: str
    mode: str  # 'graph' | 'side_by_side'
    answer: str
    citations: list[dict[str, Any]]
    graph_paths: list[dict[str, Any]]
    plan: dict[str, Any]  # {strategy, steps[]}
    withheld_count: int
    source: str  # analytics | graph | text_vector  (extra; UI ignores)
    cypher: str | None = None
    vector_fragments: list[dict[str, Any]] | None = None
    vector_answer: str | None = None
    narration_unavailable: str | None = None

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "question": self.question,
            "mode": self.mode,
            "answer": self.answer,
            "citations": self.citations,
            "graph_paths": self.graph_paths,
            "plan": self.plan,
            "withheld_count": self.withheld_count,
            "source": self.source,
        }
        if self.cypher is not None:
            d["cypher"] = self.cypher
        if self.mode == "side_by_side":
            d["vector_fragments"] = self.vector_fragments or []
            d["vector_answer"] = self.vector_answer or ""
        if self.narration_unavailable:
            d["narration_unavailable"] = self.narration_unavailable
        return d


def _snippet(text: str | None, limit: int = 240) -> str:
    if not text:
        return ""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _to_citation(c: dict[str, Any]) -> dict[str, Any]:
    cid = c.get("chunk_id") or c.get("doc_id") or ""
    cit: dict[str, Any] = {
        "id": cid,
        "doc_id": c.get("doc_id"),
        "chunk_id": c.get("chunk_id"),
        "span": _snippet(c.get("text")),
        "sensitivity": c.get("sensitivity", "public"),
    }
    if c.get("title"):
        cit["title"] = c["title"]
    if c.get("page") is not None:
        cit["page"] = c["page"]
    return cit


def _analytics_entities(analytics: A.AnalyticsResult) -> list[str]:
    """Entity names an analytics result is 'about' — used to pull supporting citation chunks."""
    names: set[str] = set()
    if analytics.tool == "shared_suppliers":
        for r in analytics.rows:
            names.add(r["supplier"])
            names.update(r["holdings"])
    elif analytics.tool == "second_order_chokepoints":
        for r in analytics.rows:
            names.add(r["choke"])
            names.update(r["via"])
    elif analytics.tool == "coverage_gap":
        for r in analytics.rows:
            names.update(r["exposed_issuers"])
    elif analytics.tool == "exposure_path":
        for r in analytics.rows:
            names.update(r["path"])
    elif analytics.tool == "board_and_alumni_links":
        for r in analytics.rows:
            for link in r["other_links"]:
                if link.get("company"):
                    names.add(link["company"])
    return sorted(names)


# --- UI graph_paths (one GraphPath = {label?, steps:[{from,rel,to,qualifiers?}]}) -----------


def _step(frm: str, rel: str, to: str, **quals: str) -> dict[str, Any]:
    step: dict[str, Any] = {"from": frm, "rel": rel, "to": to}
    if quals:
        step["qualifiers"] = {k: str(v) for k, v in quals.items()}
    return step


def _ui_graph_paths(analytics: A.AnalyticsResult, fund: str | None) -> list[dict[str, Any]]:
    paths: list[dict[str, Any]] = []
    if not analytics.rows:
        return paths

    if analytics.tool == "shared_suppliers":
        top = analytics.rows[0]
        for h in sorted(top["holdings"]):
            steps = []
            if fund:
                steps.append(_step(fund, "HOLDS", h))
            steps.append(_step(h, "SUPPLIES_TO", top["supplier"], criticality="critical"))
            paths.append(
                {"label": f"{fund or 'Portfolio'} → {h} → {top['supplier']}", "steps": steps}
            )
    elif analytics.tool == "second_order_chokepoints":
        top = analytics.rows[0]
        for h in sorted(top["via"]):
            paths.append(
                {
                    "label": f"{h} → … → {top['choke']}",
                    "steps": [_step(h, "SUPPLIES_TO*1..2", top["choke"])],
                }
            )
    elif analytics.tool == "coverage_gap":
        for r in analytics.rows:
            steps = [
                _step(i, "EXPOSED_TO", r["uncovered_risk"], category=r.get("category") or "")
                for i in sorted(r["exposed_issuers"])
            ]
            steps.append(
                _step("prospectus (485BPOS)", "COVERS?", r["uncovered_risk"], result="MISSING")
            )
            paths.append({"label": f"Uncovered: {r['uncovered_risk']}", "steps": steps})
    elif analytics.tool == "exposure_path":
        for r in analytics.rows:
            nodes, edges = r["path"], r.get("edges") or []
            steps = [
                _step(
                    nodes[i],
                    "SUPPLIES_TO",
                    nodes[i + 1],
                    **({"detail": edges[i]} if i < len(edges) else {}),
                )
                for i in range(len(nodes) - 1)
            ]
            if steps:
                paths.append({"label": f"{nodes[0]} → {nodes[-1]}", "steps": steps})
    elif analytics.tool == "board_and_alumni_links":
        company = str(analytics.params.get("company") or "")
        for r in analytics.rows:
            steps = [_step(r["person"], r["role_at_company"], company)]
            for link in r["other_links"]:
                if link.get("company"):
                    steps.append(_step(r["person"], link.get("link") or "LINK", link["company"]))
            paths.append({"label": f"{r['person']} interlocks/alumni", "steps": steps})
    return paths


# --- UI plan {strategy, steps[]} -----------------------------------------------------------

_PLAN_STEPS: dict[str, tuple[str, list[str]]] = {
    "shared_suppliers": (
        "deterministic analytics: shared critical supplier (validated read-only Cypher)",
        [
            "Match fund HOLDS → issuer → SUPPLIES_TO(criticality=critical) → supplier",
            "Group by supplier; count distinct dependent holdings",
            "Ground each dependency in a cited 10-K/20-F span",
        ],
    ),
    "second_order_chokepoints": (
        "deterministic analytics: 2nd-order chokepoint (validated read-only Cypher)",
        [
            "Traverse HOLDS → issuer → SUPPLIES_TO*1..2 → node not held by the fund",
            "Count distinct holdings reaching each downstream chokepoint",
        ],
    ),
    "coverage_gap": (
        "deterministic analytics: disclosure coverage gap (anti-join)",
        [
            "Find RiskFactors a held issuer is EXPOSED_TO",
            "Keep those with NO DisclosureSection-[:COVERS]->RiskFactor",
            "Report the uncovered risks + exposed issuers",
        ],
    ),
    "concentration_summary": (
        "deterministic analytics: concentration / HHI",
        ["Sum HOLDS weights per fund", "Compute HHI + top-5 concentration"],
    ),
    "exposure_path": (
        "deterministic analytics: shortest supply-chain path",
        ["shortestPath over SUPPLIES_TO between the two companies"],
    ),
    "board_and_alumni_links": (
        "deterministic analytics: board interlocks + alumni",
        [
            "Match Person DIRECTOR_OF/OFFICER_OF the company",
            "Follow their links to other companies",
        ],
    ),
}


def _ui_plan(
    plan: RouterPlan, analytics: A.AnalyticsResult | None, cres: CypherResult | None
) -> dict[str, Any]:
    if analytics is not None:
        strategy, steps = _PLAN_STEPS.get(
            analytics.tool, (f"analytics tool: {analytics.tool}", ["run validated Cypher"])
        )
        return {"strategy": strategy, "steps": list(steps)}
    if cres is not None and cres.source == "graph" and cres.ok:
        return {
            "strategy": "graph-first text-to-Cypher (read-only enforced)",
            "steps": [
                "Generate a read-only Cypher query",
                "Validate + execute in a reader tx",
                "Synthesize a grounded answer",
            ],
        }
    return {
        "strategy": "vector-RAG fallback over document text (answered from text, not graph)",
        "steps": [
            "Embed the question",
            "Retrieve nearest :Chunk passages (entitlement-filtered)",
            "Synthesize over the retrieved text",
        ],
    }


def _vector_fragments(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    frags: list[dict[str, Any]] = []
    for c in chunks:
        frag: dict[str, Any] = {
            "chunk_id": c.get("chunk_id"),
            "doc_id": c.get("doc_id"),
            "score": round(float(c.get("score", 0.0) or 0.0), 4),
            "text": c.get("text") or "",
        }
        if c.get("title"):
            frag["title"] = c["title"]
        frags.append(frag)
    return frags


def _vector_answer(chunks: list[dict[str, Any]]) -> str:
    if not chunks:
        return "No sufficiently similar passages were retrieved from the document text."
    top = chunks[0]
    text = " ".join((top.get("text") or "").split())
    sentence = text.split(". ")[0].strip()
    if sentence and not sentence.endswith("."):
        sentence += "."
    score = round(float(top.get("score", 0.0) or 0.0), 2)
    return f"Vector-RAG baseline (nearest passage, score {score}): {sentence}"


class QueryGraph:
    def __init__(
        self,
        store: Any,
        *,
        router: Router | None = None,
        cypher_agent: CypherAgent | None = None,
        synth: Synthesizer | None = None,
        settings: Any = None,
        vocab: Vocab | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.router = router or Router(settings=settings)
        self.cypher_agent = cypher_agent or CypherAgent(settings=settings)
        self.synth = synth or Synthesizer(settings=settings)
        self._vocab = vocab

    def vocab(self) -> Vocab:
        if self._vocab is None:
            self._vocab = load_graph_vocab(self.store)
        return self._vocab

    # -- evidence -------------------------------------------------------------------------

    def _run_graph_first(
        self, question: str, plan: RouterPlan, *, allow_fallback: bool
    ) -> tuple[A.AnalyticsResult | None, CypherResult | None, str]:
        if plan.tool is not None:
            fn = A.TOOLS[plan.tool]
            return fn(self.store, **plan.tool_args), None, "analytics"
        if allow_fallback:
            cres = self.cypher_agent.answer(self.store, question, entitlements=plan.entitlements)
        else:
            cres = self.cypher_agent.generate_and_run(self.store, question)
        return None, cres, cres.source

    # -- public API -----------------------------------------------------------------------

    def answer(
        self,
        question: str,
        *,
        entitlements: list[str] | None = None,
        mode: str = "graph",
    ) -> QueryAnswer:
        ui_mode = "side_by_side" if mode == "side_by_side" else "graph"
        allow_fallback = mode != "graph_only"
        plan = self.router.route(question, entitlements=entitlements, vocab=self.vocab())

        if mode == "vector_only":
            vcres = self.cypher_agent.vector_fallback(
                self.store, question, entitlements=plan.entitlements
            )
            out = self.synth.synthesize(question, plan_fund=plan.fund, cres=vcres)
            return QueryAnswer(
                question=question,
                mode=ui_mode,
                answer=out["answer"],
                citations=[_to_citation(c) for c in vcres.chunks],
                graph_paths=[],
                plan=_ui_plan(plan, None, vcres),
                withheld_count=out["withheld_count"],
                source="text_vector",
                cypher=None,
                narration_unavailable=out.get("narration_unavailable"),
            )

        analytics, cres, source = self._run_graph_first(
            question, plan, allow_fallback=allow_fallback
        )

        # Entitlement wall: gather supporting chunks for the answer's entities + count withheld.
        names = list(plan.entities)
        if analytics is not None:
            names = sorted(set(names) | set(_analytics_entities(analytics)))
        chunks, withheld = gather_evidence_chunks(self.store, names, plan.entitlements)

        out = self.synth.synthesize(
            question,
            plan_fund=plan.fund,
            analytics=analytics,
            cres=cres,
            chunks=chunks,
            withheld_count=withheld,
        )

        # Citations: gathered evidence chunks, plus vector chunks when we fell back to text.
        citation_chunks = list(chunks)
        if cres is not None and cres.source == "text_vector":
            seen = {c.get("chunk_id") for c in citation_chunks}
            citation_chunks += [c for c in cres.chunks if c.get("chunk_id") not in seen]
        citations = [_to_citation(c) for c in citation_chunks]

        cypher = (
            analytics.cypher
            if analytics is not None
            else (cres.cypher if (cres is not None and cres.source == "graph") else None)
        )
        graph_paths = _ui_graph_paths(analytics, plan.fund) if analytics is not None else []

        # narration_unavailable: prefer the synth reason; else a provider error from the agent.
        narration = out.get("narration_unavailable")
        if narration is None and cres is not None and cres.provider_error:
            narration = cres.provider_error

        vector_fragments = None
        vector_answer = None
        if ui_mode == "side_by_side":
            if cres is not None and cres.source == "text_vector":
                vres = cres
            else:
                vres = self.cypher_agent.vector_fallback(
                    self.store, question, entitlements=plan.entitlements
                )
            vector_fragments = _vector_fragments(vres.chunks)
            vector_answer = _vector_answer(vres.chunks)

        return QueryAnswer(
            question=question,
            mode=ui_mode,
            answer=out["answer"],
            citations=citations,
            graph_paths=graph_paths,
            plan=_ui_plan(plan, analytics, cres),
            withheld_count=out["withheld_count"],
            source=source,
            cypher=cypher,
            vector_fragments=vector_fragments,
            vector_answer=vector_answer,
            narration_unavailable=narration,
        )
