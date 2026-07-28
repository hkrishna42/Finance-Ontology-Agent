# Firm Ontology Platform — POC

Multi-agent knowledge-graph extraction, risk analysis, change-impact, and regulatory-reporting
assistant over SEC filings — a bounded, high-value slice for a demonstration asset manager (**Demo Investment Management**).

- **LLM:** Claude via the Anthropic API, behind an `LLMProvider` abstraction (Sonnet 5 for heavy
  agents, Haiku 4.5 for light agents). `FakeProvider` + cassettes run CI and stub mode offline.
- **Embeddings:** `fastembed` + `BAAI/bge-small-en-v1.5` (384-d, CPU) in full mode; a deterministic
  `HashEmbedder` in stub mode (offline, no download).
- **Graph + vector:** Neo4j 5 Community (native 384-d vector index). **App state:** SQLite.
- **Backend:** FastAPI + SSE. **Frontend:** React + Vite + TS, served by nginx.

Everything runs in Docker: `git clone` → **one command** → the whole app in your browser.

## Quick start

**Prerequisite:** [Docker Desktop](https://www.docker.com/products/docker-desktop/), running.
Nothing else — no Python, Node, or Neo4j on the host.

### Stub mode (default — fully offline, no API key)

```bash
make bootstrap
```

Builds the images, starts `neo4j → api → web`, and the api restores a **committed graph snapshot**
on first boot. When it prints healthy (first build ≈ 3–8 min, then seconds on re-runs), open:

**http://localhost:5173**

The Graph, Risk, Impact, and Report panels are all live from the snapshot. Verify from the CLI:

```bash
bash scripts/demo_check.sh
```

### Full mode (real Claude extraction + narration)

```bash
cp .env.example .env          # then set ANTHROPIC_API_KEY=sk-ant-...  (funded org)
make bootstrap MODE=full
```

Full mode swaps in real Claude (extraction, text-to-Cypher, narration) and fastembed embeddings.
The key is read from `.env` (gitignored) and injected only into the api container — never baked
into an image, never committed.

### Everyday commands

```bash
make ps         # container status
make logs       # tail all services
make down       # stop (keep data volumes)
make reset      # stop + delete volumes (Neo4j graph + SQLite state)
make snapshot   # re-freeze corpus/snapshot/ after editing the seed graph
make ci         # offline gate: ruff + pytest (no Docker, no key, no Neo4j)
```

Re-running `make bootstrap` is idempotent — a populated graph is never clobbered.

## Modes at a glance

| | **stub** (default) | **full** |
|---|---|---|
| API key | none | `ANTHROPIC_API_KEY` in `.env` |
| LLM | `FakeProvider` (offline) | real Claude (Sonnet 5 / Haiku 4.5) |
| Embeddings | `HashEmbedder` (deterministic) | `fastembed` bge-small (baked into the image) |
| Graph | restored from committed snapshot | snapshot seed, re-embedded with fastembed; grows via real `POST /documents` |
| Network | none required | Anthropic API (+ EDGAR for live filing pulls) |

**Embedder / snapshot consistency.** Vector search compares a query embedding against the
`:Chunk` embeddings, so both must come from the *same* embedder. Stub restores the committed
`HashEmbedder` vectors **and** embeds queries with `HashEmbedder`; full re-embeds the same chunks
with fastembed **and** embeds queries with fastembed. `tests/test_snapshot.py` asserts the
committed vectors are 384-d and that the current `HashEmbedder` still reproduces them (a drift
alarm — re-run `make snapshot` if it fires).

## Run without building (prebuilt images)

Tagging a release (`.github/workflows/release.yml`) publishes multi-arch images to GHCR. A target
machine then never compiles:

```bash
REGISTRY=ghcr.io/<owner> IMAGE_TAG=v0.1.0 \
  docker compose -f docker-compose.yml -f docker-compose.prebuilt.yml up --wait
# open http://localhost:5173
```

## Architecture

```
browser ──▶ web (nginx :5173)
                 ├─ serves the built React SPA
                 └─ reverse-proxies /health /ontology /graph /documents /risk /impact
                    /reports /resolve /query /ingest  ──▶  api (uvicorn :8000, internal)
                                                                └─▶ neo4j (bolt://neo4j:7687)
```

The web app calls the API with **same-origin relative URLs**, so nginx proxies them to `api` with
no CORS; SSE streams (`/ingest`, `POST /documents`) are proxied unbuffered so events flush live.
In-container the api reaches Neo4j at `bolt://neo4j:7687` (service DNS), not localhost.

## Layout

```
api/            FastAPI backend, LangGraph agents, ontology (SSOT), providers, modules, snapshot.py
web/            React + Vite frontend (served by nginx in the container)
docker/         api.Dockerfile · web.Dockerfile · nginx.conf · api-entrypoint.sh
corpus/snapshot/ committed graph snapshot (seed cypher + hash embeddings) restored on stub boot
fixtures/       FakeProvider outputs, mini filings, golden files, seed graph
scripts/        make_snapshot · demo_check · qa_check · load_seed · apply_ddl
docker-compose*.yml   base (stub) · .full.yml (real Claude) · .prebuilt.yml (GHCR images)
```

## Troubleshooting

- **`the Docker daemon isn't running`** — start Docker Desktop, wait for the whale icon to settle,
  retry. Confirm with `docker info`.
- **`docker pull` hangs / images never download** (daemon reaches no registry while your browser
  can): Docker Desktop's networking is wedged — usually its proxy. Check **Settings → Resources →
  Proxies** (set to *No proxy* unless your network needs one), then **Settings → Troubleshoot →
  Restart** (or fully quit and reopen Docker Desktop). `docker pull hello-world` should then
  succeed. This is a host-Docker issue, not a repo issue.
- **Port already in use** (`5173`, `7474`, `7687`) — stop the other process, or override:
  `WEB_PORT=8080 make bootstrap` (also `NEO4J_HTTP_PORT`, `NEO4J_BOLT_PORT`).
- **First build is slow** — it compiles the SPA and bakes the embedding model. Subsequent runs use
  the layer cache. For a target that shouldn't compile at all, use the prebuilt images above.
- **Full mode extraction fails with an auth/credit error** — the key needs API credit on the *same
  org*; a subscription or another org's credit won't work. Keep test runs small.
- **Stale/odd graph data** — `make reset` wipes the volumes; the next `make bootstrap` restores a
  clean snapshot.

See the build plan for scope, the layered ontology model, and the demo script.
