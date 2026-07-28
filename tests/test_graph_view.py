from api.graph_view import shape_documents, shape_subgraph


def _row():
    return {
        "n_id": "n1", "n_type": "Fund", "n_label": "Demo Growth Fund",
        "n_props": {"series_id": "S000090001", "embedding": [0.1] * 384},
        "m_id": "n2", "m_type": "Company", "m_label": "NVIDIA",
        "m_props": {"ticker": "NVDA"},
        "r_id": "r1", "r_type": "HOLDS", "r_conf": 1.0,
        "r_props": {"weight_pct": 11.5, "confidence": 1.0},
    }


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
