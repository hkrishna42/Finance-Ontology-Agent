"""EDGAR + GLEIF discovery — firm name → ranked onboarding candidates, and NPORT-P XML fetch.

Two deterministic, network-only entry points (no Anthropic key needed):

  * ``search_firms(query)`` merges EDGAR fund/company search (``edgartools``: fund families with
    their series list, plus operating-company hits) with a fuzzy GLEIF legal-name lookup for the
    LEI, into ranked, de-duplicated ``FirmCandidate`` dicts. **Best-effort per source**: if EDGAR
    *or* GLEIF errors/times out, the other source's results are still returned — a partial failure
    is logged and swallowed, never raised to the caller.
  * ``fetch_series_nport_xml(series_id)`` resolves a fund series' latest **NPORT-P** filing and
    returns its primary XML string (what ``api.l2.nport.parse_nport`` consumes), or ``None``.

``edgartools`` needs a declared SEC User-Agent — we call ``edgar.set_identity(...)`` once before any
call and throttle requests under SEC's ~8 req/s fair-use ceiling. ``edgar`` is imported lazily inside
the functions (it lives in the optional ``ingest`` extra) so this module imports without it, and so
tests can monkeypatch the ``edgar`` module functions to stay fully offline.
"""

from __future__ import annotations

import logging
import re
import time
from difflib import SequenceMatcher
from typing import Any, TypedDict

from ..config import get_settings
from ..resolution.gleif import GleifClient

log = logging.getLogger("api.onboarding")


class FirmCandidate(TypedDict):
    """One onboarding candidate. ``series`` may be ``[]`` for a bare GLEIF/company hit."""

    name: str
    cik: str | None
    lei: str | None
    source: str  # "edgar" | "gleif"
    kind: str  # "fund_family" | "company"
    series: list[dict[str, Any]]  # [{series_id, name, class_tickers: [str]}]
    score: float


# --- SEC fair-use throttle + backoff -------------------------------------------------------
# SEC allows ~10 req/s; we stay well under. All network waits route through these two seams so
# offline tests can monkeypatch them to no-ops (keeping the mocked pytest suite fast).

SEC_RATE_LIMIT_DELAY = 0.12  # min seconds between EDGAR requests (~8 req/s)
SEC_BACKOFF_BASE = 0.5  # backoff seconds between retries of a transient EDGAR call

_last_call = 0.0


def _throttle() -> None:
    """Sleep just enough to keep successive EDGAR calls under the SEC rate ceiling."""
    global _last_call
    wait = SEC_RATE_LIMIT_DELAY - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def _backoff(attempt: int) -> None:
    """Linear backoff sleep before retry ``attempt`` (1-based)."""
    time.sleep(SEC_BACKOFF_BASE * attempt)


_identity_set = False


def _ensure_identity(settings: Any) -> None:
    """Declare the SEC User-Agent for ``edgartools`` once per process (idempotent, offline)."""
    global _identity_set
    if _identity_set:
        return
    import edgar

    edgar.set_identity((settings or get_settings()).edgar_user_agent)
    _identity_set = True


# --- candidate construction / scoring ------------------------------------------------------


def _make_candidate(
    *,
    name: str,
    source: str,
    kind: str,
    cik: str | None = None,
    lei: str | None = None,
    series: list[dict[str, Any]] | None = None,
) -> FirmCandidate:
    return FirmCandidate(
        name=name,
        cik=cik,
        lei=lei,
        source=source,
        kind=kind,
        series=series if series is not None else [],
        score=0.0,
    )


def _score(query: str, cand: FirmCandidate) -> float:
    """Deterministic rank score: name similarity + boosts for onboardable/identified hits."""
    q = query.lower().strip()
    name = (cand.get("name") or "").lower()
    base = SequenceMatcher(None, q, name).ratio() if name else 0.0
    if q and name and q in name:
        base = max(base, 0.6) + 0.15  # strong signal: query is a substring of the name
    if cand.get("kind") == "fund_family" and cand.get("series"):
        base += 0.15  # onboardable (has NPORT series) → rank first
    if cand.get("source") == "edgar":
        base += 0.05
    if cand.get("cik"):
        base += 0.03
    if cand.get("lei"):
        base += 0.02
    return round(base, 4)


def _names_match(a: str, b: str) -> bool:
    """True when two firm names are the same entity (substring or high fuzzy ratio)."""
    a, b = a.lower().strip(), b.lower().strip()
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.85


def _best_name_match(cands: list[FirmCandidate], name: str) -> FirmCandidate | None:
    for c in cands:
        if _names_match(c.get("name") or "", name):
            return c
    return None


def _merge_candidate(a: FirmCandidate, b: FirmCandidate) -> FirmCandidate:
    """Fold duplicate ``b`` into ``a``: fill missing ids, union series, keep the richer kind."""
    out = dict(a)
    out["name"] = a.get("name") or b.get("name")
    out["cik"] = a.get("cik") or b.get("cik")
    out["lei"] = a.get("lei") or b.get("lei")
    series = list(a.get("series") or [])
    seen = {s.get("series_id") for s in series}
    for s in b.get("series") or []:
        if s.get("series_id") not in seen:
            series.append(s)
            seen.add(s.get("series_id"))
    out["series"] = series
    if "fund_family" in (a.get("kind"), b.get("kind")):
        out["kind"] = "fund_family"
    out["source"] = "edgar" if "edgar" in (a.get("source"), b.get("source")) else a.get("source")
    out["score"] = max(a.get("score", 0.0), b.get("score", 0.0))
    return out  # type: ignore[return-value]


def _dedupe_and_rank(cands: list[FirmCandidate]) -> list[FirmCandidate]:
    """Dedupe by (cik or lowercased name), merging duplicates, then sort by score desc."""
    merged: dict[str, FirmCandidate] = {}
    for c in cands:
        cik = c.get("cik")
        key = f"cik:{cik}" if cik else f"name:{(c.get('name') or '').strip().lower()}"
        merged[key] = _merge_candidate(merged[key], c) if key in merged else c
    return sorted(merged.values(), key=lambda c: (-c.get("score", 0.0), c.get("name") or ""))


# --- EDGAR side ----------------------------------------------------------------------------


def _edgar_fund_families(query: str) -> list[FirmCandidate]:
    """Fund families (grouped by CIK) with their series list + per-series class tickers."""
    import edgar

    _throttle()
    series_records = edgar.find_funds(query, search_type="series") or []

    # class tickers keyed by series_id (best-effort)
    tickers_by_series: dict[str, list[str]] = {}
    try:
        _throttle()
        for c in edgar.find_funds(query, search_type="class") or []:
            sid, ticker = getattr(c, "series_id", None), getattr(c, "ticker", None)
            if sid and ticker:
                tickers_by_series.setdefault(sid, [])
                if ticker not in tickers_by_series[sid]:
                    tickers_by_series[sid].append(ticker)
    except Exception:  # noqa: BLE001 - class enrichment is optional
        log.debug("EDGAR class lookup failed for %r", query, exc_info=True)

    # fund-family (company) names keyed by CIK (best-effort)
    names_by_cik: dict[str, str] = {}
    try:
        _throttle()
        for co in edgar.find_funds(query, search_type="company") or []:
            cik = str(getattr(co, "cik", "") or "")
            if cik and getattr(co, "name", None):
                names_by_cik[cik] = co.name
    except Exception:  # noqa: BLE001 - name enrichment is optional
        log.debug("EDGAR company lookup failed for %r", query, exc_info=True)

    families: dict[str, FirmCandidate] = {}
    for rec in series_records:
        sid = getattr(rec, "series_id", None)
        if not sid:
            continue
        cik = str(getattr(rec, "cik", "") or "")
        sname = getattr(rec, "name", None) or sid
        fam = families.get(cik)
        if fam is None:
            fam = _make_candidate(
                name=names_by_cik.get(cik) or sname,
                source="edgar",
                kind="fund_family",
                cik=cik or None,
            )
            families[cik] = fam
        fam["series"].append(
            {"series_id": sid, "name": sname, "class_tickers": tickers_by_series.get(sid, [])}
        )
    return list(families.values())


def _edgar_companies(query: str) -> list[FirmCandidate]:
    """Operating-company hits (name/CIK/ticker) via EDGAR company search — no NPORT series."""
    import edgar

    _throttle()
    results = edgar.find_company(query)
    if results is None or getattr(results, "empty", True):
        return []
    out: list[FirmCandidate] = []
    for row in results.results.itertuples(index=False):
        name = str(getattr(row, "company", "") or "").strip()
        if not name:
            continue
        cik = getattr(row, "cik", None)
        out.append(
            _make_candidate(
                name=name,
                source="edgar",
                kind="company",
                cik=str(int(cik)) if cik is not None else None,
            )
        )
    return out


def _edgar_candidates(query: str, *, settings: Any) -> list[FirmCandidate]:
    _ensure_identity(settings)
    out = _edgar_fund_families(query)
    try:
        out.extend(_edgar_companies(query))
    except Exception:  # noqa: BLE001 - company search is a secondary source
        log.debug("EDGAR company search failed for %r", query, exc_info=True)
    return out


# --- GLEIF side ----------------------------------------------------------------------------


def _gleif_hits(query: str, *, limit: int) -> list[dict[str, Any]]:
    """Fuzzy GLEIF legal-name search → ``[{lei, name}]`` (network-guarded inside the client)."""
    client = GleifClient()
    try:
        return client.search(query, limit=limit)
    finally:
        client.close()


def _merge_gleif(cands: list[FirmCandidate], query: str, *, limit: int) -> None:
    """Attach LEIs to matching EDGAR hits; add unmatched GLEIF records as company candidates."""
    for hit in _gleif_hits(query, limit=limit):
        lei = hit.get("lei")
        if not lei:
            continue
        name = hit.get("name") or ""
        match = _best_name_match(cands, name) if name else None
        if match is not None:
            if not match.get("lei"):
                match["lei"] = lei
        else:
            cands.append(
                _make_candidate(name=name or query, source="gleif", kind="company", lei=lei)
            )


# --- public API ----------------------------------------------------------------------------


def search_firms(query: str, *, limit: int = 8, settings: Any = None) -> list[FirmCandidate]:
    """Firm name → ranked onboarding candidates (EDGAR fund families/companies + GLEIF LEI).

    Best-effort per source: a failure in EDGAR *or* GLEIF is logged and the other source's
    results are still returned. Never raises for a partial failure. Returns ``[]`` for a blank
    query. Results are de-duplicated by (CIK or name) and sorted by score (desc), capped at
    ``limit``.
    """
    q = (query or "").strip()
    if not q:
        return []
    settings = settings or get_settings()

    cands: list[FirmCandidate] = []
    try:
        cands.extend(_edgar_candidates(q, settings=settings))
    except Exception:  # noqa: BLE001 - degrade to GLEIF-only on EDGAR failure
        log.warning("EDGAR discovery failed for %r; continuing GLEIF-only", q, exc_info=True)
    try:
        _merge_gleif(cands, q, limit=limit)
    except Exception:  # noqa: BLE001 - degrade to EDGAR-only on GLEIF failure
        log.warning("GLEIF discovery failed for %r; continuing EDGAR-only", q, exc_info=True)

    for c in cands:
        c["score"] = _score(q, c)
    return _dedupe_and_rank(cands)[:limit]


_SERIES_ID_RE = re.compile(r"<\s*seriesId\s*>\s*(S\d+)\s*<", re.IGNORECASE)


def _xml_is_series(xml: str, series_id: str) -> bool:
    """True unless the XML's own ``<seriesId>`` positively names a *different* series.

    Best-effort: an absent/unparseable series id does not reject the document — we only guard
    against the known sibling-series hazard, never drop a valid filing on an unexpected format.
    """
    match = _SERIES_ID_RE.search(xml)
    return match is None or match.group(1).upper() == series_id.upper()


def fetch_series_nport_xml(series_id: str, *, settings: Any = None) -> str | None:
    """Latest **NPORT-P** primary XML for a fund series, or ``None`` if none is found.

    Series-scoped via ``edgar.Fund(series_id).get_filings(series_only=True, form="NPORT-P")`` — we
    deliberately DO NOT fall back to the umbrella trust's filing list: for a multi-series trust its
    ``.latest()`` is a *sibling* series' report (GH #888), and onboarding keys a Fund on the parsed
    ``seriesId``, so a sibling would corrupt the graph. An empty series scope therefore means this
    series files no NPORT-P (e.g. a money-market series that files N-MFP instead) → ``None``. As a
    final guard the returned primary doc's own ``seriesId`` is confirmed to match. Network failures
    are retried once (backoff) then swallowed → ``None``. The returned string is exactly what
    ``api.l2.nport.parse_nport`` consumes.
    """
    sid = (series_id or "").strip()
    if not sid:
        return None
    settings = settings or get_settings()

    for attempt in range(2):
        try:
            import edgar

            _ensure_identity(settings)
            _throttle()
            fund = edgar.Fund(sid)

            filings = fund.get_filings(series_only=True, form="NPORT-P")
            if not filings or len(filings) == 0:
                return None

            latest = filings.latest() if hasattr(filings, "latest") else None
            if latest is None and len(filings) > 0:
                latest = filings[0]
            if latest is None:
                return None

            _throttle()
            xml = latest.xml()
            if not xml:
                return None
            if not _xml_is_series(xml, sid):
                log.warning("NPORT primary doc for %s named a different series; discarding", sid)
                return None
            return xml
        except Exception:  # noqa: BLE001 - transient EDGAR/network error
            if attempt == 0:
                log.debug("NPORT fetch retry for series %s", sid, exc_info=True)
                _backoff(attempt + 1)
                continue
            log.warning("NPORT fetch failed for series %s", sid, exc_info=True)
            return None
    return None
