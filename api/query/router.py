"""Query Router (Role.ROUTER) — classify intent, pick an analytics tool, set entitlement scope.

The router is deterministic-first: keyword + entity heuristics choose the intent
(`lookup|relational|aggregate|hybrid`) and, when possible, a *validated analytics tool* (which is
always preferred over free text-to-Cypher). The Role.ROUTER LLM is also consulted and its class is
recorded on the plan (`llm_intent`) — cassette-ready for full mode — but the deterministic decision
is what executes, so the pipeline stays reproducible offline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..providers.base import Role

ROUTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["intent"],
    "properties": {
        "intent": {
            "type": "string",
            "enum": ["lookup", "relational", "aggregate", "hybrid"],
        },
        "tool": {"type": "string"},
    },
}

ROUTER_SYSTEM = (
    "You are a query router for a fund knowledge graph. Classify the question as one of: "
    "lookup (a single fact about one entity), relational (multi-hop paths/links between entities), "
    "aggregate (counts/weights/concentration across a set), or hybrid (needs both graph structure "
    "and document text). Prefer the graph for anything structural."
)


@dataclass
class Vocab:
    """Graph vocabulary for entity detection: alias(lowercased) → canonical, plus fund names."""

    companies: dict[str, str] = field(default_factory=dict)
    funds: list[str] = field(default_factory=list)


@dataclass
class RouterPlan:
    question: str
    intent: str
    tool: str | None
    tool_args: dict[str, Any]
    entities: list[str]
    fund: str | None
    entitlements: list[str]
    llm_intent: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "tool": self.tool,
            "tool_args": self.tool_args,
            "entities": self.entities,
            "fund": self.fund,
            "entitlements": self.entitlements,
            "llm_intent": self.llm_intent,
        }


def load_graph_vocab(store: Any, firm: str | None = None) -> Vocab:
    """Build the entity vocabulary from the live graph (company names/tickers + fund names).

    When `firm` is set, the FUND list is scoped to that firm's funds (so fund detection / the
    default-fund fallback for fund-scoped tools like concentration_summary can never pick another
    firm's fund). Company aliases stay global on purpose: a question may reference a non-held
    supplier/chokepoint (e.g. for an exposure path), and downstream firm-scoping of evidence and
    analytics already prevents any other-firm data from reaching the answer.
    """
    companies: dict[str, str] = {}
    for r in store.run("MATCH (c:Company) RETURN c.name AS name, c.ticker AS ticker"):
        name = r["name"]
        if not name:
            continue
        companies[name.lower()] = name
        if r.get("ticker"):
            companies[str(r["ticker"]).lower()] = name
    if firm is None:
        fund_rows = store.run("MATCH (f:Fund) RETURN f.name AS name")
    else:
        fund_rows = store.run(
            "MATCH (f:Fund)-[:MANAGED_BY]->(:Company {name: $firm}) RETURN f.name AS name",
            firm=firm,
        )
    funds = [r["name"] for r in fund_rows if r["name"]]
    return Vocab(companies=companies, funds=funds)


def entitlement_scope(entitlements: list[str] | None) -> list[str]:
    """Public is always visible; any extra scopes (e.g. 'internal') are additive."""
    allowed = {"public"} | {e.strip().lower() for e in (entitlements or []) if e.strip()}
    return sorted(allowed)


# Intent keyword patterns. Token-presence based (order-independent) so paraphrases route the same.
# Stems use `\w*` so "concentrated"/"exposure" match (a bare trailing \b would not).
_SUPPLIER = re.compile(r"\bsupplier", re.I)
_SHARED_WORDS = re.compile(r"\b(shar\w*|common|same|overlap|concentrat\w*)\b", re.I)
_CHOKE = re.compile(r"\b(chokepoint|choke point|second[- ]order|hidden|downstream|reachable)\b", re.I)
_BOARD = re.compile(r"\b(board|director\w*|officer\w*|alumni|interlock\w*)\b", re.I)
_COVERAGE = re.compile(r"\b(coverage|disclos\w*|prospectus|485\w*|uncovered)\b", re.I)
_CONC = re.compile(r"\b(concentrat\w*|hhi|herfindahl|diversif\w*|weightings?)\b", re.I)
_PATH = re.compile(r"\b(exposure path|supply.?chain path|dependency path)\b", re.I)
_LOOKUP = re.compile(r"^\s*(who|what|when|where|which|is|does|did|how much|how many)\b", re.I)

# Tools whose fund argument is optional (NULL → across all funds); never dropped for a missing fund.
_OPTIONAL_FUND_TOOLS = frozenset(
    {"shared_suppliers", "second_order_chokepoints", "coverage_gap"}
)


def _default_fund(vocab: Vocab) -> str | None:
    """Primary fund for fund-required tools when none is named (the non-'focused' growth fund)."""
    for f in vocab.funds:
        if "focused" not in f.lower():
            return f
    return vocab.funds[0] if vocab.funds else None


class Router:
    """Route a question to an analytics tool or the graph/vector pipeline."""

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

    # -- entity detection -----------------------------------------------------------------

    def _detect_fund(self, q: str, vocab: Vocab) -> str | None:
        ql = q.lower()
        if not vocab.funds:
            return None

        def toks(name: str) -> set[str]:
            return {t for t in re.split(r"\W+", name.lower()) if len(t) > 2}

        # Distinctive tokens = those NOT shared by every fund (e.g. "focused" vs plain "growth").
        common = set.intersection(*[toks(f) for f in vocab.funds])
        best_name: str | None = None
        best_score = (0, 0, 0)
        for name in vocab.funds:
            t = toks(name)
            dhits = sum(1 for x in (t - common) if x in ql)
            chits = sum(1 for x in (t & common) if x in ql)
            if dhits == 0 and chits == 0:
                continue
            # Prefer distinctive matches; on ties prefer the more specific (fewer-token) name.
            score = (dhits, chits, -len(t))
            if best_name is None or score > best_score:
                best_name, best_score = name, score
        return best_name

    def _detect_companies(self, q: str, vocab: Vocab) -> list[str]:
        ql = q.lower()
        # Match longest aliases first (so "advanced micro devices" wins over a bare token), but
        # return canonical names ordered by first occurrence in the question — direction matters
        # for tools like exposure_path(a, b).
        pos: dict[str, int] = {}
        for alias in sorted(vocab.companies, key=len, reverse=True):
            if len(alias) < 3:
                continue
            m = re.search(r"\b" + re.escape(alias) + r"\b", ql)
            if m:
                canon = vocab.companies[alias]
                pos.setdefault(canon, m.start())
        return sorted(pos, key=lambda c: pos[c])

    def llm_call(self, question: str) -> dict[str, Any]:
        return {
            "role": Role.ROUTER,
            "schema": ROUTER_SCHEMA,
            "system": ROUTER_SYSTEM,
            "messages": [{"role": "user", "content": question}],
        }

    def _llm_intent(self, question: str) -> str | None:
        try:
            result = self.provider.complete_structured(**self.llm_call(question))
            return (result.data or {}).get("intent")
        except Exception:  # noqa: BLE001 - routing must not fail on provider hiccups
            return None

    # -- routing --------------------------------------------------------------------------

    def route(
        self,
        question: str,
        *,
        entitlements: list[str] | None = None,
        vocab: Vocab | None = None,
    ) -> RouterPlan:
        vocab = vocab or Vocab()
        scope = entitlement_scope(entitlements)
        fund = self._detect_fund(question, vocab)
        companies = self._detect_companies(question, vocab)

        intent = "hybrid"
        tool: str | None = None
        tool_args: dict[str, Any] = {}

        if _SUPPLIER.search(question) and _SHARED_WORDS.search(question):
            intent, tool = "relational", "shared_suppliers"
            tool_args = {"fund": fund}
        elif _CHOKE.search(question):
            intent, tool = "relational", "second_order_chokepoints"
            tool_args = {"fund": fund}
        elif _COVERAGE.search(question):
            intent, tool = "relational", "coverage_gap"
            tool_args = {"fund": fund}
        elif _BOARD.search(question):
            intent, tool = "relational", "board_and_alumni_links"
            tool_args = {"company": companies[0] if companies else None}
        elif _CONC.search(question):
            intent, tool = "aggregate", "concentration_summary"
            tool_args = {"fund": fund if fund is not None else _default_fund(vocab)}
        elif _PATH.search(question) and len(companies) >= 2:
            intent, tool = "relational", "exposure_path"
            tool_args = {"a": companies[0], "b": companies[1]}
        elif _LOOKUP.search(question):
            intent = "lookup"

        # Drop a tool only if a REQUIRED arg is missing. Fund-optional tools keep running with
        # fund=None (across all funds); board/exposure need their entities.
        if tool is not None and tool not in _OPTIONAL_FUND_TOOLS:
            if any(v is None for v in tool_args.values()):
                tool = None
                if intent == "aggregate":
                    intent = "hybrid"

        # Deterministic pre-router: when a validated tool matched we answer WITHOUT any LLM call
        # (this is what keeps the hero query live under zero credits). Only consult the Role.ROUTER
        # LLM for genuinely ambiguous questions, and even then it is best-effort.
        llm_intent = None if tool is not None else self._llm_intent(question)

        return RouterPlan(
            question=question,
            intent=intent,
            tool=tool,
            tool_args=tool_args,
            entities=companies,
            fund=fund,
            entitlements=scope,
            llm_intent=llm_intent,
        )
