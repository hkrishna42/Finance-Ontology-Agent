"""Embedding providers — a protocol separate from the LLM (Anthropic has no embeddings endpoint).

Two implementations:
  * `FastEmbedEmbedder` — the real one: `fastembed.TextEmbedding(EMBED_MODEL)` (bge-small, 384-d,
    CPU, downloads the model once and caches it). Imported lazily so CI never pulls it.
  * `HashEmbedder` — deterministic, offline, no download: hashes each text into a fixed 384-float
    L2-normalised vector. Used by CI/stub so the whole stack runs with zero network.

`get_embedder(settings)` picks by `settings.embed_backend` ("hash" default → offline CI).
"""

from __future__ import annotations

import hashlib
import math
from typing import TYPE_CHECKING

from ..ontology.schema import EMBED_MODEL, VECTOR_DIM

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings


class HashEmbedder:
    """Deterministic offline embedder: token-hashing into a unit vector. No model download.

    Not semantically meaningful, but stable and the right shape (dim = VECTOR_DIM, ~unit norm),
    which is all CI / the vector-index smoke test needs.
    """

    def __init__(self, dim: int = VECTOR_DIM) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        tokens = text.lower().split() or [text]
        for tok in tokens:
            h = hashlib.sha256(tok.encode("utf-8")).digest()
            # Use pairs of bytes to index/sign several buckets per token.
            for i in range(0, len(h) - 1, 2):
                idx = (h[i] | (h[i + 1] << 8)) % self._dim
                sign = 1.0 if (h[i] & 1) == 0 else -1.0
                vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            # Degenerate (e.g. empty text): return a fixed unit vector.
            vec[0] = 1.0
            return vec
        return [v / norm for v in vec]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]


class FastEmbedEmbedder:
    """Real in-process embedder (bge-small via fastembed). Lazy import; downloads once.

    Honours ``FASTEMBED_CACHE_PATH`` (or an explicit ``cache_dir``) so the api image can **bake**
    the model at build time and load it offline at runtime — the container sets that env var to the
    baked cache and ``HF_HUB_OFFLINE=1``, so the first real embed never stalls on a ~90MB download.
    """

    def __init__(
        self, model_name: str = EMBED_MODEL, dim: int = VECTOR_DIM, cache_dir: str | None = None
    ) -> None:
        import os

        from fastembed import TextEmbedding  # lazy: never imported in CI

        cache_dir = cache_dir or os.environ.get("FASTEMBED_CACHE_PATH") or None
        kwargs = {"cache_dir": cache_dir} if cache_dir else {}
        self._model = TextEmbedding(model_name=model_name, **kwargs)
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._model.embed(texts)]


def get_embedder(settings: Settings) -> HashEmbedder | FastEmbedEmbedder:
    """Factory: choose the embedder by `settings.embed_backend` ("hash" default = offline)."""
    backend = getattr(settings, "embed_backend", "hash")
    if backend == "fastembed":
        return FastEmbedEmbedder(model_name=settings.embed_model, dim=settings.vector_dim)
    return HashEmbedder(dim=settings.vector_dim)
