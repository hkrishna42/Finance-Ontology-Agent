"""Offline onboarding-orchestrator tests (fake store / stub resolver / stubbed discovery).

Proves the Phase-2 onboarding acceptance surface without a database or the network:
  * `onboard_firm(...)` emits the onboarding SSE sequence in contract order with monotonic seq;
  * each series is fetched → parsed → written, and the `Fund` is linked `MANAGED_BY` the firm;
  * resolved issuers enrich their `Company` nodes (merged/provisional counts land on `resolved`);
  * the SQLite registry row transitions `onboarding → ready` and the firm is activated on completion;
  * the `POST /firms/search` + `POST /firms/onboard` routes wire discovery/the SSE bridge correctly.

Discovery is stubbed (`monkeypatch`) to return the committed demo N-PORT fixture, so no EDGAR/GLEIF
call happens; the resolver is a deterministic stub, so no spine/embedding work happens either.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.contracts.events import EventType, make_event
from api.firms import store as fs
from api.onboarding import discovery
from api.onboarding import routes as onboarding_routes
from api.onboarding.pipeline import onboard_firm
from api.onboarding.routes import router as onboarding_router
from api.stores.sqlite import connect, init_db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "nport"
GROWTH_XML = (FIXTURES / "demo_growth_S000090001.xml").read_text()

FIRM_NAME = "Test Onboard Advisers LLC"
FIRM_ID = fs.slugify(FIRM_NAME)
PICKED = {
    "name": FIRM_NAME,
    "cik": "0009000001",
    "lei": "DEMOADVISER000000001",
    "series": [{"series_id": "S000090001", "name": "Demo Growth Fund"}],
}
CANDIDATE = {
    "name": FIRM_NAME,
    "cik": "0009000001",
    "lei": "DEMOADVISER000000001",
    "source": "edgar",
    "kind": "fund_family",
    "series": [{"series_id": "S000090001", "name": "Demo Growth Fund", "class_tickers": []}],
    "score": 0.99,
}


# --- offline seams --------------------------------------------------------------------------


class FakeStore:
    """Records every Cypher call; returns no rows."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def run(self, query: str, **params) -> list:
        self.calls.append((query, params))
        return []

    def close(self) -> None:  # pragma: no cover - lifecycle no-op
        pass

    def queries(self, needle: str) -> list[tuple[str, dict]]:
        return [(q, p) for q, p in self.calls if needle in q]


class _StubResolution:
    def __init__(self, resolved: bool, *, cik=None, lei=None, ticker=None) -> None:
        self.resolved = resolved
        self.cik = cik
        self.lei = lei
        self.ticker = ticker


class StubResolver:
    """Deterministic resolver: issuer names in `resolved` pin to a CIK; everything else provisional."""

    def __init__(self, resolved: set[str] | None = None) -> None:
        self.resolved = resolved or set()
        self.calls: list[str] = []

    def resolve(self, name, *, ticker=None, enrich_lei=True, conn=None) -> _StubResolution:
        self.calls.append(name)
        if name in self.resolved:
            return _StubResolution(True, cik="0001045810", lei=f"LEI-{name[:4]}", ticker=ticker)
        return _StubResolution(False)


@pytest.fixture()
def stub_discovery(monkeypatch):
    """Stub the (parallel-built) discovery layer: fixture XML + a canned search candidate."""
    monkeypatch.setattr(
        discovery,
        "fetch_series_nport_xml",
        lambda series_id, *, settings=None: GROWTH_XML if series_id == "S000090001" else None,
    )
    monkeypatch.setattr(
        discovery, "search_firms", lambda query, *, limit=8, settings=None: [CANDIDATE]
    )
    return discovery


# --- pipeline: SSE sequence + graph writes --------------------------------------------------


def test_onboard_emits_sequence_and_links_managed_by(stub_discovery):
    store = FakeStore()
    conn = init_db(connect(":memory:"))
    resolver = StubResolver({"NVIDIA", "Microsoft"})
    try:
        events = list(onboard_firm(PICKED, store=store, conn=conn, resolver=resolver))
    finally:
        conn.close()

    # SSE sequence: job.started → parsed → written → resolved → job.completed
    assert [e.event for e in events] == [
        EventType.JOB_STARTED,
        EventType.PARSED,
        EventType.WRITTEN,
        EventType.RESOLVED,
        EventType.JOB_COMPLETED,
    ]
    # seq is monotonic 0..n and every event carries the same job_id
    assert [e.seq for e in events] == list(range(len(events)))
    assert len({e.job_id for e in events}) == 1

    by = {e.event: e for e in events}
    assert by[EventType.JOB_STARTED].data["firm"] == FIRM_NAME
    assert by[EventType.PARSED].data["series_id"] == "S000090001"
    assert by[EventType.PARSED].data["fund_name"] == "Demo Growth Fund"
    assert by[EventType.PARSED].data["holdings"] == 9
    assert by[EventType.WRITTEN].data["fund_name"] == "Demo Growth Fund"
    assert by[EventType.WRITTEN].data["edges"] == 10  # 9 HOLDS + 1 MANAGED_BY
    assert by[EventType.WRITTEN].data["nodes"] == 10  # 1 Fund + 9 issuer Companies
    assert by[EventType.RESOLVED].data == {"merged": 2, "provisional": 7}
    assert by[EventType.JOB_COMPLETED].data["ok"] is True
    assert by[EventType.JOB_COMPLETED].data["firm_id"] == FIRM_ID
    assert by[EventType.JOB_COMPLETED].data["funds"] == 1
    assert by[EventType.JOB_COMPLETED].data["holdings"] == 9

    # firm Company MERGE, Fund MERGE, 9 HOLDS, and the MANAGED_BY link are all written
    assert store.queries("MERGE (firm:Company {name: $n})"), "firm Company node not MERGEd"
    assert len(store.queries("MERGE (f:Fund {name: $fund_name})")) == 1, "Fund not written"
    assert len(store.queries("HOLDS")) == 9, "expected one HOLDS per position"
    managed = store.queries("MERGE (f)-[:MANAGED_BY]->(firm)")
    assert len(managed) == 1, "Fund not linked MANAGED_BY the firm"
    assert managed[0][1] == {"fn": "Demo Growth Fund", "firmn": FIRM_NAME}

    # only the two resolved issuers get their Company node enriched
    enrich = store.queries("MATCH (co:Company {name: $n})")
    assert {p["n"] for _, p in enrich} == {"NVIDIA", "Microsoft"}
    assert all(p["cik"] == "0001045810" for _, p in enrich)


# --- registry lifecycle: onboarding → ready + active ----------------------------------------


def test_registry_row_transitions_onboarding_to_ready_active(stub_discovery):
    store = FakeStore()
    conn = init_db(connect(":memory:"))
    fs.ensure_demo_firm(conn)  # demo firm exists and is the active one
    resolver = StubResolver({"NVIDIA"})
    try:
        gen = onboard_firm(PICKED, store=store, conn=conn, resolver=resolver)

        ev0 = next(gen)  # job.started yields BEFORE the registry write
        assert ev0.event == EventType.JOB_STARTED
        assert fs.get_firm(conn, FIRM_ID) is None

        ev1 = next(gen)  # advancing runs the `onboarding` upsert, then yields parsed
        assert ev1.event == EventType.PARSED
        mid = fs.get_firm(conn, FIRM_ID)
        assert mid is not None
        assert mid["status"] == "onboarding"
        assert mid["is_active"] is False  # demo still active mid-flight

        rest = list(gen)  # written → resolved → job.completed (the `ready` + activate upsert)
        assert rest[-1].event == EventType.JOB_COMPLETED

        final = fs.get_firm(conn, FIRM_ID)
        assert final["status"] == "ready"
        assert final["is_active"] is True  # activated on completion
        assert final["fund_count"] == 1
        assert final["funds"][0]["series_id"] == "S000090001"
        assert final["identifiers"]["series"][0]["series_id"] == "S000090001"
        # activating the new firm deactivates the demo firm (single-active invariant)
        assert fs.get_firm(conn, "demo_investment_management")["is_active"] is False
    finally:
        conn.close()


# --- routes ---------------------------------------------------------------------------------


def _app() -> TestClient:
    app = FastAPI()
    app.include_router(onboarding_router)
    return TestClient(app)


def test_search_route_returns_candidates(stub_discovery):
    r = _app().post("/firms/search", json={"query": "demo"})
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list) and data
    assert data[0]["name"] == FIRM_NAME and data[0]["source"] == "edgar"


def test_search_route_blank_query_is_200_empty(stub_discovery):
    r = _app().post("/firms/search", json={"query": "   "})
    assert r.status_code == 200
    assert r.json() == []


def test_onboard_route_streams_sse(monkeypatch):
    """The route bridges the sync generator to SSE (worker thread + queue), like the ingest route."""

    def fake_onboard(picked, *, settings=None, store=None, conn=None, resolver=None):
        yield make_event(EventType.JOB_STARTED, "onboard-x", seq=0, firm=picked["name"])
        yield make_event(EventType.RESOLVED, "onboard-x", seq=1, merged=0, provisional=0)
        yield make_event(EventType.JOB_COMPLETED, "onboard-x", seq=2, ok=True)

    monkeypatch.setattr(onboarding_routes, "onboard_firm", fake_onboard)

    events: list[str] = []
    with _app().stream("POST", "/firms/onboard", json={"name": "X", "series": []}) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("event:"):
                events.append(line.split(":", 1)[1].strip())
            elif line.startswith("data:"):
                json.loads(line.split(":", 1)[1].strip())  # each data payload is valid JSON
    assert events[0] == "job.started"
    assert events[-1] == "job.completed"


def test_onboard_route_requires_name():
    r = _app().post("/firms/onboard", json={"series": []})
    assert r.status_code == 400
