"""Offline enrichment-orchestrator tests (fake store / canned extractor / stubbed EDGAR).

Proves the Phase-3 opt-in semantic-enrichment surface without a database, the network, or a key:
  * `enrich_firm(...)` picks the firm's top holdings-with-tickers and, per holding, runs the reused
    `ingest_document` pipeline, forwarding its events re-tagged with `holding`/`ticker`;
  * the reused pipeline writes the `RiskFactor` / `EXPOSED_TO` / `SUPPLIES_TO` edges (the semantic
    layer the analytics tasks consume), and the terminal `job.completed` summarizes the run;
  * a per-holding fetch failure is best-effort: a non-fatal `error` event, then the run continues;
  * the `POST /firms/{firm_id}/enrich` route resolves the firm name and bridges the SSE stream.

`source_from_edgar` is stubbed (`monkeypatch`) to return a canned 10-K Item-1A snippet, so no EDGAR
call happens; extraction runs on the offline `FakeProvider` (stub) or a `CannedExtractor` fixture,
so no Anthropic key is needed; the store is a fake, so no Neo4j.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import config
from api.contracts.events import EventType, make_event
from api.firms import store as fs
from api.ingest import sources
from api.ingest.sources import IngestSourceError, SourceDoc
from api.onboarding import routes as onboarding_routes
from api.onboarding.enrich import FILING_MAX_CHARS, MAX_FILINGS, MAX_HOLDINGS, enrich_firm
from api.onboarding.routes import router as onboarding_router
from api.providers.base import StructuredResult, Usage
from api.providers.fake import FakeProvider
from api.stores.sqlite import connect, init_db

FIRM_NAME = "Test Enrich Advisers LLC"
FIRM_ID = fs.slugify(FIRM_NAME)

# Two holdings-with-tickers, the shape `_TOP_HOLDINGS` returns (dicts, like Neo4jStore.run rows).
HOLDINGS = [
    {"name": "Nvidia Corporation", "ticker": "NVDA", "weight": 12.5},
    {"name": "Microsoft Corporation", "ticker": "MSFT", "weight": 8.0},
]

# Two funds-the-firm-manages, the shape `_FIRM_FUNDS` returns. Fund nodes carry no share-class
# ticker, so the registrant `cik` is the 485BPOS lookup key.
FUNDS = [
    {"name": "Demo Growth Fund", "series_id": "S000090001", "cik": "0009000001", "ticker": None},
    {"name": "Demo Focused Fund", "series_id": "S000090002", "cik": "0009000002", "ticker": None},
]

# A tiny 10-K Item-1A snippet naming a supplier + a risk. The extraction spans below are verbatim
# substrings of this text so the real grounding gate keeps them.
SNIPPET = (
    "Nvidia Corporation depends on a limited number of suppliers and relies on Taiwan "
    "Semiconductor Manufacturing Company for the fabrication of its processors, which "
    "exposes Nvidia Corporation to supply chain concentration risk."
)
_SPAN_SUPPLY = (
    "relies on Taiwan Semiconductor Manufacturing Company for the fabrication of its processors"
)
_SPAN_RISK = "exposes Nvidia Corporation to supply chain concentration risk"

_EXTRACTION = {
    "entities": [
        {"label": "Company", "name": "Nvidia Corporation",
         "span": "Nvidia Corporation depends on a limited number of suppliers",
         "confidence": 0.96, "attributes": {"ticker": "NVDA"}},
        {"label": "Company", "name": "Taiwan Semiconductor Manufacturing Company",
         "span": _SPAN_SUPPLY, "confidence": 0.93},
        {"label": "RiskFactor", "name": "Supply chain concentration",
         "category": "supply_chain", "span": _SPAN_RISK, "confidence": 0.9},
    ],
    "relations": [
        {"type": "SUPPLIES_TO", "subject": "Taiwan Semiconductor Manufacturing Company",
         "object": "Nvidia Corporation", "qualifiers": {"component": "processor fabrication"},
         "span": _SPAN_SUPPLY, "confidence": 0.92},
        {"type": "EXPOSED_TO", "subject": "Nvidia Corporation",
         "object": "Supply chain concentration",
         "qualifiers": {"severity_language": "exposes"},
         "span": _SPAN_RISK, "confidence": 0.9},
    ],
}

# A tiny 485BPOS prospectus snippet: a Principal-Risks heading (what `_bounded_excerpt` anchors on)
# followed by the same risk prose, so `CannedExtractor(_EXTRACTION)`'s spans stay groundable
# against it.
PROSPECTUS = (
    "Principal Investment Risks. As a shareholder of the Fund you could lose money. " + SNIPPET
)


# --- offline seams --------------------------------------------------------------------------


class FakeStore:
    """Records Cypher; returns canned holdings for the top-holdings query, canned funds for the
    firm-funds query, else []."""

    def __init__(
        self, holdings: list[dict] | None = None, funds: list[dict] | None = None
    ) -> None:
        self.holdings = holdings if holdings is not None else HOLDINGS
        self.funds = funds if funds is not None else []
        self.calls: list[tuple[str, dict]] = []

    def run(self, query: str, **params) -> list:
        self.calls.append((query, params))
        if "sum(h.weight_pct)" in query:
            return list(self.holdings)
        if "fund.series_id" in query:  # the _FIRM_FUNDS query
            return list(self.funds)
        return []

    def close(self) -> None:  # pragma: no cover - lifecycle no-op
        pass

    def queries(self, needle: str) -> list[tuple[str, dict]]:
        return [(q, p) for q, p in self.calls if needle in q]


class CannedExtractor:
    """An offline `LLMProvider` that returns a fixed extraction for the EXTRACTION role (no key)."""

    def __init__(self, data: dict) -> None:
        self.data = data
        self.calls = 0

    def complete_structured(self, *, role, schema, system, messages, max_tokens=2048,
                            cache_system=False) -> StructuredResult:
        self.calls += 1
        return StructuredResult(
            data=self.data, raw="{}", model="fake-canned",
            usage=Usage(input_tokens=100, output_tokens=30), stop_reason="end_turn",
        )

    def complete(self, *, role, system, messages, max_tokens=1024):  # pragma: no cover
        raise NotImplementedError


def _make_source(ticker: str) -> SourceDoc:
    return SourceDoc(
        doc_id=f"{ticker.lower()}_10k_1a",
        doc_type="10-K",
        sensitivity="public",
        text=SNIPPET,
        title=f"{ticker} 10-K Item 1A",
        source="edgar",
    )


@pytest.fixture()
def stub_edgar(monkeypatch):
    """Stub `source_from_edgar` to return the canned snippet; records the (ticker, form, sections)."""
    calls: list[dict] = []

    def fake_edgar(*, ticker=None, form=None, sections=None, **kw) -> SourceDoc:
        calls.append({"ticker": ticker, "form": form, "sections": sections})
        return _make_source(ticker)

    monkeypatch.setattr(sources, "source_from_edgar", fake_edgar)
    return calls


# --- pipeline: SSE stream + forwarded events + reused writes ---------------------------------


def test_enrich_streams_per_holding_events_and_writes_semantic_edges(stub_edgar):
    store = FakeStore()
    events = list(
        enrich_firm(FIRM_NAME, top=3, store=store, provider=CannedExtractor(_EXTRACTION))
    )

    # leading job.started announces the firm + the two picked holdings-with-tickers
    assert events[0].event == EventType.JOB_STARTED
    assert events[0].data["firm"] == FIRM_NAME
    assert [h["ticker"] for h in events[0].data["holdings"]] == ["NVDA", "MSFT"]

    # seq is monotonic 0..n and the whole stream shares one enrich job_id
    assert [e.seq for e in events] == list(range(len(events)))
    assert len({e.job_id for e in events}) == 1

    # ingest_document ran per holding: EDGAR was asked for each ticker's 10-K Item 1A
    assert [c["ticker"] for c in stub_edgar] == ["NVDA", "MSFT"]
    assert all(c["form"] == "10-K" and c["sections"] == ["1A"] for c in stub_edgar)

    # the reused pipeline's events are forwarded, each re-tagged with its holding
    written = [e for e in events if e.event == EventType.WRITTEN]
    assert {e.data["ticker"] for e in written} == {"NVDA", "MSFT"}
    assert all("holding" in e.data and e.data["nodes"] >= 1 for e in written)
    extracted = [e for e in events if e.event == EventType.EXTRACTED]
    assert {e.data["ticker"] for e in extracted} == {"NVDA", "MSFT"}
    # no inner per-doc lifecycle leaks into the stream (exactly one job.started/completed, ours)
    assert sum(e.event == EventType.JOB_STARTED for e in events) == 1
    assert sum(e.event == EventType.JOB_COMPLETED for e in events) == 1

    # the semantic layer landed in the store: RiskFactor node + EXPOSED_TO + SUPPLIES_TO edges
    assert store.queries("(n:RiskFactor"), "no RiskFactor node written"
    assert store.queries("SUPPLIES_TO"), "no SUPPLIES_TO edge written"
    assert store.queries("EXPOSED_TO"), "no EXPOSED_TO edge written"

    # terminal completion summarizes: both holdings enriched, one DISTINCT risk factor added
    done = events[-1]
    assert done.event == EventType.JOB_COMPLETED
    assert done.data["ok"] is True
    assert done.data["firm"] == FIRM_NAME
    assert done.data["enriched"] == 2
    assert done.data["failed"] == 0
    assert done.data["holdings_total"] == 2
    assert done.data["risk_factors_added"] == 1  # both filings surface the same title → deduped


def test_enrich_runs_offline_in_stub_mode_with_fakeprovider(stub_edgar):
    """The literal stub path: FakeProvider extracts nothing, but the whole pipeline still runs."""
    store = FakeStore()
    events = list(enrich_firm(FIRM_NAME, top=3, store=store, provider=FakeProvider()))

    assert events[0].event == EventType.JOB_STARTED
    assert events[-1].event == EventType.JOB_COMPLETED
    # ingest_document ran for both holdings (each produced a forwarded WRITTEN event)
    assert {e.data["ticker"] for e in events if e.event == EventType.WRITTEN} == {"NVDA", "MSFT"}
    done = events[-1].data
    assert done["enriched"] == 2 and done["holdings_total"] == 2 and done["failed"] == 0
    assert done["risk_factors_added"] == 0  # stub extraction is empty → no risk factors


def test_enrich_is_best_effort_on_holding_failure(monkeypatch):
    """A per-holding EDGAR failure emits a non-fatal error event; the run continues + completes."""
    store = FakeStore()

    def flaky_edgar(*, ticker=None, form=None, sections=None, **kw) -> SourceDoc:
        if ticker == "MSFT":
            raise IngestSourceError("no 10-K filing found for MSFT")
        return _make_source(ticker)

    monkeypatch.setattr(sources, "source_from_edgar", flaky_edgar)
    events = list(enrich_firm(FIRM_NAME, top=3, store=store, provider=FakeProvider()))

    errors = [e for e in events if e.event == EventType.ERROR]
    assert len(errors) == 1
    assert errors[0].data["ticker"] == "MSFT"
    assert errors[0].data["fatal"] is False
    assert "source" in errors[0].data["where"]

    done = events[-1]
    assert done.event == EventType.JOB_COMPLETED
    assert done.data["enriched"] == 1  # NVDA succeeded
    assert done.data["failed"] == 1  # MSFT failed
    assert done.data["holdings_total"] == 2


def test_enrich_resolves_holding_by_name_when_no_ticker(monkeypatch):
    """A tickerless holding (the N-PORT norm) is fetched by resolving its NAME to a CIK
    (`sources.cik_from_name`) and pulling that CIK's 10-K — so real onboarded holdings still enrich."""
    calls: list[dict] = []

    def fake_edgar(*, ticker=None, form=None, sections=None, **kw) -> SourceDoc:
        calls.append({"ticker": ticker, "form": form, "sections": sections})
        return _make_source(ticker or "unknown")

    def fake_cik(name, *, user_agent=None):
        return {"Guidewire Software Inc": "1528396"}.get(name)

    monkeypatch.setattr(sources, "source_from_edgar", fake_edgar)
    monkeypatch.setattr(sources, "cik_from_name", fake_cik)

    store = FakeStore(holdings=[{"name": "Guidewire Software Inc", "ticker": None, "weight": 5.0}])
    events = list(enrich_firm(FIRM_NAME, top=1, store=store, provider=FakeProvider()))

    # the tickerless holding was fetched by its resolved CIK (as the edgar identifier arg), 10-K/1A
    tenk = [c for c in calls if c["form"] == "10-K"]
    assert [c["ticker"] for c in tenk] == ["1528396"]
    assert tenk[0]["sections"] == ["1A"]
    done = events[-1].data
    assert done["enriched"] == 1 and done["holdings_total"] == 1 and done["failed"] == 0


def test_enrich_skips_holding_with_no_ticker_and_unresolvable_name(monkeypatch):
    """A tickerless holding whose name resolves to no CIK is a non-fatal, best-effort skip."""
    monkeypatch.setattr(sources, "source_from_edgar",
                        lambda **kw: _make_source(kw.get("ticker") or "x"))
    monkeypatch.setattr(sources, "cik_from_name", lambda name, *, user_agent=None: None)

    store = FakeStore(holdings=[{"name": "Prc Newco Inc", "ticker": None, "weight": 2.0}])
    events = list(enrich_firm(FIRM_NAME, top=1, store=store, provider=FakeProvider()))

    errors = [e for e in events if e.event == EventType.ERROR]
    assert len(errors) == 1 and errors[0].data["fatal"] is False
    assert "source" in errors[0].data["where"]
    done = events[-1].data
    assert done["enriched"] == 0 and done["failed"] == 1 and done["holdings_total"] == 1


def test_enrich_hard_caps_filings_at_five(stub_edgar):
    """`top` is clamped to <= 5 filings even when a caller asks for more."""
    many = [{"name": f"Co {i}", "ticker": f"T{i}", "weight": 100 - i} for i in range(10)]
    store = FakeStore(holdings=many)
    events = list(enrich_firm(FIRM_NAME, top=99, store=store, provider=FakeProvider()))

    assert events[0].data["top"] == 5
    assert len(events[0].data["holdings"]) == 5
    assert len(stub_edgar) == 5  # never fetched more than the cap
    assert events[-1].data["holdings_total"] == 5


# --- prospectuses: the 485BPOS fund stage ---------------------------------------------------


def _make_prospectus(ticker: str) -> SourceDoc:
    return SourceDoc(
        doc_id=f"{ticker}_485bpos",
        doc_type="485BPOS",
        sensitivity="public",
        text=PROSPECTUS,
        title=f"{ticker} 485BPOS",
        source="edgar",
    )


def test_enrich_fetches_fund_prospectuses(monkeypatch):
    """After the holdings' 10-Ks, each fund's 485BPOS principal-risks are ingested + forwarded."""
    calls: list[dict] = []

    def fake_edgar(*, ticker=None, form=None, sections=None, **kw) -> SourceDoc:
        calls.append({"ticker": ticker, "form": form, "sections": sections})
        return _make_prospectus(ticker) if form == "485BPOS" else _make_source(ticker)

    monkeypatch.setattr(sources, "source_from_edgar", fake_edgar)
    store = FakeStore(funds=FUNDS)  # 2 holdings (default) + 2 funds
    events = list(enrich_firm(FIRM_NAME, top=3, store=store, provider=CannedExtractor(_EXTRACTION)))

    # job.started announces both the holdings and the funds slated for enrichment
    started = events[0]
    assert started.event == EventType.JOB_STARTED
    assert [f["series_id"] for f in started.data["funds"]] == ["S000090001", "S000090002"]

    # both holdings' 10-Ks AND both funds' 485BPOS were fetched, each via the right form; the fund
    # prospectus is keyed on the registrant cik (no share-class ticker on the Fund node)
    tenk = [c for c in calls if c["form"] == "10-K"]
    prospectus = [c for c in calls if c["form"] == "485BPOS"]
    assert [c["ticker"] for c in tenk] == ["NVDA", "MSFT"]
    assert [c["ticker"] for c in prospectus] == ["0009000001", "0009000002"]

    # the prospectus ingest events are forwarded, re-tagged with the fund + form
    fund_written = [e for e in events if e.event == EventType.WRITTEN and "fund" in e.data]
    assert {e.data["fund"] for e in fund_written} == {"Demo Growth Fund", "Demo Focused Fund"}
    assert all(e.data["form"] == "485BPOS" for e in fund_written)

    # the prospectus chunks are deterministically MENTIONS-linked to the fund (firm-discoverable)
    fund_links = store.queries("(x:Fund")
    assert {p["name"] for _, p in fund_links} == {"Demo Growth Fund", "Demo Focused Fund"}

    # still exactly one lifecycle pair for the whole (holdings + funds) stream
    assert sum(e.event == EventType.JOB_STARTED for e in events) == 1
    assert sum(e.event == EventType.JOB_COMPLETED for e in events) == 1

    # the prospectus chunks landed too (real :Chunk writes, mode-aware)
    assert store.queries("MERGE (c:Chunk"), "no Chunk written"

    done = events[-1]
    assert done.data["enriched"] == 2  # holdings
    assert done.data["funds_enriched"] == 2  # prospectuses
    assert done.data["funds_failed"] == 0
    assert done.data["funds_total"] == 2
    # the risk title recurs across every filing (10-Ks + prospectuses) → deduped to one
    assert done.data["risk_factors_added"] == 1


def test_enrich_prospectus_is_best_effort_on_fund_failure(monkeypatch):
    """A per-fund 485BPOS failure emits a non-fatal error; holdings + the other fund still complete."""
    def flaky_edgar(*, ticker=None, form=None, sections=None, **kw) -> SourceDoc:
        if form == "485BPOS" and ticker == "0009000002":
            raise IngestSourceError("no 485BPOS for registrant 0009000002")
        return _make_prospectus(ticker) if form == "485BPOS" else _make_source(ticker)

    monkeypatch.setattr(sources, "source_from_edgar", flaky_edgar)
    store = FakeStore(funds=FUNDS)
    events = list(enrich_firm(FIRM_NAME, top=3, store=store, provider=FakeProvider()))

    errors = [e for e in events if e.event == EventType.ERROR]
    assert len(errors) == 1
    assert errors[0].data["fund"] == "Demo Focused Fund"
    assert errors[0].data["fatal"] is False
    assert "prospectus" in errors[0].data["where"]

    done = events[-1]
    assert done.event == EventType.JOB_COMPLETED
    assert done.data["ok"] is True
    assert done.data["enriched"] == 2  # both holdings unaffected
    assert done.data["funds_enriched"] == 1  # Demo Growth Fund succeeded
    assert done.data["funds_failed"] == 1  # Demo Focused Fund failed
    assert done.data["funds_total"] == 2


def test_enrich_total_filings_bounded_at_max(monkeypatch):
    """Holdings + prospectuses share one hard `MAX_FILINGS` budget (holdings capped first)."""
    many_h = [{"name": f"Co {i}", "ticker": f"T{i}", "weight": 100 - i} for i in range(10)]
    many_f = [
        {"name": f"Fund {i}", "series_id": f"S{i:09d}", "cik": f"C{i}", "ticker": None}
        for i in range(10)
    ]
    calls: list[dict] = []

    def fake_edgar(*, ticker=None, form=None, sections=None, **kw) -> SourceDoc:
        calls.append({"ticker": ticker, "form": form})
        return _make_prospectus(ticker) if form == "485BPOS" else _make_source(ticker)

    monkeypatch.setattr(sources, "source_from_edgar", fake_edgar)
    store = FakeStore(holdings=many_h, funds=many_f)
    events = list(enrich_firm(FIRM_NAME, top=99, store=store, provider=FakeProvider()))

    started = events[0]
    assert started.data["top"] == MAX_HOLDINGS  # holdings hard-capped
    assert len(started.data["holdings"]) == MAX_HOLDINGS
    assert len(started.data["funds"]) == MAX_FILINGS - MAX_HOLDINGS  # prospectuses get the remainder

    # the total number of filings fetched never exceeds the hard cap
    assert len(calls) == MAX_FILINGS
    assert sum(1 for c in calls if c["form"] == "10-K") == MAX_HOLDINGS
    assert sum(1 for c in calls if c["form"] == "485BPOS") == MAX_FILINGS - MAX_HOLDINGS

    done = events[-1]
    assert done.data["holdings_total"] == MAX_HOLDINGS
    assert done.data["funds_total"] == MAX_FILINGS - MAX_HOLDINGS


# --- deterministic provenance + per-filing bounding -----------------------------------------


def test_enrich_links_chunks_to_holdings_in_stub_mode(stub_edgar):
    """Even in stub mode (FakeProvider extracts nothing), every chunk of a holding's 10-K is
    deterministically MENTIONS-linked to the held company and the Document is stamped with the firm —
    so a firm-scoped `:Chunk`-[:MENTIONS]->held-company query finds them (the QA blocker)."""
    store = FakeStore()
    events = list(enrich_firm(FIRM_NAME, top=3, store=store, provider=FakeProvider()))

    # chunk→Company MENTIONS links written for both holdings, each keyed to that filing's doc_id
    links = store.queries("(x:Company")
    assert {(p["doc_id"], p["name"]) for _, p in links} == {
        ("nvda_10k_1a", "Nvidia Corporation"),
        ("msft_10k_1a", "Microsoft Corporation"),
    }
    assert store.queries("MERGE (c)-[:MENTIONS]->(x)"), "no deterministic MENTIONS edge written"

    # the enriched Documents are stamped with the firm (belt-and-suspenders firm provenance)
    stamped = store.queries("SET d.firm = $firm")
    assert {p["doc_id"] for _, p in stamped} == {"nvda_10k_1a", "msft_10k_1a"}
    assert all(p["firm"] == FIRM_NAME for _, p in stamped)

    # stub mode still adds zero risk factors — proving the linking is independent of extraction
    assert events[-1].data["risk_factors_added"] == 0


def test_enrich_bounds_large_filing_chunk_count(monkeypatch):
    """A huge 10-K is truncated to FILING_MAX_CHARS before ingest, so its chunk count (== extraction
    calls) stays small — the QA cost blocker (Bloom Energy's 10-K produced ~1400 chunks)."""
    from api.extract.chunk import chunk_document

    para = (
        "The company faces significant supply-chain concentration risk because it relies on a "
        "limited number of foreign suppliers for critical components and manufacturing capacity."
    )
    big = "Risk Factors.\n\n" + (para + "\n\n") * 8000  # ~1.4M chars, hundreds of chunks whole
    assert len(big) > 10 * FILING_MAX_CHARS  # the raw filing is far larger than the cap
    untruncated_chunks = len(chunk_document(big))
    assert untruncated_chunks > 100  # ingested whole it would be hundreds of extraction calls

    def big_edgar(*, ticker=None, form=None, sections=None, **kw) -> SourceDoc:
        return SourceDoc(
            doc_id=f"{ticker}_10k",
            doc_type="10-K",
            sensitivity="public",
            text=big,
            title=f"{ticker} 10-K",
            source="edgar",
        )

    monkeypatch.setattr(sources, "source_from_edgar", big_edgar)
    store = FakeStore(holdings=[{"name": "Big Co", "ticker": "BIG", "weight": 50.0}])
    events = list(enrich_firm(FIRM_NAME, top=1, store=store, provider=FakeProvider()))

    chunked = [e for e in events if e.event == EventType.CHUNKED]
    assert len(chunked) == 1
    # truncation to FILING_MAX_CHARS collapses hundreds of chunks to a small bounded handful
    assert 0 < chunked[0].data["chunks"] <= 10
    assert chunked[0].data["chunks"] < untruncated_chunks // 10


# --- routes ---------------------------------------------------------------------------------


def _app() -> TestClient:
    app = FastAPI()
    app.include_router(onboarding_router)
    return TestClient(app)


def _seed_firm(tmp_path, monkeypatch, *, name: str) -> str:
    """Point settings at a temp SQLite registry seeded with one firm; return its firm_id."""
    db = tmp_path / "app.db"
    monkeypatch.setenv("SQLITE_PATH", str(db))
    config.get_settings.cache_clear()
    conn = init_db(connect(str(db)))
    fs.upsert_firm(conn, name=name, status="ready", source="edgar")
    conn.close()
    return fs.slugify(name)


def test_enrich_route_streams_sse(monkeypatch, tmp_path):
    """The route resolves the firm name then bridges the sync generator to SSE (worker + queue)."""
    firm_id = _seed_firm(tmp_path, monkeypatch, name="Acme Advisers")

    def fake_enrich(firm_name, *, top=3, **kw):
        yield make_event(EventType.JOB_STARTED, "enrich-x", seq=0, firm=firm_name, top=top)
        yield make_event(EventType.WRITTEN, "enrich-x", seq=1,
                         holding="Nvidia Corporation", ticker="NVDA", nodes=3, edges=4)
        yield make_event(EventType.JOB_COMPLETED, "enrich-x", seq=2,
                         ok=True, firm=firm_name, enriched=1, risk_factors_added=2)

    monkeypatch.setattr(onboarding_routes, "enrich_firm", fake_enrich)

    seen: list[str] = []
    with _app().stream("POST", f"/firms/{firm_id}/enrich?top=2") as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("event:"):
                seen.append(line.split(":", 1)[1].strip())
            elif line.startswith("data:"):
                json.loads(line.split(":", 1)[1].strip())  # each payload is valid JSON
    assert seen[0] == "job.started"
    assert seen[-1] == "job.completed"


def test_enrich_route_unknown_firm_is_404(monkeypatch, tmp_path):
    db = tmp_path / "app.db"
    monkeypatch.setenv("SQLITE_PATH", str(db))
    config.get_settings.cache_clear()
    init_db(connect(str(db))).close()  # empty registry
    r = _app().post("/firms/does-not-exist/enrich")
    assert r.status_code == 404
