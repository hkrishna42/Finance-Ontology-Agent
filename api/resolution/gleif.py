"""GLEIF LEI enrichment — map a resolved issuer name to its Legal Entity Identifier.

`GleifClient` calls the public GLEIF v1 API (`https://api.gleif.org/api/v1/lei-records`) over
`httpx`. The `httpx.Client` is injectable, so offline tests pass an `httpx.MockTransport` and never
touch the network. `StaticGleifClient` is an even simpler dict-backed stand-in for tests that don't
care about the HTTP shape. Both satisfy the `Gleif` protocol the resolver depends on.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import httpx

GLEIF_BASE_URL = "https://api.gleif.org/api/v1"
_HEADERS = {"Accept": "application/vnd.api+json"}


@runtime_checkable
class Gleif(Protocol):
    def lei_for(self, name: str, *, cik: str | None = None) -> str | None: ...


def _lei_from_record(record: dict[str, Any]) -> str | None:
    """LEI of a single GLEIF record: `attributes.lei`, else the record `id`."""
    if not isinstance(record, dict):
        return None
    attrs = record.get("attributes", {})
    lei = attrs.get("lei") if isinstance(attrs, dict) else None
    return lei or record.get("id")


def _name_from_record(record: dict[str, Any]) -> str | None:
    """Legal name of a single GLEIF record (`attributes.entity.legalName.name`)."""
    if not isinstance(record, dict):
        return None
    attrs = record.get("attributes", {})
    entity = attrs.get("entity", {}) if isinstance(attrs, dict) else {}
    legal = entity.get("legalName", {}) if isinstance(entity, dict) else {}
    return legal.get("name") if isinstance(legal, dict) else None


def _lei_from_payload(payload: dict[str, Any]) -> str | None:
    data = payload.get("data") or []
    if not data:
        return None
    return _lei_from_record(data[0])


class GleifClient:
    """Real GLEIF lookup over httpx. Exact legal-name filter, first record wins."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        base_url: str = GLEIF_BASE_URL,
        timeout: float = 10.0,
    ) -> None:
        self._client = client or httpx.Client(base_url=base_url, headers=_HEADERS, timeout=timeout)
        self._owns_client = client is None

    def lei_for(self, name: str, *, cik: str | None = None) -> str | None:
        if not name:
            return None
        try:
            resp = self._client.get(
                "/lei-records",
                params={"filter[entity.legalName]": name, "page[size]": 1},
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            return None
        try:
            return _lei_from_payload(resp.json())
        except ValueError:
            return None

    def search(self, name: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """Fuzzy legal-name search → `[{lei, name}]` (most-relevant first).

        Uses GLEIF's `filter[fulltext]` fuzzy index rather than the exact
        `legalName` filter `lei_for` uses. Timeout/HTTP-error guarded: returns
        `[]` on any failure so discovery can degrade to EDGAR-only results.
        """
        if not name or not name.strip():
            return []
        size = max(1, int(limit))
        try:
            resp = self._client.get(
                "/lei-records",
                params={"filter[fulltext]": name.strip(), "page[size]": size},
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            return []
        try:
            payload = resp.json()
        except ValueError:
            return []
        out: list[dict[str, Any]] = []
        for record in (payload.get("data") or [])[:size]:
            lei = _lei_from_record(record)
            if not lei:
                continue
            out.append({"lei": lei, "name": _name_from_record(record) or ""})
        return out

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class StaticGleifClient:
    """Offline stand-in: resolve LEIs from an in-memory `{normalized-or-exact name: lei}` map."""

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self._map = dict(mapping or {})

    def add(self, name: str, lei: str) -> None:
        self._map[name] = lei

    def lei_for(self, name: str, *, cik: str | None = None) -> str | None:
        return self._map.get(name)
