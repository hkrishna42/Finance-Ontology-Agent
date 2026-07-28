"""Config tests: stub mode loads with no API key."""

from __future__ import annotations

from api.config import Settings, get_settings


def test_get_settings_loads_stub_without_key():
    settings = get_settings()
    assert settings.provider_mode == "stub"
    assert settings.is_full is False


def test_defaults(monkeypatch):
    # Ensure defaults hold even with no env / no .env influence on these keys.
    monkeypatch.delenv("PROVIDER_MODE", raising=False)
    monkeypatch.delenv("EMBED_BACKEND", raising=False)
    s = Settings(_env_file=None)
    assert s.provider_mode == "stub"
    assert s.embed_backend == "hash"
    assert s.vector_dim == 384
    assert s.routing_profile == "balanced"
    assert s.neo4j_uri.startswith("bolt://")
