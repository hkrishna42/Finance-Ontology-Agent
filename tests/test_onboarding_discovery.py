"""Phase-2 / onboarding — EDGAR + GLEIF discovery, fully OFFLINE (mocked network).

Every EDGAR call (`edgar.find_funds` / `edgar.find_company` / `edgar.Fund` / `edgar.set_identity`)
and the `GleifClient` are monkeypatched so nothing touches the network. Covers `search_firms`
merge/dedupe/rank + best-effort partial-failure handling, and `fetch_series_nport_xml` returning
the primary NPORT-P XML string (which `api.l2.nport.parse_nport` then accepts).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import edgar
import httpx
import pandas as pd
import pytest

from api.l2 import nport
from api.onboarding import discovery
from api.resolution.gleif import GLEIF_BASE_URL, GleifClient

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "nport"
GROWTH_XML = (FIXTURES / "demo_growth_S000090001.xml").read_text()


# --- shared fakes --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_network_waits(monkeypatch):
    """Neutralize the SEC throttle/backoff + identity call so mocked tests stay fast + hermetic."""
    monkeypatch.setattr(discovery, "_throttle", lambda: None)
    monkeypatch.setattr(discovery, "_backoff", lambda *a, **k: None)
    monkeypatch.setattr(discovery, "_identity_set", False, raising=False)
    monkeypatch.setattr(edgar, "set_identity", lambda *a, **k: None)


def _series(series_id: str, name: str, cik: str):
    return SimpleNamespace(series_id=series_id, name=name, cik=cik)


def _klass(series_id: str, ticker: str, name: str = "Class"):
    return SimpleNamespace(series_id=series_id, ticker=ticker, name=name)


def _company(cik: str, name: str):
    return SimpleNamespace(cik=cik, name=name)


def _install_find_funds(monkeypatch, *, series=None, classes=None, companies=None):
    buckets = {"series": series or [], "class": classes or [], "company": companies or []}

    def fake_find_funds(query, search_type="series"):
        return buckets.get(search_type, [])

    monkeypatch.setattr(edgar, "find_funds", fake_find_funds)


def _install_find_company(monkeypatch, rows=None):
    """rows: list of dicts with cik/ticker/company/score (mirrors CompanySearchResults.results)."""
    df = pd.DataFrame(rows or [], columns=["cik", "ticker", "company", "score"])
    results = SimpleNamespace(empty=df.empty, results=df)
    monkeypatch.setattr(edgar, "find_company", lambda query: results)


def _install_gleif(monkeypatch, hits):
    class _FakeGleif:
        def search(self, name, *, limit=5):
            return list(hits)

        def close(self):
            pass

    monkeypatch.setattr(discovery, "GleifClient", lambda *a, **k: _FakeGleif())


def _install_gleif_raising(monkeypatch):
    class _Boom:
        def search(self, name, *, limit=5):
            raise RuntimeError("gleif down")

        def close(self):
            pass

    monkeypatch.setattr(discovery, "GleifClient", lambda *a, **k: _Boom())


# --- search_firms: merge / dedupe / rank ---------------------------------------------------


def test_search_merges_fund_family_series_and_attaches_gleif_lei(monkeypatch):
    _install_find_funds(
        monkeypatch,
        series=[
            _series("S000001", "Vanguard 500 Index Fund", "0000102909"),
            _series("S000002", "Vanguard Growth Index Fund", "0000102909"),
        ],
        classes=[
            _klass("S000001", "VFIAX"),
            _klass("S000001", "VOO"),
            _klass("S000002", "VIGAX"),
        ],
        companies=[_company("0000102909", "VANGUARD INDEX FUNDS")],
    )
    _install_find_company(monkeypatch, [])
    _install_gleif(
        monkeypatch,
        [
            {"lei": "LEI-VANGUARD-INDEX", "name": "VANGUARD INDEX FUNDS"},
            {"lei": "LEI-UNRELATED-999", "name": "Unrelated Holdings PLC"},
        ],
    )

    out = discovery.search_firms("Vanguard", limit=8)

    # One fund family (deduped by CIK) carrying both series, ranked first (onboardable).
    top = out[0]
    assert top["kind"] == "fund_family"
    assert top["source"] == "edgar"
    assert top["cik"] == "0000102909"
    assert top["name"] == "VANGUARD INDEX FUNDS"
    assert {s["series_id"] for s in top["series"]} == {"S000001", "S000002"}
    by_id = {s["series_id"]: s for s in top["series"]}
    assert by_id["S000001"]["class_tickers"] == ["VFIAX", "VOO"]
    assert by_id["S000002"]["class_tickers"] == ["VIGAX"]
    # GLEIF LEI folded into the matching EDGAR family, not added as a duplicate.
    assert top["lei"] == "LEI-VANGUARD-INDEX"

    # The non-matching GLEIF record survives as a standalone company candidate.
    standalone = [c for c in out if c["source"] == "gleif"]
    assert len(standalone) == 1
    assert standalone[0]["lei"] == "LEI-UNRELATED-999"
    assert standalone[0]["kind"] == "company"
    assert standalone[0]["series"] == []

    # Ranking: onboardable fund family outranks the bare GLEIF hit.
    assert out.index(top) < out.index(standalone[0])
    assert top["score"] > standalone[0]["score"]


def test_search_dedupes_operating_company_by_cik(monkeypatch):
    _install_find_funds(monkeypatch)  # no fund families
    _install_find_company(
        monkeypatch,
        [{"cik": 320193, "ticker": "AAPL", "company": "APPLE INC", "score": 99}],
    )
    _install_gleif(monkeypatch, [{"lei": "LEI-APPLE", "name": "APPLE INC"}])

    out = discovery.search_firms("Apple", limit=8)

    assert len(out) == 1  # EDGAR company + GLEIF LEI merged into one candidate
    cand = out[0]
    assert cand["kind"] == "company"
    assert cand["cik"] == "320193"
    assert cand["lei"] == "LEI-APPLE"
    assert cand["series"] == []


def test_search_blank_query_returns_empty_without_network(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("must not touch the network for a blank query")

    monkeypatch.setattr(edgar, "find_funds", _boom)
    monkeypatch.setattr(edgar, "find_company", _boom)
    monkeypatch.setattr(discovery, "GleifClient", _boom)

    assert discovery.search_firms("   ") == []


def test_search_no_hits_returns_empty_list(monkeypatch):
    _install_find_funds(monkeypatch)
    _install_find_company(monkeypatch, [])
    _install_gleif(monkeypatch, [])
    assert discovery.search_firms("Nonexistent Fund XYZ") == []


# --- search_firms: best-effort partial failure ---------------------------------------------


def test_search_edgar_failure_still_returns_gleif(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("EDGAR 503")

    monkeypatch.setattr(edgar, "find_funds", _boom)
    monkeypatch.setattr(edgar, "find_company", _boom)
    _install_gleif(monkeypatch, [{"lei": "LEI-ONLY", "name": "Only Via Gleif Ltd"}])

    out = discovery.search_firms("Only Via Gleif")
    assert [c["lei"] for c in out] == ["LEI-ONLY"]
    assert out[0]["source"] == "gleif"


def test_search_gleif_failure_still_returns_edgar(monkeypatch):
    _install_find_funds(
        monkeypatch,
        series=[_series("S000010", "Fidelity Contrafund", "0000024238")],
        companies=[_company("0000024238", "FIDELITY CONTRAFUND")],
    )
    _install_find_company(monkeypatch, [])
    _install_gleif_raising(monkeypatch)

    out = discovery.search_firms("Fidelity")
    assert len(out) == 1
    assert out[0]["source"] == "edgar"
    assert out[0]["kind"] == "fund_family"
    assert out[0]["lei"] is None  # GLEIF failed → no LEI, but EDGAR result survives


# --- fetch_series_nport_xml ----------------------------------------------------------------


class _FakeFiling:
    def __init__(self, xml):
        self._xml = xml

    def xml(self):
        return self._xml


class _FakeFilings:
    def __init__(self, filings):
        self._f = list(filings)

    def __len__(self):
        return len(self._f)

    def __getitem__(self, i):
        return self._f[i]

    def latest(self, n=1):
        return self._f[0] if self._f else None


def _install_fund(monkeypatch, *, series_filings, umbrella_filings=None, calls=None):
    class _Fund:
        def __init__(self, identifier):
            self.identifier = identifier

        def get_filings(self, series_only=False, form=None):
            if calls is not None:
                calls.append({"series_only": series_only, "form": form})
            if series_only:
                return _FakeFilings(series_filings)
            return _FakeFilings(series_filings if umbrella_filings is None else umbrella_filings)

    monkeypatch.setattr(edgar, "Fund", _Fund)


def test_fetch_series_returns_primary_xml_via_series_scope(monkeypatch):
    calls: list[dict] = []
    _install_fund(monkeypatch, series_filings=[_FakeFiling(GROWTH_XML)], calls=calls)

    xml = discovery.fetch_series_nport_xml("S000090001")

    assert xml == GROWTH_XML
    # series-scoped NPORT-P request (avoids sibling-series data from the umbrella trust)
    assert calls[0] == {"series_only": True, "form": "NPORT-P"}
    # and the returned string is exactly what the L2 parser consumes
    filing = nport.parse_nport(xml)
    assert filing.series_id == "S000090001"
    assert filing.fund_name == "Demo Growth Fund"
    assert len(filing.holdings) == 9


def test_fetch_series_never_falls_back_to_umbrella_trust(monkeypatch):
    # An empty series scope must NOT fall back to the trust-wide list, whose .latest() would be a
    # SIBLING series' report (GH #888). It must return None and never issue a non-series query.
    calls: list[dict] = []
    _install_fund(
        monkeypatch,
        series_filings=[],  # series_only path yields nothing
        umbrella_filings=[_FakeFiling(GROWTH_XML)],  # present, but must be ignored
        calls=calls,
    )

    assert discovery.fetch_series_nport_xml("S000090001") is None
    assert calls == [{"series_only": True, "form": "NPORT-P"}]  # exactly one, series-scoped call


def test_fetch_series_discards_mismatched_sibling_series_xml(monkeypatch):
    # If EDGAR ever returns a primary doc for the wrong series, the seriesId guard drops it.
    _install_fund(monkeypatch, series_filings=[_FakeFiling(GROWTH_XML)])  # xml is S000090001
    assert discovery.fetch_series_nport_xml("S000099999") is None


def test_fetch_series_none_when_no_filings(monkeypatch):
    _install_fund(monkeypatch, series_filings=[])
    assert discovery.fetch_series_nport_xml("S000090001") is None


def test_fetch_series_blank_returns_none_without_network(monkeypatch):
    monkeypatch.setattr(
        edgar, "Fund", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network"))
    )
    assert discovery.fetch_series_nport_xml("") is None
    assert discovery.fetch_series_nport_xml("   ") is None


def test_fetch_series_swallows_errors_returns_none(monkeypatch):
    def _boom(identifier):
        raise RuntimeError("EDGAR unreachable")

    monkeypatch.setattr(edgar, "Fund", _boom)
    assert discovery.fetch_series_nport_xml("S000090001") is None


def test_fetch_series_none_when_xml_is_empty(monkeypatch):
    _install_fund(monkeypatch, series_filings=[_FakeFiling(None)])
    assert discovery.fetch_series_nport_xml("S000090001") is None


# --- GleifClient.search (fuzzy fulltext) directly, via MockTransport ------------------------


def _gleif_client(handler) -> GleifClient:
    transport = httpx.MockTransport(handler)
    inner = httpx.Client(base_url=GLEIF_BASE_URL, transport=transport)
    return GleifClient(inner)


def test_gleif_search_uses_fulltext_and_parses_records():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/lei-records")
        assert request.url.params["filter[fulltext]"] == "Vanguard"
        assert request.url.params["page[size]"] == "5"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "LEI-A",
                        "attributes": {
                            "lei": "LEI-A",
                            "entity": {"legalName": {"name": "VANGUARD GROUP INC"}},
                        },
                    },
                    {"id": "LEI-B", "attributes": {"entity": {"legalName": {"name": "Vanguard 2"}}}},
                ]
            },
        )

    hits = _gleif_client(handler).search("Vanguard", limit=5)
    assert hits == [
        {"lei": "LEI-A", "name": "VANGUARD GROUP INC"},
        {"lei": "LEI-B", "name": "Vanguard 2"},  # LEI falls back to record id
    ]


def test_gleif_search_empty_and_error_are_empty_list():
    ok_empty = _gleif_client(lambda req: httpx.Response(200, json={"data": []}))
    assert ok_empty.search("Nothing") == []

    boom = _gleif_client(lambda req: httpx.Response(500, json={"error": "boom"}))
    assert boom.search("Anything") == []

    # blank query short-circuits without an HTTP call
    def _no_call(req):
        raise AssertionError("must not call for blank query")

    assert _gleif_client(_no_call).search("   ") == []
