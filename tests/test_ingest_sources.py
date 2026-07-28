"""Offline tests for ingest source resolution + deterministic classification."""

from __future__ import annotations

import pytest

from api.ingest.sources import (
    IngestSourceError,
    detect_doc_type,
    detect_sensitivity,
    source_from_bytes,
    source_from_edgar,
    source_from_file,
    source_from_text,
    strip_html,
)


def test_strip_html_removes_tags_and_keeps_paragraphs():
    html = (
        "<html><head><style>.x{}</style></head><body>"
        "<h1>Risk Factors</h1><p>We depend on TSMC for fabrication.</p>"
        "<p>Advanced packaging is constrained.</p>"
        "<script>evil()</script></body></html>"
    )
    text = strip_html(html)
    assert "evil()" not in text
    assert ".x{}" not in text
    assert "We depend on TSMC for fabrication." in text
    # block tags become paragraph boundaries the chunker can split on
    assert "\n\n" in text
    assert "Advanced packaging is constrained." in text


def test_source_from_text_basic_and_hash_id():
    doc = source_from_text("NVIDIA depends on TSMC for GPU fabrication.")
    assert doc.source == "text"
    assert doc.doc_id.startswith("doc_")
    assert doc.sensitivity == "public"
    assert doc.chars > 0


def test_source_from_text_empty_raises():
    with pytest.raises(IngestSourceError):
        source_from_text("   ")


def test_source_from_text_html_is_stripped():
    doc = source_from_text("<div><p>Apple sources silicon from TSMC.</p></div>")
    assert "<p>" not in doc.text
    assert "Apple sources silicon from TSMC." in doc.text


def test_explicit_classification_overrides_heuristic():
    doc = source_from_text("hello world", doc_type="8-K", sensitivity="internal", doc_id="d1")
    assert (doc.doc_id, doc.doc_type, doc.sensitivity) == ("d1", "8-K", "internal")


def test_detect_doc_type_finds_form():
    assert detect_doc_type("UNITED STATES FORM 10-K ANNUAL REPORT") == "10-K"
    assert detect_doc_type("just some prose", default="text") == "text"


def test_detect_sensitivity_flags_internal_marker():
    assert detect_sensitivity("CONFIDENTIAL - internal use only") == "internal"
    assert detect_sensitivity("public filing text") == "public"


def test_source_from_bytes_html_vs_text():
    html_doc = source_from_bytes("filing.html", b"<p>TSMC fabricates chips.</p>")
    assert "<p>" not in html_doc.text and "TSMC fabricates chips." in html_doc.text
    txt_doc = source_from_bytes("note.txt", b"A plain analyst note about NVIDIA.")
    assert txt_doc.doc_type == "document"
    assert "analyst note" in txt_doc.text


def test_source_from_file_reads_disk(tmp_path):
    p = tmp_path / "memo.txt"
    p.write_text("Broadcom depends on TSMC for advanced ASICs.")
    doc = source_from_file(p)
    assert doc.source == "file"
    assert doc.doc_id == "memo"
    assert "Broadcom" in doc.text


def test_source_from_file_missing_raises(tmp_path):
    with pytest.raises(IngestSourceError):
        source_from_file(tmp_path / "nope.txt")


def test_source_from_edgar_requires_ref_offline():
    # No accession and no ticker+form: must fail fast with a clear error (no network touched).
    with pytest.raises(IngestSourceError):
        source_from_edgar()
