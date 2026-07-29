"""Ontology-driven entity matching (blocking + scoring) across source records.

Composes existing seams — `api/resolution/normalize.py::normalize` for name/address normalization and
`rapidfuzz` (a base dep) for fuzzy similarity — rather than a parallel resolver. Produces a match
confidence for a cluster of source records that refer to the same real-world entity. A shared strong
identifier (LEI) or a normalized identifier crosswalk is decisive; otherwise fuzzy name + normalized
address carry the score.
"""

from __future__ import annotations

import re
from typing import Any

from rapidfuzz import fuzz

from ..resolution.normalize import normalize
from .models import MatchResult


def _norm_id(value: Any) -> str:
    """Normalize an identifier for crosswalk comparison: PROP-1001 == PROP1001 -> 'prop1001'."""
    text = re.sub(r"[^a-z0-9]+", "", str(value or "").lower())
    return "" if text in ("", "notstated", "na", "none") else text


def _features(record: dict[str, Any], name_field: str, id_field: str | None,
              strong_ids: tuple[str, ...]) -> dict[str, Any]:
    attrs = record.get("attributes", {})
    return {
        "name": normalize(str(attrs.get(name_field) or "")),
        "addr": normalize(str(attrs.get("address") or "")),
        "id": _norm_id(attrs.get(id_field)) if id_field else "",
        "strong": {str(attrs.get(f)).strip().upper() for f in strong_ids if attrs.get(f)},
    }


def _pair_score(a: dict[str, Any], b: dict[str, Any]) -> float:
    sims: list[float] = []
    if a["name"] and b["name"]:
        sims.append(fuzz.token_set_ratio(a["name"], b["name"]) / 100.0)
    if a["addr"] and b["addr"]:
        sims.append(fuzz.token_set_ratio(a["addr"], b["addr"]) / 100.0)
    if (a["strong"] and b["strong"] and a["strong"] & b["strong"]) or (
        a["id"] and a["id"] == b["id"]
    ):
        sims.append(1.0)  # a shared LEI / crosswalked identifier is decisive
    if not sims:
        return 0.0
    # Real MDM confirms a match on its single strongest signal; the mean tempers it slightly.
    return max(sims) * 0.75 + (sum(sims) / len(sims)) * 0.25


def match(
    records: list[dict[str, Any]],
    *,
    name_field: str,
    id_field: str | None = None,
    strong_ids: tuple[str, ...] = (),
    threshold: float = 0.88,
    blocking: list[str] | None = None,
) -> MatchResult:
    """Score a cluster of source records as the same entity → a `MatchResult` (confidence %)."""
    feats = [_features(r, name_field, id_field, strong_ids) for r in records]
    pairs = [
        _pair_score(feats[i], feats[j])
        for i in range(len(feats))
        for j in range(i + 1, len(feats))
    ]
    confidence = round(sum(pairs) / len(pairs), 3) if pairs else 1.0
    keys = blocking or [k for k in (id_field, "address", name_field) if k]
    return MatchResult(
        confidence=confidence,
        threshold=threshold,
        resolved=confidence >= threshold,
        matched_record_ids=[r["record_id"] for r in records],
        blocking_keys=keys,
        method="blocking on identifier + address normalization + fuzzy name similarity",
    )
