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
from api.onboarding.enrich import enrich_firm
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


# --- offline seams --------------------------------------------------------------------------


class FakeStore:
    """Records every Cypher call; returns the canned holdings for the top-holdings query, else []."""

    def __init__(self, holdings: list[dict] | None = None) -> None:
        self.holdings = holdings if holdings is not None else HOLDINGS
        self.calls: list[tuple[str, dict]] = []

    def run(self, query: str, **params) -> list:
        self.calls.append((query, params))
        if "sum(h.weight_pct)" in query:
            return list(self.holdings)
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


def test_enrich_hard_caps_filings_at_five(stub_edgar):
    """`top` is clamped to <= 5 filings even when a caller asks for more."""
    many = [{"name": f"Co {i}", "ticker": f"T{i}", "weight": 100 - i} for i in range(10)]
    store = FakeStore(holdings=many)
    events = list(enrich_firm(FIRM_NAME, top=99, store=store, provider=FakeProvider()))

    assert events[0].data["top"] == 5
    assert len(events[0].data["holdings"]) == 5
    assert len(stub_edgar) == 5  # never fetched more than the cap
    assert events[-1].data["holdings_total"] == 5


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
