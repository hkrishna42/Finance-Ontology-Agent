"""M2 / resolution — GLEIF LEI enrichment over httpx, mocked offline via MockTransport."""

from __future__ import annotations

import httpx

from api.resolution.gleif import GLEIF_BASE_URL, GleifClient, StaticGleifClient


def _client(handler) -> GleifClient:
    transport = httpx.MockTransport(handler)
    inner = httpx.Client(base_url=GLEIF_BASE_URL, transport=transport)
    return GleifClient(inner)


def test_gleif_returns_lei_from_record():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/lei-records")
        assert request.url.params["filter[entity.legalName]"] == "NVIDIA CORPORATION"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "type": "lei-records",
                        "id": "549300N1KM4WQE9DTA96",
                        "attributes": {"lei": "549300N1KM4WQE9DTA96"},
                    }
                ]
            },
        )

    g = _client(handler)
    assert g.lei_for("NVIDIA CORPORATION") == "549300N1KM4WQE9DTA96"


def test_gleif_empty_result_is_none():
    g = _client(lambda req: httpx.Response(200, json={"data": []}))
    assert g.lei_for("No Such Issuer XYZ") is None


def test_gleif_http_error_is_none():
    g = _client(lambda req: httpx.Response(500, json={"error": "boom"}))
    assert g.lei_for("NVIDIA CORPORATION") is None


def test_gleif_falls_back_to_record_id_when_no_attr_lei():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "5493001KJTIIGC8Y1R12", "attributes": {}}]})

    assert _client(handler).lei_for("Some Co") == "5493001KJTIIGC8Y1R12"


def test_static_gleif_client():
    g = StaticGleifClient({"NVIDIA CORP": "549300N1KM4WQE9DTA96"})
    assert g.lei_for("NVIDIA CORP") == "549300N1KM4WQE9DTA96"
    assert g.lei_for("Unknown") is None
