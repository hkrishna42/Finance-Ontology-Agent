"""Deterministic Form N-PORT-P parser — the L2 holdings path (no LLM).

Form N-PORT-P is a registered fund's monthly portfolio-holdings report. We parse it with a plain,
auditable XML walk (stdlib `xml.etree`) into:

  * one `Fund` (series identity: name, series_id, cik, lei), and
  * one weighted `HOLDS` edge per position, carrying
    `method="structured_parse", confidence=1.0, source, as_of, weight_pct, value_usd`
    (and `shares`, plus the security identifiers we can read: ticker/cusip/lei).

Because `HOLDS` is a *structural* relation (`extractable=False` in the ontology), it is written by
this code, never by the extraction agent — the first line of the reliability story.

Namespace-robust: real N-PORT uses the `http://www.sec.gov/edgar/nport` namespace, but we match on
each element's *local* name so the parser also accepts namespace-free fixtures. Fully offline and
deterministic (byte-in → rows-out); no network, no model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

HOLDS_METHOD = "structured_parse"
HOLDS_CONFIDENCE = 1.0
DEFAULT_SOURCE = "NPORT-P"


# --- data model ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Holding:
    """One parsed portfolio position."""

    name: str
    weight_pct: float
    value_usd: float
    ticker: str | None = None
    cusip: str | None = None
    lei: str | None = None
    shares: float | None = None


@dataclass(frozen=True)
class NportFiling:
    """A parsed N-PORT-P filing: fund identity + its holdings."""

    fund_name: str
    series_id: str
    cik: str
    lei: str | None
    as_of: str
    holdings: tuple[Holding, ...] = field(default_factory=tuple)

    @property
    def total_pct(self) -> float:
        return round(sum(h.weight_pct for h in self.holdings), 6)

    @property
    def total_value_usd(self) -> float:
        return round(sum(h.value_usd for h in self.holdings), 2)


# --- XML helpers (namespace-agnostic) ------------------------------------------------------


def _local(tag: str) -> str:
    """Strip any `{namespace}` prefix from an element tag → its local name."""
    return tag.rsplit("}", 1)[-1]


def _find(elem: ET.Element, name: str) -> ET.Element | None:
    """First descendant whose local tag == `name` (namespace-agnostic)."""
    if _local(elem.tag) == name:
        return elem
    for child in elem.iter():
        if _local(child.tag) == name:
            return child
    return None


def _text(elem: ET.Element | None, name: str) -> str | None:
    """Text of the first descendant named `name`, stripped; None if absent/empty."""
    if elem is None:
        return None
    target = _find(elem, name)
    if target is None or target.text is None:
        return None
    t = target.text.strip()
    return t or None


def _children_named(elem: ET.Element, name: str) -> list[ET.Element]:
    """All descendants whose local tag == `name`."""
    return [c for c in elem.iter() if _local(c.tag) == name]


def _float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.replace(",", ""))
    except (TypeError, ValueError):
        return None


# --- parsing -------------------------------------------------------------------------------


def _parse_ticker(sec: ET.Element) -> str | None:
    """Read the security ticker from `<identifiers><ticker value="..."/></identifiers>`."""
    for t in _children_named(sec, "ticker"):
        val = t.get("value") or (t.text.strip() if t.text else None)
        if val:
            return val
    return None


def _parse_holding(sec: ET.Element) -> Holding | None:
    name = _text(sec, "name")
    pct = _float(_text(sec, "pctVal"))
    val = _float(_text(sec, "valUSD"))
    if not name or pct is None or val is None:
        # A position must have an issuer name and both weight + value to be a valid HOLDS.
        return None
    return Holding(
        name=name,
        weight_pct=pct,
        value_usd=val,
        ticker=_parse_ticker(sec),
        cusip=_text(sec, "cusip"),
        lei=_text(sec, "lei"),
        shares=_float(_text(sec, "balance")),
    )


def parse_nport(source: str | Path) -> NportFiling:
    """Parse an N-PORT-P document (path or XML string) into an `NportFiling`.

    Deterministic and offline. Raises `ValueError` if the fund identity (series id) is missing.
    """
    if isinstance(source, Path) or (isinstance(source, str) and "<" not in source):
        root = ET.parse(str(source)).getroot()
    else:
        root = ET.fromstring(source)

    gen = _find(root, "genInfo")
    series_id = _text(gen, "seriesId") if gen is not None else None
    if not series_id:
        # fall back to the header identity block
        series_id = _text(root, "seriesId")
    if not series_id:
        raise ValueError("N-PORT parse error: no seriesId found (cannot identify the fund)")

    fund_name = (_text(gen, "seriesName") if gen is not None else None) or series_id
    cik = (_text(gen, "regCik") if gen is not None else None) or _text(root, "cik") or ""
    lei = _text(gen, "seriesLei") if gen is not None else None
    as_of = (
        (_text(gen, "repPdEnd") if gen is not None else None)
        or (_text(gen, "repPdDate") if gen is not None else None)
        or ""
    )

    secs = _children_named(root, "invstOrSec")
    holdings = tuple(h for h in (_parse_holding(s) for s in secs) if h is not None)

    return NportFiling(
        fund_name=fund_name,
        series_id=series_id,
        cik=cik,
        lei=lei,
        as_of=as_of,
        holdings=holdings,
    )


# --- graph write plan ----------------------------------------------------------------------


def holds_rows(filing: NportFiling, *, source: str = DEFAULT_SOURCE) -> list[dict[str, Any]]:
    """Flatten a filing into HOLDS row dicts (one per position) for inspection/writes."""
    rows: list[dict[str, Any]] = []
    for h in filing.holdings:
        rows.append(
            {
                "fund_name": filing.fund_name,
                "company_name": h.name,
                "ticker": h.ticker,
                "cusip": h.cusip,
                "lei": h.lei,
                "weight_pct": h.weight_pct,
                "value_usd": h.value_usd,
                "shares": h.shares,
                "as_of": filing.as_of,
                "source": source,
                "method": HOLDS_METHOD,
                "confidence": HOLDS_CONFIDENCE,
            }
        )
    return rows


# Cypher: MERGE the Fund on its key (name), MERGE each issuer Company on name (attaching the
# ticker/cusip we parsed), and MERGE the weighted HOLDS edge with structured-parse provenance.
_FUND_MERGE = (
    "MERGE (f:Fund {name: $fund_name}) "
    "SET f.series_id = $series_id, f.cik = $cik, f.lei = coalesce($lei, f.lei)"
)

_HOLDS_MERGE = (
    "MATCH (f:Fund {name: $fund_name}) "
    "MERGE (co:Company {name: $company_name}) "
    "  ON CREATE SET co.ticker = $ticker "
    "SET co.ticker = coalesce(co.ticker, $ticker), co.lei = coalesce(co.lei, $lei) "
    "MERGE (f)-[r:HOLDS]->(co) "
    "SET r.weight_pct = $weight_pct, r.value_usd = $value_usd, r.shares = $shares, "
    "    r.as_of = $as_of, r.source = $source, r.method = $method, r.confidence = $confidence"
)


def cypher_writes(
    filing: NportFiling, *, source: str = DEFAULT_SOURCE
) -> list[tuple[str, dict[str, Any]]]:
    """Return an ordered (query, params) plan that writes the Fund + weighted HOLDS edges.

    Idempotent (all MERGE). Kept as data so it can be inspected/tested without a live database.
    """
    plan: list[tuple[str, dict[str, Any]]] = [
        (
            _FUND_MERGE,
            {
                "fund_name": filing.fund_name,
                "series_id": filing.series_id,
                "cik": filing.cik,
                "lei": filing.lei,
            },
        )
    ]
    for row in holds_rows(filing, source=source):
        plan.append((_HOLDS_MERGE, row))
    return plan


def write(store: Any, filing: NportFiling, *, source: str = DEFAULT_SOURCE) -> int:
    """Execute the write plan against a `Neo4jStore` (or anything with `.run(query, **params)`).

    Returns the number of HOLDS edges written (== number of holdings).
    """
    for query, params in cypher_writes(filing, source=source):
        store.run(query, **params)
    return len(filing.holdings)


def load_and_write(store: Any, path: str | Path, *, source: str | None = None) -> NportFiling:
    """Convenience: parse an N-PORT file at `path` and write it. `source` defaults to the filename."""
    filing = parse_nport(path)
    src = source or f"{DEFAULT_SOURCE}:{Path(path).name}"
    write(store, filing, source=src)
    return filing
