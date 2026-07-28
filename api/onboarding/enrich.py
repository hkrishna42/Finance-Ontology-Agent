"""Opt-in semantic enrichment — layer the LLM extraction onto an onboarded firm's structural graph.

Phase 2 onboarding gives a firm a *structural* graph (Fund + weighted HOLDS + MANAGED_BY, written
deterministically from N-PORT). That is enough to see *what* a firm holds, but not *why* those
holdings are risky, nor what risks the funds themselves disclose. `enrich_firm(...)` closes that gap
by REUSING the ingest pipeline over two filing sources:

  1. **Holdings' 10-K Item 1A** — pick the firm's top-`top` holdings by descending HOLDS weight; for
     each, pull its 10-K Item 1A (Risk Factors) from EDGAR — keyed on the holding's ticker when it has
     one, else on the CIK resolved from its name (`cik_from_name`; N-PORT identifies holdings by name +
     CUSIP/LEI and rarely a ticker) — and run the full `ingest_document` pipeline (chunk → extract(LLM)
     → resolve → steward → write), forwarding its SSE events re-tagged with the holding.
  2. **Funds' 485BPOS principal risks** — for each distinct fund the firm manages, best-effort pull
     the registrant's latest 485BPOS (statutory prospectus), slice it to its bounded Principal-Risks
     region, and ingest it the same way, forwarding events re-tagged with the fund.

Both paths write the `Chunk`/`Document`, `RiskFactor`, `EXPOSED_TO`, `SUPPLIES_TO`, and `MENTIONS`
edges the Risk-Lens / Change-Impact analytics (and Analyst Chat / Documents) need, then a terminal
`job.completed` summarizes how many holdings + funds were enriched and how many distinct risk
factors landed.

Bounded spend: a hard cap of `MAX_FILINGS` (8) filings PER RUN — at most `MAX_HOLDINGS` (5) holding
10-Ks, with the remaining budget spent on fund prospectuses. Every fetched filing is anchored on its
risk section and truncated to `FILING_MAX_CHARS` BEFORE ingest (a real 10-K "Item 1A" or a whole
485BPOS can be hundreds of chunks otherwise), so its chunk count — and thus the one extraction call
`ingest_document` makes per chunk — stays bounded no matter how large EDGAR returns it. Only the
extraction step spends tokens; the embedder, resolver, and steward run on the deterministic offline
seams. Best-effort per filing — a fetch or extraction failure emits a non-fatal `error` event and
moves on.

Deterministic provenance: after each filing ingests, a pass INDEPENDENT of the LLM (so it works in
BOTH modes) stamps the `Document` with the firm and MENTIONS-links every chunk of the filing to its
known subject — the held `Company` for a 10-K, the `Fund` for a prospectus. Because we fetched the
filing by that subject (the holding's ticker / the fund's id) the subject is known even when
`stub`/FakeProvider extracts nothing; this is what makes the enriched chunks discoverable by the
firm-scoped `:Chunk`-[:MENTIONS]->subject queries behind Analyst Chat and the Documents view (without
it, stub-mode chunks are orphaned and the firm scope stays empty).

A fund's 485BPOS has no series-scoped path through `source_from_edgar` (it exposes `ticker` + `form`
or `accession` only), so we reuse the `ticker` + `form` path keyed on the registrant identifier: a
share-class ticker if the Fund node carries one, else the registrant `cik` (edgartools' `Company()`
accepts a CIK). That yields the registrant's combined statutory prospectus — for a multi-series
trust it is shared across siblings, so funds are de-duplicated on that key. When neither a ticker nor
a cik is present the prospectus is skipped gracefully; it never blocks the 10-K path.

Both run modes: `stub` → `FakeProvider` (offline, canned; adds 0 risk factors but exercises the
whole path, writing real `Chunk`s), `full` → real Claude. Every seam (`store`, `provider`, queue
`conn`) is injectable so the whole thing runs offline in tests with a fake store; `source_from_edgar`
is called by attribute so tests can stub it.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from typing import Any
from uuid import uuid4

from ..contracts.events import EventType, SSEEvent, make_event
from ..ingest import sources
from ..ingest.pipeline import ingest_document

# The total number of filings a single enrichment run may ever pull (hard spend cap): holding
# 10-Ks + fund prospectuses combined.
MAX_FILINGS = 8
# The hard cap on the holding-10-K slice; the remaining budget (MAX_FILINGS - holdings) is available
# to fund prospectuses. Keeps the previous "top holdings hard-capped at 5" guarantee intact.
MAX_HOLDINGS = 5
DEFAULT_TOP = 3
# Per-filing text bound. Every fetched filing (a holding's 10-K Item-1A slice, or a fund's 485BPOS
# statutory prospectus) is anchored on its risk section and truncated to this many chars BEFORE
# ingest, so its chunk count (== the extraction call `ingest_document` makes per chunk) stays bounded
# no matter how large EDGAR returns a filing — a real "Item 1A" can be hundreds of chunks otherwise.
FILING_MAX_CHARS = 24000

# Inner pipeline events forwarded (re-tagged) to the caller. The per-document lifecycle events
# (job.started / job.completed) and inner error events are intentionally NOT forwarded: this stream
# has exactly one leading job.started and one terminal job.completed of its own, and per-filing
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

# The firm's top holdings by descending HOLDS weight — a ticker is NOT required (N-PORT rarely carries
# one; a tickerless holding is fetched by resolving its name to a CIK downstream). The junk 'N/A' name
# some N-PORT derivative rows carry is filtered out here so it never wastes a filing slot. Grouping is
# implicit on the non-aggregated return keys (co.name, co.ticker).
_TOP_HOLDINGS = (
    "MATCH (fund:Fund)-[:MANAGED_BY]->(:Company {name: $firm}) "
    "MATCH (fund)-[h:HOLDS]->(co:Company) "
    "WHERE co.name IS NOT NULL AND co.name <> 'N/A' "
    "RETURN co.name AS name, co.ticker AS ticker, sum(h.weight_pct) AS weight "
    "ORDER BY weight DESC LIMIT $top"
)

# The distinct funds the firm manages (for prospectus enrichment). `series_id`/`cik` come from the
# N-PORT-written Fund node; `ticker` is usually absent (share-class tickers are not persisted), so
# the registrant `cik` is the practical 485BPOS lookup key.
_FIRM_FUNDS = (
    "MATCH (fund:Fund)-[:MANAGED_BY]->(:Company {name: $firm}) "
    "RETURN fund.name AS name, fund.series_id AS series_id, "
    "fund.cik AS cik, fund.ticker AS ticker "
    "ORDER BY fund.name"
)

# Section headings each filing's excerpt anchors on (case-insensitive), first hit wins; falls back
# to the head of the document when none is found.
_RISK_FACTOR_HEADINGS = ("item 1a", "risk factors")
_PRINCIPAL_RISKS_HEADINGS = ("principal investment risks", "principal risks", "principal risk")


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
    """Coerce `top` to an int in [0, MAX_HOLDINGS] (the hard holding-10-K cap)."""
    try:
        value = int(top)
    except (TypeError, ValueError):
        value = DEFAULT_TOP
    return max(0, min(value, MAX_HOLDINGS))


def _bounded_excerpt(
    text: str, headings: tuple[str, ...], *, max_chars: int = FILING_MAX_CHARS
) -> str:
    """Anchor `text` on the first of `headings` that appears, returning at most `max_chars` from there.

    Falls back to the leading `max_chars` when no heading is found, so a filing whose section we
    can't locate is still bounded (never ingested whole) — this is what keeps each filing's chunk
    count (== extraction calls) bounded regardless of how large EDGAR returns it. Substring-based, so
    the extraction spans stay groundable against the sliced chunk text.
    """
    body = text or ""
    low = body.lower()
    start = 0
    for heading in headings:
        idx = low.find(heading)
        if idx != -1:
            start = idx
            break
    return body[start : start + max_chars]


def _source_for_holding(name: str, ticker: Any, *, settings: Any) -> Any:
    """Best-effort 10-K `SourceDoc` for a held company, keyed on whatever identifier resolves.

    N-PORT identifies holdings by name (+ CUSIP/LEI) and rarely a ticker, so: when the holding carries
    a ticker, fetch its 10-K by ticker; otherwise resolve the company NAME to an EDGAR CIK
    (`sources.cik_from_name`) and fetch by CIK (edgartools' `Company()` accepts a CIK). Raises
    `IngestSourceError` when neither identifier yields a filing, so the caller's best-effort seam skips
    the holding. `source_from_edgar` / `cik_from_name` are called by attribute so tests can stub them.
    """
    tkr = str(ticker or "").strip()
    if tkr:
        return sources.source_from_edgar(ticker=tkr, form="10-K", sections=["1A"])
    cik = sources.cik_from_name(name, user_agent=getattr(settings, "edgar_user_agent", None))
    if cik:
        return sources.source_from_edgar(ticker=str(cik), form="10-K", sections=["1A"])
    raise sources.IngestSourceError(f"no ticker and no resolvable EDGAR CIK for holding {name!r}")


def enrich_firm(
    firm_name: str,
    *,
    top: int = DEFAULT_TOP,
    prospectuses: bool = True,
    settings: Any = None,
    store: Any = None,
    conn: Any = None,
    provider: Any = None,
) -> Iterator[SSEEvent]:
    """Semantically enrich `firm_name`'s top holdings + fund prospectuses, yielding SSE events.

    Streams (in contract order): `job.started` → per holding, the forwarded ingest events (re-tagged
    with `holding`/`ticker`) → per fund, the forwarded ingest events (re-tagged with `fund`/`form`) →
    a terminal `job.completed{firm, enriched, funds_enriched, risk_factors_added, ...}`.

    `top` bounds the holding 10-Ks (clamped to `MAX_HOLDINGS`); `prospectuses` toggles the 485BPOS
    stage. The two stages share a hard `MAX_FILINGS` budget. `store` defaults to a real `Neo4jStore`
    (owned + closed here); inject a fake store for offline tests. `provider` defaults to
    `get_llm_provider(settings)` (the ONLY LLM seam — `stub` gives the offline `FakeProvider`).
    `conn`, when supplied, is forwarded to `ingest_document` as the SQLite resolution queue; when
    omitted the queue is disabled (nothing else here touches SQLite).

    Best-effort: a per-filing fetch/extract failure emits a non-fatal `error` event and continues;
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

    def run_ingest(source: Any, *, tag: dict[str, Any], where: str) -> Iterator[SSEEvent]:
        """Run `ingest_document` for one filing, forwarding its events re-tagged with `tag`.

        A generator whose return value (captured via `yield from`) is True on success, False after a
        non-fatal `error` event — the best-effort per-filing seam shared by both stages.
        """
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
                    yield emit(ev.event, **{**ev.data, **tag})
            return True
        except Exception as exc:  # noqa: BLE001 - best-effort: warn on this filing, keep going
            yield emit(
                EventType.ERROR, message=str(exc)[:400], where=where, fatal=False, **tag
            )
            return False

    def link_subject(doc_id: str, *, label: str, name: str) -> None:
        """Attach provenance for one enriched filing DETERMINISTICALLY — no LLM, so it works in BOTH
        modes. Stamps the `Document` with the firm and MENTIONS-links every chunk of the filing to
        its known subject node (`label`/`name`). We fetched the filing BY that subject (a holding's
        ticker / a fund's id), so the subject is known even when `stub`/FakeProvider extracts nothing;
        without this, the chunks would be orphaned and invisible to the firm-scoped
        `:Chunk`-[:MENTIONS]->subject queries that power Analyst Chat and the Documents view.
        """
        counting.run(
            "MATCH (d:Document {doc_id: $doc_id}) SET d.firm = $firm",
            doc_id=doc_id,
            firm=firm_name,
        )
        counting.run(
            f"MATCH (c:Chunk {{doc_id: $doc_id}}) MATCH (x:{label} {{name: $name}}) "
            "MERGE (c)-[:MENTIONS]->(x)",
            doc_id=doc_id,
            name=name,
        )

    enriched = 0
    failed = 0
    funds_enriched = 0
    funds_failed = 0

    try:
        # 1. resolve the firm's top-N holdings that carry a ticker (descending HOLDS weight) --------
        holdings: list[dict[str, Any]] = []
        if top > 0:
            rows = counting.run(_TOP_HOLDINGS, firm=firm_name, top=top)
            for r in list(rows)[:top]:  # slice too: belt-and-suspenders on the hard cap
                name = str(r.get("name") or "").strip()
                if not name or name.upper() == "N/A":  # skip unnamed / derivative junk rows
                    continue
                # ticker is optional now: a tickerless holding is fetched by its resolved CIK
                holdings.append({"name": name, "ticker": r.get("ticker"), "weight": r.get("weight")})

        # 2. resolve the firm's funds for prospectus enrichment, within the remaining budget --------
        prospectus_budget = max(0, MAX_FILINGS - len(holdings))
        funds: list[dict[str, Any]] = []
        if prospectuses and prospectus_budget > 0:
            seen_keys: set[str] = set()
            frows = counting.run(_FIRM_FUNDS, firm=firm_name)
            for r in list(frows):
                fund_name = r.get("name")
                key = str(r.get("ticker") or r.get("cik") or "").strip()
                if not fund_name or not key or key in seen_keys:
                    continue
                seen_keys.add(key)
                funds.append({"name": fund_name, "series_id": r.get("series_id"), "key": key})
                if len(funds) >= prospectus_budget:
                    break

        yield emit(
            EventType.JOB_STARTED,
            firm=firm_name,
            top=top,
            holdings=[dict(h) for h in holdings],
            funds=[{"name": f["name"], "series_id": f["series_id"]} for f in funds],
        )

        # 3. per holding — pull 10-K Item 1A → run the ingest pipeline → forward its events ----------
        for h in holdings:
            name, ticker = h["name"], h["ticker"]
            try:
                source = _source_for_holding(name, ticker, settings=settings)
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

            doc = replace(source, text=_bounded_excerpt(source.text, _RISK_FACTOR_HEADINGS))
            ok = yield from run_ingest(
                doc, tag={"holding": name, "ticker": ticker}, where="enrich_firm:ingest"
            )
            if ok:
                enriched += 1
                link_subject(doc.doc_id, label="Company", name=name)  # chunks MENTION the holding
            else:
                failed += 1

        # 4. per fund — pull the 485BPOS, slice to Principal Risks → ingest → forward its events -----
        for f in funds:
            fund_name, key = f["name"], f["key"]
            try:
                psource = sources.source_from_edgar(ticker=key, form="485BPOS")
            except Exception as exc:  # noqa: BLE001 - best-effort: warn on this fund, keep going
                funds_failed += 1
                yield emit(
                    EventType.ERROR,
                    fund=fund_name,
                    series_id=f["series_id"],
                    message=str(exc)[:400],
                    where="enrich_firm:prospectus_source",
                    fatal=False,
                )
                continue

            bounded = replace(psource, text=_bounded_excerpt(psource.text, _PRINCIPAL_RISKS_HEADINGS))
            ok = yield from run_ingest(
                bounded,
                tag={"fund": fund_name, "series_id": f["series_id"], "form": "485BPOS"},
                where="enrich_firm:prospectus",
            )
            if ok:
                funds_enriched += 1
                link_subject(bounded.doc_id, label="Fund", name=fund_name)  # chunks MENTION the fund
            else:
                funds_failed += 1

        # 5. terminal summary ----------------------------------------------------------------------
        yield emit(
            EventType.JOB_COMPLETED,
            ok=True,
            firm=firm_name,
            enriched=enriched,
            failed=failed,
            holdings_total=len(holdings),
            funds_enriched=funds_enriched,
            funds_failed=funds_failed,
            funds_total=len(funds),
            risk_factors_added=len(counting.risk_titles),
        )
    finally:
        if own_store:
            store.close()
