"""PDF ingest via pypdf — born-digital text extraction (offline; no OCR)."""

from __future__ import annotations

import pytest

from api.ingest.sources import IngestSourceError, source_from_bytes


def _make_pdf(text: str) -> bytes:
    """A minimal, valid single-page PDF with an extractable text layer (offsets computed here)."""
    stream = b"BT /F1 18 Tf 72 700 Td (" + text.encode("latin-1") + b") Tj ET"
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length " + str(len(stream)).encode() + b">>stream\n" + stream + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += str(i).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 " + str(len(objs) + 1).encode() + b"\n0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (b"trailer\n<</Size " + str(len(objs) + 1).encode() + b"/Root 1 0 R>>\n"
            b"startxref\n" + str(xref_at).encode() + b"\n%%EOF")
    return out


def test_born_digital_pdf_text_is_extracted():
    doc = source_from_bytes("report.pdf", _make_pdf("Acme Corp supply chain concentration risk."))
    assert "Acme Corp" in doc.text and "supply chain" in doc.text
    assert doc.source == "file"


def test_pdf_detected_by_magic_without_extension():
    doc = source_from_bytes("upload", _make_pdf("Detected by the PDF magic header."))
    assert "magic header" in doc.text


def test_scanned_pdf_without_text_raises_pointing_to_ocr():
    with pytest.raises(IngestSourceError) as ei:
        source_from_bytes("scan.pdf", _make_pdf(""))
    assert "OCR" in str(ei.value)
