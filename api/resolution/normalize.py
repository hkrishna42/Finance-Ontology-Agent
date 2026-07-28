"""Issuer-name normalization — the deterministic front of the resolution pipeline.

`normalize()` folds the surface variants of a corporate name onto one comparable key by lowercasing,
dropping punctuation, and stripping common legal/organizational suffixes (Inc, Corp, Co, Ltd,
Holding(s), PLC, NV, SA, AG, ADR, …). It is intentionally conservative — it never merges distinct
issuers, it just removes noise so `"NVIDIA"`, `"NVIDIA Corp"`, and `"NVIDIA CORPORATION"` share a key.
"""

from __future__ import annotations

import re

# Legal-form / descriptor tokens that carry no disambiguating signal. Longest-first so multi-word
# forms are removed before their single-word constituents.
_SUFFIX_TOKENS: tuple[str, ...] = (
    "incorporated",
    "corporation",
    "companies",
    "company",
    "holdings",
    "holding",
    "limited",
    "group",
    "corp",
    "inc",
    "co",
    "ltd",
    "llc",
    "lp",
    "plc",
    "nv",
    "sa",
    "ag",
    "se",
    "adr",
    "ads",
    "cl",
    "class",
)

# Connector stopwords dropped so "Eli Lilly", "Eli Lilly & Co", and "Eli Lilly and Company" all
# fold to the same key.
_STOPWORDS: tuple[str, ...] = ("and", "the", "of")

_PUNCT = re.compile(r"[^a-z0-9\s]")
_WS = re.compile(r"\s+")
_SUFFIX_RE = re.compile(r"\b(" + "|".join(_SUFFIX_TOKENS + _STOPWORDS) + r")\b")


def normalize(name: str) -> str:
    """Return the normalized comparison key for a company name (may be empty for junk input)."""
    if not name:
        return ""
    s = name.lower().strip()
    s = s.replace("&", " ")
    s = s.replace(".", " ")
    s = _PUNCT.sub(" ", s)
    s = _SUFFIX_RE.sub(" ", s)
    s = _WS.sub(" ", s).strip()
    return s


def normalize_ticker(ticker: str | None) -> str | None:
    """Uppercase + trim a ticker; map dot/dash class separators to a canonical dash. None-safe."""
    if not ticker:
        return None
    t = ticker.strip().upper().replace(".", "-")
    return t or None
