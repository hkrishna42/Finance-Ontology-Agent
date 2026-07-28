"""Typed configuration (pydantic-settings). Reads environment / .env.

Defaults to `stub` mode so the app runs with no API key. `full` mode requires ANTHROPIC_API_KEY.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from .ontology.schema import EMBED_MODEL, VECTOR_DIM


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Run mode: stub = FakeProvider (offline, no key); full = real Anthropic.
    provider_mode: Literal["stub", "full"] = "stub"

    # LLM backend
    anthropic_api_key: str | None = None
    anthropic_target: Literal["anthropic", "bedrock"] = "anthropic"
    aws_region: str = "us-east-1"
    routing_profile: Literal["balanced", "quality-max"] = "balanced"

    # Cassettes (record real responses once; replay offline in CI / make eval)
    cassette_dir: str = ".cassettes"
    record_cassettes: bool = False

    # Stores
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "firmontology"
    sqlite_path: str = "./data/app.db"

    # Embeddings (kept in sync with the ontology)
    embed_model: str = EMBED_MODEL
    vector_dim: int = VECTOR_DIM
    # Embedding backend: "hash" = deterministic offline stub (CI default, no download);
    # "fastembed" = real bge-small (downloads the model once).
    embed_backend: Literal["hash", "fastembed"] = "hash"

    # EDGAR requires a declared User-Agent ("Name email")
    edgar_user_agent: str = "Example Firm Research contact@example.com"

    log_level: str = "INFO"

    @property
    def is_full(self) -> bool:
        return self.provider_mode == "full"


@lru_cache
def get_settings() -> Settings:
    return Settings()
