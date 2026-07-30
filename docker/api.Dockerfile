# syntax=docker/dockerfile:1.7
# Firm Ontology Platform — API image (FastAPI + SSE).
#
# Multi-stage: a builder resolves the frozen dependency set with uv and bakes the embedding model
# into the image; a slim non-root runtime carries only the venv, the baked model, and the app.
# Runs the same in both modes — PROVIDER_MODE / EMBED_BACKEND / ANTHROPIC_API_KEY are runtime env.

# ---- builder: frozen deps + baked fastembed model ---------------------------------------------
# Base images pinned by digest for reproducibility (tags kept for readability). Refresh with:
#   docker buildx imagetools inspect python:3.12-slim   # etc.
FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de AS builder

# uv (pinned) provides fast, reproducible, --frozen installs.
COPY --from=ghcr.io/astral-sh/uv:0.11.12@sha256:3a59a3cdd5f7c217faa36e32dbc7fddbb0412889c2a0a5229f6d790e5a019dd7 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependency layer first (cache-friendly): only the lockfile + manifest, no app source yet, so an
# app-code change does not bust the resolved-deps cache. Core + the light `ingest` extra (EDGAR +
# HTML parsing); no dev tools, no torch/docling.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --extra ingest

# Bake BAAI/bge-small-en-v1.5 (384-d) into the image so full mode never blocks on a ~90MB download
# at runtime. One warmup embed forces the full ONNX init + cache population.
#
# HF_HUB_DISABLE_XET=1 forces HuggingFace's classic HTTPS download instead of the newer Xet/CAS
# backend (which fails behind some proxies with "CAS Client Error: ... File reconstruction error").
#
# The bake is NON-FATAL. On a network that blocks or TLS-intercepts huggingface.co the download can't
# complete (e.g. a corporate MITM proxy -> "CERTIFICATE_VERIFY_FAILED: self-signed certificate in
# certificate chain"). Stub mode uses the deterministic HashEmbedder and needs no model, so a failed
# bake still yields a working stub image; full mode then needs the model (build on an un-intercepted
# network, or add your proxy's root CA to the image's trust store). BAKE_FASTEMBED=0 skips the attempt.
ARG BAKE_FASTEMBED=1
RUN mkdir -p /opt/fastembed && \
    if [ "$BAKE_FASTEMBED" = "1" ]; then \
      HF_HUB_DISABLE_XET=1 /app/.venv/bin/python -c "from fastembed import TextEmbedding; list(TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='/opt/fastembed').embed(['warmup']))" \
      || echo "WARN: fastembed model bake failed (network/proxy blocked huggingface.co) — the STUB demo still works; full mode needs the model."; \
    else \
      echo "BAKE_FASTEMBED=0 — skipping the fastembed model bake (stub-only image)."; \
    fi

# ---- runtime: slim, non-root -----------------------------------------------------------------
FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de AS runtime

RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FASTEMBED_CACHE_PATH=/opt/fastembed \
    HF_HUB_OFFLINE=1 \
    PROVIDER_MODE=stub \
    EMBED_BACKEND=hash \
    NEO4J_URI=bolt://neo4j:7687 \
    SQLITE_PATH=/app/data/app.db

# venv + baked model from the builder.
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /opt/fastembed /opt/fastembed

# App source + the committed snapshot the container restores on first boot. (No .env is ever copied
# — secrets arrive only via runtime env. fixtures/ and corpus/downloads/ are not needed at runtime.)
COPY api/ ./api/
COPY corpus/snapshot/ ./corpus/snapshot/
COPY docker/api-entrypoint.sh /usr/local/bin/api-entrypoint.sh

RUN mkdir -p /app/data \
    && chmod +x /usr/local/bin/api-entrypoint.sh \
    && chown -R app:app /app /opt/fastembed

USER app
EXPOSE 8000

# Liveness on the real app surface (no curl in the image → use stdlib urllib).
HEALTHCHECK --interval=15s --timeout=5s --start-period=45s --retries=10 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)" || exit 1

ENTRYPOINT ["/usr/local/bin/api-entrypoint.sh"]
