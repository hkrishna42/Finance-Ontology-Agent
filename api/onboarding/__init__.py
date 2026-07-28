"""Firm onboarding — turn a firm name into onboarding candidates and fetch their filings.

The discovery layer (`discovery.py`) is DETERMINISTIC and network-only (no Anthropic key): it
merges EDGAR fund/company search (via `edgartools`) with a fuzzy GLEIF LEI lookup into ranked
`FirmCandidate` dicts, and resolves a fund series' latest NPORT-P primary XML for the L2 parser.
"""

from __future__ import annotations

from .discovery import FirmCandidate, fetch_series_nport_xml, search_firms

__all__ = ["FirmCandidate", "fetch_series_nport_xml", "search_firms"]
