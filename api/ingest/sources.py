"""Source resolution — normalize any ingest input into a `SourceDoc`.

Three input shapes funnel into one `SourceDoc(doc_id, doc_type, sensitivity, text, ...)`:

  (a) an uploaded file (``.txt`` / ``.html``) — HTML-first: tags are stripped to text;
  (b) pasted ``text`` — used verbatim;
  (c) EDGAR by ``accession`` OR ``ticker`` + ``form`` via ``edgartools`` — HTML-first, with an
      optional Item-section filter for 10-K / 8-K (Items 1, 1A, 7).

Classification (``doc_type`` + ``sensitivity``) is DETERMINISTIC: explicit input wins, otherwise a
cheap keyword heuristic. No LLM call here — ingest cost stays bounded to the extraction step.

``edgartools`` / ``beautifulsoup4`` live in the optional ``ingest`` extra and are imported lazily,
so this module (and the rest of the pipeline) imports cleanly with only the base dependencies.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --- errors --------------------------------------------------------------------------------


class IngestSourceError(RuntimeError):
    """A source could not be resolved (missing file, bad EDGAR ref, missing optional extra)."""


# --- the normalized document ---------------------------------------------------------------


@dataclass
class SourceDoc:
    """A document normalized for the pipeline: identity, classification, and plain text."""

    doc_id: str
    doc_type: str
    sensitivity: str
    text: str
    title: str | None = None
    source: str = "text"  # "file" | "text" | "edgar"
    accession_no: str | None = None
    filing_date: str | None = None
    url: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def pages(self) -> int:
        """A coarse page estimate (~3k chars/page) for the `parsed` event / UI."""
        return max(1, round(self.chars / 3000))


# --- HTML → text ---------------------------------------------------------------------------

# Block-level tags after which we inject a paragraph break so the heading-aware chunker keeps its
# blank-line boundaries (it splits on blank lines and starts a new chunk at headings).
_BLOCK_TAGS = (
    "p", "div", "br", "li", "tr", "table", "section", "article", "blockquote",
    "h1", "h2", "h3", "h4", "h5", "h6",
)
_DROP_TAGS = ("script", "style", "head", "meta", "link", "noscript")


def _collapse(text: str) -> str:
    """Collapse whitespace but preserve blank-line paragraph boundaries."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    paras = re.split(r"\n\s*\n", text)
    out = [" ".join(p.split()) for p in paras]
    return "\n\n".join(p for p in out if p)


def strip_html(html: str) -> str:
    """Extract readable text from HTML, preserving paragraph breaks. bs4 with a regex fallback."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover - bs4 ships in the ingest extra
        # Regex fallback: drop script/style/head *content* first, then <br> -> breaks, then tags.
        dropped = re.sub(r"(?is)<(script|style|head|noscript)\b[^>]*>.*?</\1>", " ", html)
        # block-level tags become paragraph boundaries the chunker can split on
        dropped = re.sub(
            r"(?i)</?(p|div|h[1-6]|li|tr|section|article|ul|ol|table|br)\b[^>]*>", "\n\n", dropped
        )
        return _collapse(re.sub(r"<[^>]+>", " ", dropped))

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(list(_DROP_TAGS)):
        tag.decompose()
    for tag in soup.find_all(list(_BLOCK_TAGS)):
        tag.append("\n\n")
    return _collapse(soup.get_text(""))


def _looks_like_html(text: str) -> bool:
    head = text[:2048].lower()
    return "<html" in head or "<!doctype html" in head or "<body" in head or "<div" in head


def _pdf_to_text(data: bytes) -> str:
    """Extract a born-digital PDF's text layer with pypdf (no OCR). Scanned PDFs yield ''.

    Agent A (Retriever): the lightweight default. Scanned / image-only PDFs (no text layer) return
    empty and the caller raises, pointing at the opt-in `ingest-tables` OCR extra (docling).
    """
    import io

    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - pypdf is a base dependency
        raise IngestSourceError("PDF ingest needs pypdf") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - a corrupt PDF is a clean 400, not a 500
        raise IngestSourceError(f"could not read PDF: {str(exc)[:120]}") from exc
    parts: list[str] = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - skip an unparseable page, keep the rest
            continue
    return _collapse("\n\n".join(p for p in parts if p.strip()))


# --- deterministic classification ----------------------------------------------------------

_FORM_LABELS = (
    "10-K", "10-Q", "8-K", "20-F", "6-K", "40-F", "DEF 14A", "DEFA14A",
    "S-1", "424B", "N-PORT", "N-CSR", "485BPOS", "497",
)
_INTERNAL_MARKERS = (
    "internal use only", "confidential", "do not distribute", "internal note",
    "privileged", "(synthetic)",
)


def detect_doc_type(text: str, *, filename: str | None = None, default: str = "text") -> str:
    """Best-effort SEC form / doc-type detection from the first page of text or the filename."""
    head = text[:4000].upper()
    head_squashed = head.replace("-", "").replace(" ", "")
    for label in _FORM_LABELS:
        squashed = label.replace("-", "").replace(" ", "")
        if f"FORM {label}" in head or label in head or squashed in head_squashed:
            return label
    if filename:
        up = filename.upper()
        for label in _FORM_LABELS:
            if label in up:
                return label
    return default


def detect_sensitivity(text: str, *, filename: str | None = None, default: str = "public") -> str:
    """Flag `internal` when the text/filename carries a non-public marker; else the default."""
    hay = (text[:4000] + " " + (filename or "")).lower()
    if any(marker in hay for marker in _INTERNAL_MARKERS):
        return "internal"
    return default


# --- doc_id helpers ------------------------------------------------------------------------


def _slug(value: str, *, fallback: str = "doc") -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug[:64] or fallback


def _hash_id(text: str, *, prefix: str = "doc") -> str:
    return f"{prefix}_{hashlib.sha256(text.encode('utf-8')).hexdigest()[:10]}"


def _finalize(
    *,
    text: str,
    doc_id: str,
    doc_type: str | None,
    sensitivity: str | None,
    title: str | None,
    source: str,
    filename: str | None = None,
    default_type: str = "text",
    **extra: Any,
) -> SourceDoc:
    """Apply explicit overrides, else the deterministic heuristics, and assemble the SourceDoc."""
    resolved_type = doc_type or detect_doc_type(text, filename=filename, default=default_type)
    resolved_sens = sensitivity or detect_sensitivity(text, filename=filename)
    return SourceDoc(
        doc_id=doc_id,
        doc_type=resolved_type,
        sensitivity=resolved_sens,
        text=text,
        title=title,
        source=source,
        **extra,
    )


# --- (b) pasted text -----------------------------------------------------------------------


def source_from_text(
    text: str,
    *,
    doc_id: str | None = None,
    doc_type: str | None = None,
    sensitivity: str | None = None,
    title: str | None = None,
) -> SourceDoc:
    """Normalize pasted text. HTML-looking input is stripped to plain text first."""
    if not text or not text.strip():
        raise IngestSourceError("empty text input")
    body = strip_html(text) if _looks_like_html(text) else _collapse(text)
    return _finalize(
        text=body,
        doc_id=doc_id or _hash_id(body),
        doc_type=doc_type,
        sensitivity=sensitivity,
        title=title,
        source="text",
        default_type="text",
    )


# --- (a) uploaded file ---------------------------------------------------------------------


def source_from_bytes(
    filename: str,
    data: bytes,
    *,
    doc_id: str | None = None,
    doc_type: str | None = None,
    sensitivity: str | None = None,
    title: str | None = None,
) -> SourceDoc:
    """Normalize an uploaded file's bytes. PDF (born-digital text) → HTML → plain text."""
    name = filename or "upload"
    if name.lower().endswith(".pdf") or data[:5] == b"%PDF-":
        body = _pdf_to_text(data)
        if not body.strip():
            raise IngestSourceError(
                f"no extractable text layer in {name}; scanned PDFs need the ingest-tables OCR extra"
            )
    else:
        raw = data.decode("utf-8", errors="replace")
        is_html = name.lower().endswith((".html", ".htm")) or _looks_like_html(raw)
        body = strip_html(raw) if is_html else _collapse(raw)
        if not body.strip():
            raise IngestSourceError(f"no extractable text in {name}")
    stem = Path(name).stem or "document"
    return _finalize(
        text=body,
        doc_id=doc_id or _slug(stem),
        doc_type=doc_type,
        sensitivity=sensitivity,
        title=title or stem,
        source="file",
        filename=name,
        default_type="document",
    )


def source_from_file(
    path: str | Path,
    *,
    doc_id: str | None = None,
    doc_type: str | None = None,
    sensitivity: str | None = None,
    title: str | None = None,
) -> SourceDoc:
    """Read a ``.txt`` / ``.html`` file from disk and normalize it."""
    p = Path(path).expanduser()
    if not p.exists():
        raise IngestSourceError(f"file not found: {p}")
    return source_from_bytes(
        p.name,
        p.read_bytes(),
        doc_id=doc_id,
        doc_type=doc_type,
        sensitivity=sensitivity,
        title=title,
    )


# --- (c) EDGAR -----------------------------------------------------------------------------

# 10-K / 8-K item headers we can slice to when a caller asks for a section subset.
_ITEM_ALIASES = {
    "1": ("item 1.", "item 1:", "item 1 "),
    "1a": ("item 1a",),
    "7": ("item 7.", "item 7:", "item 7 "),
}


def _filter_sections(text: str, sections: list[str]) -> str | None:
    """Slice out requested Item sections (e.g. ['1', '1A', '7']); None if none were located."""
    wanted = {s.lower().replace("item", "").strip() for s in sections}
    lines = text.splitlines()
    marks: list[tuple[int, str]] = []
    for i, ln in enumerate(lines):
        low = ln.strip().lower()
        for key, aliases in _ITEM_ALIASES.items():
            if any(low.startswith(a) for a in aliases):
                marks.append((i, key))
    if not marks:
        return None
    chunks: list[str] = []
    for idx, (line_no, key) in enumerate(marks):
        if key not in wanted:
            continue
        end = marks[idx + 1][0] if idx + 1 < len(marks) else len(lines)
        chunks.append("\n".join(lines[line_no:end]).strip())
    joined = "\n\n".join(c for c in chunks if c)
    return joined or None


def source_from_edgar(
    *,
    accession: str | None = None,
    ticker: str | None = None,
    form: str | None = None,
    sections: list[str] | None = None,
    doc_id: str | None = None,
    doc_type: str | None = None,
    sensitivity: str | None = None,
    user_agent: str | None = None,
) -> SourceDoc:
    """Pull a filing from EDGAR (HTML-first) via ``edgartools``.

    Requires the optional ``ingest`` extra. Provide either ``accession`` or ``ticker`` + ``form``.
    """
    try:
        import edgar
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise IngestSourceError(
            "EDGAR ingest needs the optional extra: `uv sync --extra ingest`"
        ) from exc

    from ..config import get_settings

    edgar.set_identity(user_agent or get_settings().edgar_user_agent)

    if accession:
        filing = edgar.get_by_accession_number(accession)
        if filing is None:
            raise IngestSourceError(f"no EDGAR filing for accession {accession}")
    elif ticker and form:
        filings = edgar.Company(ticker).get_filings(form=form)
        filing = filings.latest() if filings is not None else None
        if filing is None:
            raise IngestSourceError(f"no {form} filing found for {ticker}")
    else:
        raise IngestSourceError("EDGAR ingest needs `accession` or (`ticker` and `form`)")

    text = ""
    try:
        html = filing.html()
        if html:
            text = strip_html(html)
    except Exception:  # noqa: BLE001 - fall back to plain text extraction
        text = ""
    if not text:
        text = _collapse(str(filing.text()))
    if not text.strip():
        raise IngestSourceError("EDGAR filing yielded no extractable text")

    resolved_form = doc_type or str(getattr(filing, "form", None) or form or "filing")
    if sections and resolved_form.upper() in {"10-K", "8-K", "10-Q"}:
        filtered = _filter_sections(text, sections)
        if filtered:
            text = filtered

    acc = str(getattr(filing, "accession_no", None) or accession or "")
    company = str(getattr(filing, "company", None) or ticker or "")
    filing_date = str(getattr(filing, "filing_date", "") or "")
    return _finalize(
        text=text,
        doc_id=doc_id or _slug(acc or f"{ticker}_{resolved_form}"),
        doc_type=resolved_form,
        sensitivity=sensitivity,
        title=f"{company} {resolved_form}".strip(),
        source="edgar",
        default_type=resolved_form,
        accession_no=acc or None,
        filing_date=filing_date or None,
        url=str(getattr(filing, "filing_url", "") or "") or None,
        meta={"company": company, "form": resolved_form},
    )


def cik_from_name(name: str, *, user_agent: str | None = None) -> str | None:
    """Best-effort: resolve a company NAME to its EDGAR CIK via ``edgartools``' company search.

    Returns the top hit's CIK (as a string) or ``None``. Enrichment uses this to fetch a holding's
    10-K when N-PORT gave no usable ticker (the common case — N-PORT identifies holdings by name +
    CUSIP/LEI, not ticker). Requires the optional ``ingest`` extra; ANY failure (missing extra,
    unknown/ambiguous name, empty results, network hiccup) degrades to ``None`` and never raises, so
    the caller's best-effort seam simply skips a holding it cannot resolve.
    """
    query = (name or "").strip()
    if not query:
        return None
    try:
        import edgar
    except ImportError:  # pragma: no cover - exercised only without the extra
        return None

    from ..config import get_settings

    try:
        edgar.set_identity(user_agent or get_settings().edgar_user_agent)
        results = edgar.find_company(query)
        top = results[0]  # CompanySearchResults is indexable; IndexError when there is no hit
        cik = getattr(top, "cik", None)
    except Exception:  # noqa: BLE001 - best-effort resolution; any failure → skip this holding
        return None
    return str(cik) if cik else None
