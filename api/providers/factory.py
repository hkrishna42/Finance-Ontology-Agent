"""LLM provider factory — the single place run-mode maps to a concrete provider.

  * `stub`  → `FakeProvider` (offline, deterministic; no key, no network).
  * `full`  → `AnthropicProvider`, optionally wrapped in `CassetteProvider`:
      - `record_cassettes=True`     → record mode (call Anthropic, write recordings).
      - else a populated cassette dir → replay mode (offline replay of recorded calls).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from .fake import CassetteProvider, FakeProvider

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings


def get_llm_provider(settings: Settings, log_conn: Any = None) -> Any:
    """Return the `LLMProvider` for the current run mode."""
    if settings.provider_mode == "stub":
        return FakeProvider()

    # full mode: real Anthropic, imported lazily so `stub` never needs the SDK path.
    from .anthropic import AnthropicProvider

    inner = AnthropicProvider(settings, log_conn=log_conn)
    cassette_dir = Path(settings.cassette_dir)
    if settings.record_cassettes:
        return CassetteProvider(inner, cassette_dir, record=True)
    if cassette_dir.exists() and any(cassette_dir.glob("*.json")):
        return CassetteProvider(inner, cassette_dir, record=False)
    return inner
