import api.graph_view as gv
from api.graph_view import (
    _DOCS_CYPHER,
    _DOCS_FIRM_CYPHER,
    _SUBGRAPH_CYPHER,
    _SUBGRAPH_FIRM_CYPHER,
    get_documents,
    get_graph,
    shape_documents,
    shape_subgraph,
)


def _row():
    return {
        "n_id": "n1", "n_type": "Fund", "n_label": "Demo Growth Fund",
        "n_props": {"series_id": "S000090001", "embedding": [0.1] * 384},
        "m_id": "n2", "m_type": "Company", "m_label": "NVIDIA",
        "m_props": {"ticker": "NVDA"},
        "r_id": "r1", "r_type": "HOLDS", "r_conf": 1.0,
        "r_props": {"weight_pct": 11.5, "confidence": 1.0},
    }


def _doc_row():
    return {
        "doc_id": "nvda_10k", "title": "NVIDIA FY2025 10-K", "doc_type": "10-K",
        "sensitivity": "public", "filing_date": "2026-02-26", "url": None,
        "chunks": [
            {"chunk_id": "nvda_10k_c1", "text": "We depend on TSMC.", "sensitivity": "public"},
        ],
    }


# --- pure shape functions ------------------------------------------------------------------


def test_shape_dedupes_and_strips_embedding():
    row2 = _row() | {"m_id": "n3", "m_type": "Company", "m_label": "Microsoft",
                     "m_props": {"ticker": "MSFT"}, "r_id": "r2"}
    out = shape_subgraph([_row(), row2])
    assert len(out["nodes"]) == 3
    assert len(out["edges"]) == 2
    fund = next(n for n in out["nodes"] if n["type"] == "Fund")
    assert "embedding" not in fund["props"]
    assert fund["label"] == "Demo Growth Fund"
    edge = out["edges"][0]
    assert edge["source"] == "n1" and edge["type"] == "HOLDS" and edge["confidence"] == 1.0


def test_label_falls_back_to_type():
    out = shape_subgraph([_row() | {"m_label": None}])
    m = next(n for n in out["nodes"] if n["id"] == "n2")
    assert m["label"] == "Company"


def test_null_confidence_normalizes_to_one():
    # structural edges (no confidence) must not surface as null -> UI numeric filter breaks
    out = shape_subgraph([_row() | {"r_conf": None}])
    assert out["edges"][0]["confidence"] == 1.0


def test_shape_documents_builds_docrecords_with_spans():
    rows = [{
        "doc_id": "nvda_10k", "title": "NVIDIA FY2025 10-K", "doc_type": "10-K",
        "sensitivity": "public", "filing_date": "2026-02-26", "url": None,
        "chunks": [
            {"chunk_id": "nvda_10k_c1", "text": "We depend on TSMC.", "sensitivity": "public"},
            {"chunk_id": "nvda_10k_c2", "text": "Customer concentration.", "sensitivity": "public"},
            None,  # OPTIONAL MATCH can yield a null
        ],
    }]
    docs = shape_documents(rows)
    assert len(docs) == 1
    d = docs[0]
    assert d["doc_id"] == "nvda_10k" and d["sensitivity"] == "public"
    assert "We depend on TSMC." in d["text"] and "Customer concentration." in d["text"]
    assert [s["chunk_id"] for s in d["spans"]] == ["nvda_10k_c1", "nvda_10k_c2"]
    assert d["spans"][0]["quote"] == "We depend on TSMC."


# --- firm-scoping (offline fake driver: records which Cypher + params a handler runs) -------


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def data(self):
        return self._rows


class _FakeSession:
    def __init__(self, calls, rows):
        self._calls = calls
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, cypher, **params):
        self._calls.append({"cypher": cypher, "params": params})
        return _FakeResult(self._rows)


class _FakeDriver:
    def __init__(self, calls, rows):
        self._calls = calls
        self._rows = rows

    def session(self):
        return _FakeSession(self._calls, self._rows)

    def close(self):
        pass


def _install_fake(monkeypatch, rows):
    """Route the handlers' Neo4j driver to an offline fake; return the recorded-calls list."""
    calls: list[dict] = []
    monkeypatch.setattr(gv, "_driver", lambda: _FakeDriver(calls, rows))
    return calls


def test_graph_scoped_to_active_firm_uses_firm_cypher(monkeypatch):
    # No explicit ?firm= → the active firm is resolved and its subgraph query is used.
    monkeypatch.setattr(gv.scope, "active_firm_name", lambda conn=None: "ACME GLOBAL REAL ESTATE FUND")
    calls = _install_fake(monkeypatch, [_row()])
    out = get_graph(limit=300, min_confidence=0.0, entitlements=["public"], firm=None)
    assert len(calls) == 1
    assert calls[0]["cypher"] == _SUBGRAPH_FIRM_CYPHER
    assert calls[0]["params"]["firm"] == "ACME GLOBAL REAL ESTATE FUND"
    # entitlement / confidence / limit still threaded through unchanged
    assert calls[0]["params"]["entitlements"] == ["public"]
    assert calls[0]["params"]["min_conf"] == 0.0 and calls[0]["params"]["limit"] == 300
    # rows are shaped by the same pure function (nodes/edges shape unchanged)
    assert len(out["nodes"]) == 2 and len(out["edges"]) == 1


def test_graph_explicit_firm_overrides_active(monkeypatch):
    # An explicit ?firm= wins over the active firm and never touches the registry.
    monkeypatch.setattr(gv.scope, "active_firm_name",
                        lambda conn=None: (_ for _ in ()).throw(AssertionError("registry touched")))
    calls = _install_fake(monkeypatch, [_row()])
    get_graph(limit=50, min_confidence=0.5, entitlements=["public", "internal"],
              firm="Demo Investment Management")
    assert calls[0]["cypher"] == _SUBGRAPH_FIRM_CYPHER
    assert calls[0]["params"]["firm"] == "Demo Investment Management"
    assert calls[0]["params"]["entitlements"] == ["public", "internal"]
    assert calls[0]["params"]["min_conf"] == 0.5 and calls[0]["params"]["limit"] == 50


def test_graph_falls_back_to_whole_graph_when_no_firm(monkeypatch):
    # No active firm (fresh / demo-less install) → whole-graph query, no firm param → never blank.
    monkeypatch.setattr(gv.scope, "active_firm_name", lambda conn=None: None)
    calls = _install_fake(monkeypatch, [_row()])
    out = get_graph(limit=300, min_confidence=0.0, entitlements=["public"], firm=None)
    assert calls[0]["cypher"] == _SUBGRAPH_CYPHER
    assert "firm" not in calls[0]["params"]
    assert len(out["nodes"]) == 2


def test_documents_scoped_to_firm(monkeypatch):
    calls = _install_fake(monkeypatch, [_doc_row()])
    docs = get_documents(entitlements=["public"], firm="Demo Investment Management")
    assert calls[0]["cypher"] == _DOCS_FIRM_CYPHER
    assert calls[0]["params"]["firm"] == "Demo Investment Management"
    assert calls[0]["params"]["entitlements"] == ["public"]
    assert len(docs) == 1 and docs[0]["doc_id"] == "nvda_10k"


def test_documents_empty_for_unenriched_firm(monkeypatch):
    # A structural-only firm has no chunks mentioning its holdings → [] (NOT the demo's filings).
    calls = _install_fake(monkeypatch, [])
    docs = get_documents(entitlements=["public"], firm="ACME GLOBAL REAL ESTATE FUND")
    assert calls[0]["cypher"] == _DOCS_FIRM_CYPHER
    assert docs == []


def test_documents_whole_corpus_when_no_firm(monkeypatch):
    monkeypatch.setattr(gv.scope, "active_firm_name", lambda conn=None: None)
    calls = _install_fake(monkeypatch, [])
    get_documents(entitlements=["public"], firm=None)
    assert calls[0]["cypher"] == _DOCS_CYPHER
    assert "firm" not in calls[0]["params"]


def test_graph_all_scope_sentinel_is_unscoped(monkeypatch):
    # The "All data" scope: ?firm=__all__ forces the whole-graph query even when a firm IS active,
    # so a document uploaded outside any firm is still browsable.
    from api.firms.scope import ALL_SCOPE

    monkeypatch.setattr(gv.scope, "active_firm_name", lambda conn=None: "Demo Investment Management")
    calls = _install_fake(monkeypatch, [_row()])
    out = get_graph(limit=300, min_confidence=0.0, entitlements=["public"], firm=ALL_SCOPE)
    assert calls[0]["cypher"] == _SUBGRAPH_CYPHER  # whole graph, not the firm subgraph
    assert "firm" not in calls[0]["params"]
    assert len(out["nodes"]) == 2


def test_documents_all_scope_sentinel_is_unscoped(monkeypatch):
    from api.firms.scope import ALL_SCOPE

    monkeypatch.setattr(gv.scope, "active_firm_name", lambda conn=None: "Demo Investment Management")
    calls = _install_fake(monkeypatch, [_doc_row()])
    docs = get_documents(entitlements=["public"], firm=ALL_SCOPE)
    assert calls[0]["cypher"] == _DOCS_CYPHER  # all documents, not the firm's
    assert "firm" not in calls[0]["params"]
    assert len(docs) == 1
