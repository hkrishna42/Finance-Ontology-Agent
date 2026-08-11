"""M8 /eval endpoint — a live `EvalCard[]` served by the API (replacing the web fixture fallback)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.modules.eval_routes import router

_CARD_KEYS = {"id", "title", "description", "status", "metrics"}


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_get_eval_returns_evalcard_array():
    resp = _client().get("/eval")
    assert resp.status_code == 200
    cards = resp.json()
    assert isinstance(cards, list) and len(cards) >= 1
    ids = {c["id"] for c in cards}
    assert {"extraction_pr", "grounding", "vector_vs_graph", "entitlement"} <= ids
    for c in cards:
        assert _CARD_KEYS <= set(c)
        assert c["status"] in {"wired", "placeholder"}
        assert isinstance(c["metrics"], list) and c["metrics"]
        for m in c["metrics"]:
            assert "label" in m and "value" in m
            assert m["value"] is None or isinstance(m["value"], (int, float))


def test_grounding_threshold_is_a_live_number():
    cards = _client().get("/eval").json()
    grounding = next(c for c in cards if c["id"] == "grounding")
    thr = next(m for m in grounding["metrics"] if "threshold" in m["label"].lower())
    # a real, non-null backend value (0..1) — proves the endpoint serves live state, not a fixture
    assert isinstance(thr["value"], (int, float)) and 0 < thr["value"] <= 1
