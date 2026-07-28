"""Opt-in semantic enrichment — layer the LLM extraction onto an onboarded firm's structural graph.

Phase 2 onboarding gives a firm a *structural* graph (Fund + weighted HOLDS + MANAGED_BY, written
deterministically from N-PORT). That is enough to see *what* a firm holds, but not *why* those
holdings are risky. `enrich_firm(...)` closes that gap by REUSING the ingest pipeline:

  1. pick the firm's top-`top` holdings that carry a ticker, by descending HOLDS weight;
  2. for each, pull its 10-K Item 1A (Risk Factors) from EDGAR (`source_from_edgar`) and run the
     full `ingest_document` pipeline (chunk → extract(LLM) → resolve → steward → write), forwarding
     its SSE events re-tagged with the holding — this is what writes the `RiskFactor`,
     `EXPOSED_TO`, `SUPPLIES_TO`, and `MENTIONS` edges the Risk-Lens / Change-Impact analytics need;
  3. emit a terminal `job.completed` summarizing how many holdings were enriched and how many
     distinct risk factors landed.

Bounded spend: at most `top` (default 3, hard-capped at 5) filings, and `ingest_document` bounds
each filing to one extraction call per chunk. Only the extraction step spends tokens; the embedder,
resolver, and steward run on the deterministic offline seams. Best-effort per holding — a fetch or
extraction failure emits a non-fatal `error` event and moves on.

Both run modes: `stub` → `FakeProvider` (offline, canned; adds 0 risk factors but exercises the
whole path), `full` → real Claude. Every seam (`store`, `provider`, queue `conn`) is injectable so
the whole thing runs offline in tests with a fake store; `source_from_edgar` is called by attribute
so tests can stub it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

from ..contracts.events import EventType, SSEEvent, make_event
from ..ingest import sources
from ..ingest.pipeline import ingest_document

# The maximum number of filings a single enrichment run may ever pull (hard spend cap).
MAX_FILINGS = 5
DEFAULT_TOP = 3

# Inner pipeline events forwarded (re-tagged) to the caller. The per-document lifecycle events
# (job.started / job.completed) and inner error events are intentionally NOT forwarded: this stream
# has exactly one leading job.started and one terminal job.completed of its own, and per-holding
# failures are surfaced as a single non-fatal error event by the best-effort handler below.
_FORWARDED = frozenset(
    {
        EventType.CLASSIFIED,
        EventType.PARSED,
        EventType.CHUNKED,
        EventType.EXTRACTED,
        EventType.RESOLVED,
        EventType.WRITTEN,
    }
)

# The firm's top holdings that carry a ticker, aggregated by descending HOLDS weight. Grouping is
# implicit on the non-aggregated return keys (co.name, co.ticker).
_TOP_HOLDINGS = (
    "MATCH (fund:Fund)-[:MANAGED_BY]->(:Company {name: $firm}) "
    "MATCH (fund)-[h:HOLDS]->(co:Company) "
    "WHERE co.ticker IS NOT NULL "
    "RETURN co.name AS name, co.ticker AS ticker, sum(h.weight_pct) AS weight "
    "ORDER BY weight DESC LIMIT $top"
)


class _RiskCountingStore:
    """Transparent store proxy that tallies the distinct `RiskFactor` titles written through it.

    Wraps the real (or fake) store: `run` is forwarded verbatim (so query results still flow), while
    every `MERGE (n:RiskFactor {title: $kv}) ...` write contributes its title to `risk_titles`. Any
    other attribute access (`close`, ...) delegates to the wrapped store.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.risk_titles: set[str] = set()

    def run(self, query: str, **params: Any) -> Any:
        if "(n:RiskFactor" in query and params.get("kv"):
            self.risk_titles.add(params["kv"])
        return self._inner.run(query, **params)

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - simple delegation
        return getattr(self._inner, name)


def _clamp_top(top: Any) -> int:
    """Coerce `top` to an int in [0, MAX_FILINGS] (the hard filing/spend cap)."""
    try:
        value = int(top)
    except (TypeError, ValueError):
        value = DEFAULT_TOP
    return max(0, min(value, MAX_FILINGS))


def enrich_firm(
    firm_name: str,
    *,
    top: int = DEFAULT_TOP,
    settings: Any = None,
    store: Any = None,
    conn: Any = None,
    provider: Any = None,
) -> Iterator[SSEEvent]:
    """Semantically enrich `firm_name`'s top holdings, yielding SSE events in contract order.

    Streams: `job.started` → per holding, the forwarded ingest events (re-tagged with
    `holding`/`ticker`) → a terminal `job.completed{firm, enriched, risk_factors_added, ...}`.

    `store` defaults to a real `Neo4jStore` (owned + closed here); inject a fake store for offline
    tests. `provider` defaults to `get_llm_provider(settings)` (the ONLY LLM seam — `stub` gives the
    offline `FakeProvider`). `conn`, when supplied, is forwarded to `ingest_document` as the SQLite
    resolution queue; when omitted the queue is disabled (nothing else here touches SQLite).

    Best-effort: a per-holding fetch/extract failure emits a non-fatal `error` event and continues;
    the run always ends with `job.completed`.
    """
    from ..config import get_settings

    settings = settings or get_settings()
    firm_name = str(firm_name or "").strip()
    if not firm_name:
        raise ValueError("enrich_firm: firm_name is required")

    top = _clamp_top(top)
    job_id = f"enrich-{uuid4().hex[:8]}"

    if provider is None:
        from ..providers.factory import get_llm_provider

        provider = get_llm_provider(settings)

    own_store = store is None
    if own_store:
        from ..stores.neo4j import Neo4jStore

        store = Neo4jStore(settings)
    counting = _RiskCountingStore(store)

    seq = 0

    def emit(event_type: EventType, **data: Any) -> SSEEvent:
        nonlocal seq
        ev = make_event(event_type, job_id, seq=seq, **data)
        seq += 1
        return ev

    enriched = 0
    failed = 0

    try:
        # 1. resolve the firm's top-N holdings that carry a ticker (descending HOLDS weight) --------
        holdings: list[dict[str, Any]] = []
        if top > 0:
            rows = counting.run(_TOP_HOLDINGS, firm=firm_name, top=top)
            for r in list(rows)[:top]:  # slice too: belt-and-suspenders on the hard cap
                ticker = r.get("ticker")
                name = r.get("name")
                if not ticker or not name:
                    continue
                holdings.append({"name": name, "ticker": ticker, "weight": r.get("weight")})

        yield emit(
            EventType.JOB_STARTED,
            firm=firm_name,
            top=top,
            holdings=[dict(h) for h in holdings],
        )

        # 2. per holding — pull 10-K Item 1A → run the ingest pipeline → forward its events ----------
        for h in holdings:
            name, ticker = h["name"], h["ticker"]
            try:
                source = sources.source_from_edgar(ticker=ticker, form="10-K", sections=["1A"])
            except Exception as exc:  # noqa: BLE001 - best-effort: warn on this holding, keep going
                failed += 1
                yield emit(
                    EventType.ERROR,
                    holding=name,
                    ticker=ticker,
                    message=str(exc)[:400],
                    where="enrich_firm:source",
                    fatal=False,
                )
                continue

            try:
                for ev in ingest_document(
                    source,
                    settings=settings,
                    provider=provider,
                    store=counting,
                    queue_conn=conn,
                    queue=conn is not None,
                ):
                    if ev.event in _FORWARDED:
                        yield emit(ev.event, **{**ev.data, "holding": name, "ticker": ticker})
                enriched += 1
            except Exception as exc:  # noqa: BLE001 - best-effort: warn on this holding, keep going
                failed += 1
                yield emit(
                    EventType.ERROR,
                    holding=name,
                    ticker=ticker,
                    message=str(exc)[:400],
                    where="enrich_firm:ingest",
                    fatal=False,
                )
                continue

        # 3. terminal summary ----------------------------------------------------------------------
        yield emit(
            EventType.JOB_COMPLETED,
            ok=True,
            firm=firm_name,
            enriched=enriched,
            failed=failed,
            holdings_total=len(holdings),
            risk_factors_added=len(counting.risk_titles),
        )
    finally:
        if own_store:
            store.close()
