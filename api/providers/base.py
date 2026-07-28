"""Provider abstraction — the seam that makes the LLM swappable.

Agent code NEVER names a model. It calls `provider.complete(role=..., ...)` /
`provider.complete_structured(role=..., schema=..., ...)`; the provider maps the role to a model
via the routing registry. That is what lets us run Anthropic in dev, FakeProvider/cassettes in
CI, and (later, unchanged) Claude-on-Bedrock in a VPC.

Embeddings are a SEPARATE protocol: Anthropic has no embeddings endpoint, so embeddings come
from a local in-process model (fastembed) regardless of the LLM backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, TypedDict, runtime_checkable


class Role(str, Enum):
    """Agent roles. The routing registry maps each to a model + effort/sampling policy."""

    INTAKE = "intake"
    EXTRACTION = "extraction"
    RESOLUTION = "resolution"
    STEWARD = "steward"
    ROUTER = "router"
    ANALYST = "analyst"
    SYNTHESIZER = "synthesizer"
    IMPACT = "impact"


class Message(TypedDict):
    role: str  # "user" | "assistant"
    content: str


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_input_tokens + other.cache_read_input_tokens,
            self.cache_creation_input_tokens + other.cache_creation_input_tokens,
        )


@dataclass
class Completion:
    text: str
    model: str
    usage: Usage = field(default_factory=Usage)
    stop_reason: str | None = None


@dataclass
class StructuredResult:
    data: dict  # validated against the supplied JSON Schema
    raw: str  # the raw JSON text the model returned
    model: str
    usage: Usage = field(default_factory=Usage)
    stop_reason: str | None = None


@runtime_checkable
class LLMProvider(Protocol):
    """Text + structured completions. Implementations: AnthropicProvider, FakeProvider,
    CassetteProvider."""

    def complete(
        self,
        *,
        role: Role,
        system: str,
        messages: list[Message],
        max_tokens: int = 1024,
    ) -> Completion: ...

    def complete_structured(
        self,
        *,
        role: Role,
        schema: dict,
        system: str,
        messages: list[Message],
        max_tokens: int = 2048,
        cache_system: bool = False,
    ) -> StructuredResult: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    """In-process embeddings (fastembed / bge-small). Kept separate from LLMProvider."""

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    @property
    def dim(self) -> int: ...
