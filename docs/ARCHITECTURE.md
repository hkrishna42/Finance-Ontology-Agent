# Architecture

Firm Ontology Platform — a multi-agent knowledge-graph extraction + analytics system over SEC
filings. This document describes the system design; see [FEATURES.md](FEATURES.md) for what it does
and [PROJECT_SUMMARY.md](PROJECT_SUMMARY.md) for current status.

## Design principles (harness engineering)

1. **Contracts as the single source of truth + codegen.** `api/ontology/schema.py` (14 entities /
   21 relations) generates the extraction JSON Schema, the Neo4j DDL, and the prompt schema-card —
   nothing is hand-duplicated, so nothing drifts.
2. **Deterministic gates around every non-deterministic (LLM) step.** Structured output → a
   grounding gate (a verbatim span must fuzzy-match its chunk) → a steward (domain/range + dedupe +
   provenance). The *graph* stays stable even when LLM prose varies.
3. **Offline, free, deterministic CI.** A `FakeProvider` + a deterministic `HashEmbedder` run the
   whole stack with no API key and no network. Real Claude + fastembed are opt-in (`full` mode).
4. **The firm is a runtime variable.** No client identity is hardcoded: the repo ships an anonymized
   demo, and real firm data is discovered + onboarded at runtime (never committed).

## Tech stack

| Layer | Choice |
|---|---|
| Backend | FastAPI + SSE, Python 3.12, managed by `uv` |
| Frontend | React + Vite + TypeScript, served by nginx |
| Graph + vector | Neo4j 5 Community (native 384-d vector index) |
| App state | SQLite (jobs, spans, agent logs, reports, **firms registry**, resolution queue) |
| LLM | Claude via the Anthropic API behind an `LLMProvider` abstraction (Sonnet 5 heavy / Haiku 4.5 light) |
| Embeddings | `fastembed` + `BAAI/bge-small-en-v1.5` (384-d, CPU) in full mode; `HashEmbedder` (deterministic, offline) in stub |
| Discovery | SEC EDGAR (`edgartools`) + GLEIF (LEI) — free, no key |
| Packaging | Docker (multi-stage api + nginx web images) + Docker Compose |

## Runtime topology

```
browser ──▶ web  (nginx, host :5173)
                 ├─ serves the built React SPA
                 └─ reverse-proxies same-origin API routes ──▶ api (uvicorn :8000, internal)
                        /health /ontology /graph /documents /risk /impact /reports              │
                        /resolve /query /ingest /firms /eval   (proxy_buffering off for SSE)     ▼
                                                                              neo4j (bolt://neo4j:7687)
                                                                              sqlite (/app/data/app.db)
```

The SPA calls the API with **same-origin relative URLs**, so nginx proxies them with no CORS; SSE
streams (`/ingest`, `POST /documents`, `POST /firms/onboard`, `/firms/{id}/enrich`) are proxied
unbuffered. In-container the API reaches Neo4j at `bolt://neo4j:7687` (service DNS), not localhost.

## Backend package map (`api/`)

| Package | Responsibility |
|---|---|
| `ontology/` | **Single source of truth**: `schema.py` (entities/relations) → JSON Schema + Neo4j DDL + prompt card; `models.py`; `impact_policy.yaml` |
| `providers/` | `base.py` (LLMProvider + EmbeddingProvider split), `anthropic.py`, `fake.py` (+ cassettes), `embeddings.py` (HashEmbedder / FastEmbedEmbedder), `registry.py` (model routing), `factory.py` |
| `stores/` | `neo4j.py` (driver + DDL + vector index), `sqlite.py` (app-state schema incl. the `firms` table) |
| `extract/` | chunking + grounded extraction + the grounding gate |
| `steward/` | domain/range validation, dedupe, provenance stamping, graph writes |
| `resolution/` | issuer name → CIK/LEI resolution (`resolver.py`), GLEIF client (`gleif.py`), the SEC spine, the provisional queue |
| `ingest/` | `POST /documents` pipeline (`ingest_document`), EDGAR + HTML sources |
| `l2/` | `nport.py` — deterministic NPORT-P holdings parser + graph writer (Fund + weighted HOLDS) |
| `query/` | router, text-to-Cypher agent (read-only guard + retries), synthesizer, entitlement filter |
| `modules/` | the three apps: `risk_lens.py`, `change_impact.py`, `reg_reports/` |
| `firms/` | **firm registry** (`store.py`: SQLite CRUD + active-firm), **graph ops** (`graph_ops.py`: firm→funds, delete), `routes.py` |
| `onboarding/` | **discovery** (`discovery.py`: EDGAR/GLEIF search + NPORT fetch), **orchestrator** (`pipeline.py`), **enrichment** (`enrich.py`), `routes.py` |
| `snapshot.py` | generate (offline) + restore-on-empty the committed demo graph |
| `graph_view.py` | `GET /graph`, `GET /documents` (entitlement-aware) |
| `main.py` | app assembly, router wiring, startup registry sync |

## The layered ontology

- **L1 (extracted from text):** Company, Person, RiskFactor, Product, DisclosureSection, … — pulled
  by the LLM under the grounding gate, 9 of the 14 entity types are extractable.
- **L2 (structured):** Fund, HOLDS (weighted), MANAGED_BY — parsed deterministically from NPORT-P XML
  (no LLM), `confidence = 1.0`.
- Every edge carries provenance (doc_id, chunk_id, page, span, confidence, as_of, extractor_model).

## Two data paths

1. **Demo (committed, offline).** `corpus/snapshot/` (seed cypher + HashEmbedder vectors) is restored
   on first boot when the graph is empty (`api/snapshot.py::restore_if_empty`). This is the anonymized
   "Demo Investment Management" firm — instant, no key, no network.
2. **Onboarding (runtime, live).** A user types a firm name → `POST /firms/search` (EDGAR + GLEIF) →
   picks a match → `POST /firms/onboard` streams: for each fund series, fetch the latest NPORT-P XML →
   `l2/nport.parse_nport` → write Fund + weighted HOLDS + MANAGED_BY → resolve issuers → save to the
   `firms` registry + activate. Deterministic, **network-only (no API key)**. Optional `POST
   /firms/{id}/enrich` then runs the LLM extraction pipeline over top holdings' 10-K risk factors to
   add the semantic layer.

The **active firm** (SQLite `firms.is_active`) scopes the analytics endpoints; `?firm=<name>` overrides.

## Embedder / snapshot consistency (important invariant)

Vector search compares a query embedding against `:Chunk` embeddings, so both must come from the
*same* embedder. **Stub** restores the committed HashEmbedder vectors **and** embeds queries with
HashEmbedder; **full** re-embeds chunks with fastembed **and** embeds queries with fastembed.
`tests/test_snapshot.py` asserts the committed vectors are 384-d and that the current HashEmbedder
still reproduces them (a drift alarm — re-run `make snapshot` if it fires).

## Containerization

- `docker/api.Dockerfile` — multi-stage `python:3.12-slim` + `uv --frozen` (core + light `ingest`
  extra; **no torch/CUDA** — Docling is an opt-in `ingest-tables` extra); **bakes bge-small** into the
  image; non-root; `/health` healthcheck; entrypoint waits for Neo4j → inits SQLite → restores the
  snapshot on empty → `uvicorn`.
- `docker/web.Dockerfile` — node build → `nginx:alpine` serving `web/dist` + the reverse proxy
  (`docker/nginx.conf`).
- `docker-compose.yml` (stub, default) + `docker-compose.full.yml` (real Claude, key via `.env`
  env_file) + `docker-compose.prebuilt.yml` (pull pinned GHCR images). Base images pinned by digest.
- `make bootstrap [MODE=full]` → `docker compose up --build --wait`.

## CI/CD

- `.github/workflows/ci.yml` — offline gate (ruff + pytest, FakeProvider/HashEmbedder) + `docker
  compose config` validation (all 3 compositions) + **multi-arch image builds** (buildx amd64+arm64).
- `.github/workflows/release.yml` — on a `v*` tag: push multi-arch images to GHCR + attach the
  compose files + snapshot to a GitHub Release.
