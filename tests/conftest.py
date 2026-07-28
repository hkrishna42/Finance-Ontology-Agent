"""Hermetic offline test environment.

The unit/integration suite must be deterministic regardless of a developer's local `.env`
(which may set PROVIDER_MODE=full + a real ANTHROPIC_API_KEY once they're running the real
stack). This autouse fixture ignores `.env`, forces stub/hash, and drops the key, so
`make ci` / `make test` behave the same on a laptop with a full `.env` and in clean CI.

Integration tests that need a live Neo4j still read NEO4J_URI (default localhost:7687) and
self-skip when the DB is unreachable.
"""

from __future__ import annotations

import pytest

from api import config


@pytest.fixture(autouse=True)
def _hermetic_stub_env(monkeypatch):
    # Ignore the developer's .env so tests don't inherit full-mode/key from it.
    monkeypatch.setitem(config.Settings.model_config, "env_file", None)
    monkeypatch.setenv("PROVIDER_MODE", "stub")
    monkeypatch.setenv("EMBED_BACKEND", "hash")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()
