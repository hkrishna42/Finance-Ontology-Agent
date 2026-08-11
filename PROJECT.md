# FinanceOnto — Project Overview

**FinanceOnto** turns unstructured financial documents into a **FIBO-grounded knowledge graph**,
maps every entity to a **simulated internal lakehouse** (a medallion model with golden-record
dimensions), and reconciles the same real-world entity across systems into one auditable
**Master Data Management (MDM) golden record** — then exposes it all through an interactive graph
explorer and a responsive analyst UI.

It is built on the **Firm Ontology Platform** POC: a multi-agent extraction + analytics system over
SEC filings. The platform is *firm-agnostic* — it ships an anonymized demo (**Demo Investment
Management**) and lets a user onboard any real firm by name at runtime.

> **Naming.** "FinanceOnto" is the product vision; "Firm Ontology Platform" is the codebase it is
> built on. In the UI the shell is branded "Firm Ontology"; the demo firm is "Demo Investment
> Management". All data in this repo is neutral/fictional (e.g. *Harborview Tower*, *Meridian Core
> Real Estate Fund*, *Atlantic Credit Company LLC*); public issuers used in the SEC-filings demo are
> real public companies, never a client.

---

## 1. Vision & what it does

The system demonstrates an end-to-end path from **documents → ontology → analytics**, with three
distinct but connected capabilities:

1. **Grounded knowledge graph.** Unstructured filings are parsed, chunked, and passed through an LLM
   extraction step under a **grounding gate** (every claim must fuzzy-match a verbatim span) and a
   **steward** (domain/range validation, dedup, provenance). Every entity is grounded to a real
   **FIBO OWL class**, and an `owlrl` reasoner checks the result for OWL consistency.
2. **Simulated lakehouse + MDM.** A medallion model (Bronze → Silver → Gold) holds deliberately
   *conflicting* source-system records for the same real-world entity. The MDM engine matches them
   (blocking + fuzzy scoring → a confidence %) and applies **attribute-level survivorship rules** to
   reconcile them into one canonical **golden record** with a per-attribute decision log and lineage.
3. **Analyst experience.** An interactive **Graph Explorer** (search, domain filters, multiple
   layouts, node inspector with FIBO + lakehouse provenance, SPARQL), a graph-grounded **Analyst
   Chat** (text-to-Cypher vs. vector-RAG, side by side), and analytics panels for **Risk**, **Change
   Impact**, and **Regulatory Reporting** — all provenance-carrying and firm-scoped.

There are **two committed demos**:

| Demo | What it shows | Neutral names used |
|---|---|---|
| **SEC-filings graph** (Demo Investment Management) | Supply-chain / risk graph extracted from public 10-K/8-K/NPORT filings; the three analytics apps run on it | Demo Investment Management; public issuers (e.g. NVIDIA, TSMC) in the committed snapshot |
| **Real-estate MDM** | Golden-record reconciliation across 4 source systems for the same property/entity/fund | Harborview Tower (`PROP-1001`), Meridian Investments LLC, Atlantic Credit Company LLC, Meridian Core Real Estate Fund |

---

## 2. Architecture

### 2.1 Runtime topology

```
browser ──▶ web  (nginx, host :5173)
                 ├─ serves the built React SPA (Vite)
                 └─ same-origin reverse proxy ──▶ api (uvicorn :8000, internal)
                        /health /ontology /graph /documents /risk /impact /reports
                        /firms /resolve /query /ingest /eval /fibo /mdm /lakehouse
                        (proxy_buffering off, so SSE streams flush live)          │
                                                                                  ▼
                                                        neo4j (bolt://neo4j:7687) — graph + 384-d vectors
                                                        sqlite (/app/data/app.db) — app state
```

The SPA calls the API with **same-origin relative URLs**, so nginx proxies with no CORS. In-container
the API reaches Neo4j at `bolt://neo4j:7687` (service DNS), not localhost.

### 2.2 The ingest pipeline (streamed as SSE)

A document (uploaded file, pasted text, or an EDGAR reference) flows through a synchronous generator
that emits a frozen `SSEEvent` sequence, bridged to the browser over Server-Sent Events:

```
job.started → classified → parsed → chunked → extracted → resolved → written → (impact) → job.completed
```

| Stage | What happens |
|---|---|
| **classify** | Detect doc type + sensitivity |
| **parse** | Extract text (HTML/XBRL via bs4/lxml for EDGAR; **PDF via `pypdf`**) |
| **chunk** | Split into embeddable chunks (`:Chunk` nodes carry the 384-d vector) |
| **extract** | LLM emits entities/relations as **structured JSON** constrained by the ontology schema; only *extractable* types are allowed |
| **grounding gate** | Each entity/relation must carry a **verbatim `span`** that fuzzy-matches its chunk (≥ ~0.9), or it is dropped — the model cannot invent facts |
| **resolve** | Issuer mentions are pinned to a CIK/LEI spine; **FIBO grounding** is stamped on entity nodes; **canonical-identity dedup** snaps name variants onto the existing node; unresolved mentions are parked in the **resolution queue** |
| **steward / write** | Domain/range validation + provenance stamping, then deterministic Neo4j `MERGE` writes |

Every **semantic** edge carries provenance: `doc_id, chunk_id, page, span, confidence, as_of,
reported_at, extractor_model, sensitivity`. Structural edges (from N-PORT, MDM, etc.) are written by
deterministic code at `confidence = 1.0`.

### 2.3 FIBO grounding + OWL reasoning

- A **vendored, curated FIBO slice** (`api/fibo/vendor/firm_fibo_slice.ttl`) with **real
  FIBO/OMG-Commons class IRIs** plus `rdfs:subClassOf` and `owl:disjointWith` axioms. It is
  import-free, so it reasons in milliseconds.
- `tbox.py` parses it with **rdflib** and materializes the **OWL-RL deductive closure** with
  **owlrl** (subclass transitivity, class membership, disjointness) — genuine OWL, fully offline, no
  JRE.
- `grounding.py` (Agent B, "Ontologist") is **deterministic**: a label→FIBO-class map plus refinement
  rules (e.g. a bond/debt-issuer role refines `Company` → `CorporateDebtIssuer`; property-type
  variants collapse to the canonical `RealProperty` class).
- `reasoner.py` (Agent C, "Validator") materializes an ABox over the reasoned TBox and flags any
  individual entailed into two disjoint classes (via SPARQL) — this drives the "reasoning valid /
  violations" status.

### 2.4 The simulated lakehouse (medallion over SQLite)

Its own `lh_*` tables in the app SQLite DB (never touching the frozen `stores/sqlite.py` schema):

| Table | Role |
|---|---|
| `lh_source_system` | Source systems + **trust scores** (0–100) and layer (gold / operational / unstructured) |
| `lh_bronze_record` | Raw, **conflicting** per-source records, grouped by `master_key` |
| `lh_silver_record` | Normalized / match-grouped records |
| `lh_gold_dim` | Canonical **golden-record dimensions** (`dim_property`, `dim_legal_entity`, `dim_fund`, …), each with a PK + FIBO class |
| `lh_lineage` | Per-attribute Gold←Bronze lineage (which source won each attribute) |

Seeded from `fixtures/lakehouse/seed.json` — 4 source systems (Curated Gold `95`, Property Management
`88`, Valuation PDF `86`, Loan Servicing `80`) with deliberately conflicting representations of the
same entities.

### 2.5 MDM: matching + attribute survivorship

- **Matching** (`mdm/matching.py`): blocking + `normalize` + **rapidfuzz** token-set similarity over
  name/address, with a shared **strong id** (LEI) or crosswalked identifier treated as decisive →
  a match **confidence %** vs. a per-entity threshold.
- **Survivorship** (`mdm/survivorship.py` + `survivorship_policy.yaml`): a declarative, per-attribute
  rule set reconciles conflicts into one canonical value with an auditable rationale. Rule kinds:

  | Rule | Behavior |
  |---|---|
  | `system_of_record` | A governed source (e.g. Curated Gold) wins |
  | `trust_wins` | Highest-trust source with a non-empty value wins |
  | `canonical_identifier_crosswalk` | Source-of-record id is canonical; other ids **retained** as alternates |
  | `most_complete` | Longest / most-complete value wins (e.g. address with full postcode) |
  | `numeric_tolerance_then_trust` | Agree within tolerance → curated value survives; else fall back to trust |
  | `ontology_classification_mapping` | Variant type strings map to one canonical FIBO-aligned class |

- **Publish** (`run_match_and_merge`): writes the Gold dim + per-attribute lineage **and**, best-effort,
  a canonical **FIBO-grounded Neo4j node** (`mdm_golden:true`, carrying `fibo_class`, `lakehouse_table`,
  `lakehouse_pk`) — reusing the deterministic MERGE write, never the LLM steward.

### 2.6 Resolution queue (human-in-the-loop steward)

Mentions the resolver cannot confidently pin land in a provisional queue (SQLite) rather than being
guessed. A steward then acts, and the actions **reconcile the graph** (no APOC):

- **Merge** (`POST /resolve/merge`) — records the audit decision *and*, when a `canonical_key` is
  supplied, repoints the duplicate mention node's edges onto the canonical node and removes the
  duplicate.
- **Promote** (`POST /resolve/promote`) — keep the mention as its own new canonical node.
- **Reject** (`POST /resolve/reject`) — mark rejected, kept for audit.

Uploaded documents thread a `queue_conn` into the ingest path so their unresolved mentions are queued
for MDM review.

### 2.7 Graph + embeddings + the query/GraphRAG layer

- **Neo4j 5 Community** holds the published canonical graph with a native **384-d vector index**
  (`chunk_embedding`, cosine) and a full-text index over chunk text. The ontology's `neo4j_ddl()`
  generates the uniqueness constraints + indexes.
- **Embeddings**: `fastembed` + `BAAI/bge-small-en-v1.5` (384-d, CPU) in full mode; a deterministic
  `HashEmbedder` (offline, no download) in stub mode. Query and chunk embeddings must come from the
  **same** embedder (a snapshot-drift test guards this).
- **Query** (`QueryGraph`): Router → (analytics tool | text-to-Cypher + vector) → Synthesizer. The
  text-to-Cypher agent is **read-only-guarded** with self-correction retries; a deterministic
  pre-router answers the hero queries with **zero LLM credits**. `side_by_side` mode compares graph
  traversal against a plain vector-RAG baseline. Answers are entitlement-filtered, firm-scoped, and
  cited; the endpoint **never 500s** (provider failures degrade gracefully).

### 2.8 Entitlement wall

Reads are entitlement-aware **at the data layer**: nodes/edges/documents tagged
`sensitivity: internal` are excluded unless `internal` is in the caller's entitlements (default
`["public"]`). The query endpoint maps a UI wall toggle to that sensitivity scope.

---

## 3. Backend (FastAPI routers)

Routers are wired in `api/main.py` via `include_router`. Each owns its own `APIRouter`; none edits
`main.py`. Endpoint → purpose:

### Core (app-level, `api/main.py`)
| Endpoint | Purpose |
|---|---|
| `GET /health` | `{status, mode}` |
| `GET /ontology/info` | Ontology counts (entities/relations/extractable/vector dim) |
| `GET /ontology/schema` | The extraction JSON Schema (derived from the SSOT) |
| `GET /ontology/ddl` | Neo4j DDL statements |
| `GET /ontology/card` | The prompt schema-card |
| `GET /ingest/demo/stream` | Canned SSE pipeline demo |

### Ingest — `api/ingest/`
| Endpoint | Purpose |
|---|---|
| `POST /documents` (alias `POST /ingest/documents`) | Ingest one doc (multipart file / JSON `{text}` / JSON `{edgar}`) → streams the pipeline as SSE |

### Graph & documents — `api/graph_view.py`
| Endpoint | Purpose |
|---|---|
| `GET /graph?limit&min_confidence&entitlements&firm` | Entitlement-aware, firm-scoped subgraph `{nodes, edges}` |
| `GET /documents?entitlements&firm` | Source docs + chunks (provenance), firm-scoped |

### Query — `api/query/`
| Endpoint | Purpose |
|---|---|
| `POST /query` `{question, mode, entitlement_wall, firm}` | Graph-grounded, cited answer; `mode ∈ graph │ side_by_side` |

### Resolution queue — `api/resolution/`
| Endpoint | Purpose |
|---|---|
| `GET /resolve` | The provisional-entity queue (`ProvisionalEntity[]`) |
| `POST /resolve/` | Resolve one mention (unresolved → queued) |
| `GET /resolve/provisional`, `GET /resolve/queue` | Pending / full queue (audit) |
| `POST /resolve/merge` | Steward merge + **repoint Neo4j** onto the canonical node |
| `POST /resolve/promote` | Keep mention as a new canonical node |
| `POST /resolve/reject` | Reject (kept for audit) |

### Risk Lens — `api/modules/risk_*`
| Endpoint | Purpose |
|---|---|
| `GET /risk?firm` | Concentration + HHI, heatmap, single-source flags (firm-scoped) |
| `GET /risk/metrics` | Metric names + the exact fetch/explain Cypher |
| `GET /risk/{fund}/report` · `/metric/{m}` · `/narrative` | Per-fund report, one metric, LLM briefing |
| `GET /risk/compare?fund_a&fund_b` | Pairwise HHI comparison |

### Change Impact — `api/modules/impact_*`
| Endpoint | Purpose |
|---|---|
| `GET /impact?firm&v1&v2` | Change briefings (v1→v2 diff, propagated through the graph) |
| `GET /impact/policy` · `/run` · `/stream` | Policy, one-shot run, SSE stream |

### Regulatory Reporting — `api/modules/reg_*`
| Endpoint | Purpose |
|---|---|
| `GET /reports?firm` | Report packs for the firm's funds |
| `GET /reports/13f/{fund}` (+ `/informationtable.xml`, `/reviewer.csv`) | 13F draft (JSON / XML / CSV) |
| `GET /reports/coverage/{fund}` | Principal-risks coverage check |
| `GET /reports/pack/{fund}` (+ `/html`) | Report pack + stable SHA-256 + rendered HTML |

### Firms registry — `api/firms/`
| Endpoint | Purpose |
|---|---|
| `GET /firms` · `GET /firms/active` | List firms / the active firm |
| `POST /firms/{id}/select` · `DELETE /firms/{id}` | Switch active firm / remove firm (+ its graph subtree) |

### Onboarding — `api/onboarding/`
| Endpoint | Purpose |
|---|---|
| `POST /firms/search` `{query}` | EDGAR + GLEIF candidate search (no API key) |
| `POST /firms/onboard` | Stream a deterministic NPORT-P pull → Fund + weighted `HOLDS` + `MANAGED_BY` (SSE) |
| `POST /firms/{id}/enrich?top` | Opt-in LLM semantic enrichment over top holdings' 10-K risk factors (SSE) |

### FIBO — `api/fibo/`
| Endpoint | Purpose |
|---|---|
| `GET /fibo/classes` | The curated FIBO classes (iri, curie, label, parents) |
| `GET /fibo/ground?label&category` | Deterministic FIBO grounding for one label |
| `POST /fibo/validate` `{instances}` | OWL-RL reasoning result (valid + violations) |
| `POST /fibo/sparql` `{query}` | Read-only SPARQL over the reasoned TBox |

### Lakehouse — `api/lakehouse/`
| Endpoint | Purpose |
|---|---|
| `GET /lakehouse/source-systems` | Medallion source systems + trust scores |
| `GET /lakehouse/dim/{dim_table}/{pk}` | A published gold row + its lineage |

### MDM wizard — `api/mdm/`
| Endpoint | Purpose |
|---|---|
| `GET /mdm/entities` | Selectable master entities (FIBO-grounded clusters) |
| `GET /mdm/entities/{entity_id}/sources` | The conflicting source-system records |
| `POST /mdm/match` `{entity_id}` | Ontology-driven matching (blocking + confidence %) |
| `GET /mdm/survivorship-policy` | The attribute-level rule set |
| `POST /mdm/merge` `{entity_id}` | Run match & merge; publish the golden record |
| `GET /mdm/golden/{dim_table}/{pk}` | Re-fetch a published golden record + lineage |

### The ontology (single source of truth) — `api/ontology/schema.py`

One declarative spec (`ENTITY_SPECS` + `RELATION_SPECS`) drives **three** derived artifacts so nothing
can drift: the extraction JSON Schema, the Neo4j DDL, and the prompt schema-card.

- **19 entity types / 27 relation types**, of which **9 entities / 16 relations are LLM-extractable**;
  the rest are structural (written by deterministic code — N-PORT, MDM, reports, chunks).
- Layered: **L1** market/issuer (Company, Person, RiskFactor, Product, …) · **L2** firm & real-estate
  structure (Fund, RealProperty, Portfolio, Lease, Loan, Valuation) · **L3** obligations/outputs
  (DisclosureSection, RegulatoryForm, GeneratedReport) · **infra** (Document, Chunk).
- Selected entities carry a default `fibo_class` (e.g. `Company → cmns-org:LegalEntity`,
  `RealProperty → fibo-fnd-plc-rp:RealProperty`, `Loan → fibo-loan-ln-ln:Loan`).

---

## 4. Frontend (React + Vite + TypeScript)

A single-page app (`web/src/App.tsx`) served by nginx. The shell is a sidebar + topbar with a
theme toggle, a **firm selector** (switch active firm / "+ Add firm…" / "All data" unscoped view),
and live status pills (API OK · mode; ontology counts).

### The 10 panels (grouped into 3 sections)

| Section | Panel | Component | What it shows |
|---|---|---|---|
| **Ask & Explore** | Analyst Chat | `ChatPanel` | NL Q&A; graph vs. side-by-side vector-RAG; entitlement-wall toggle |
| | Graph Explorer | `GraphExplorer` | **cytoscape** graph: search, domain-filter chips + legend, Force/Tree/Radial layouts, zoom/fit + minimap, node inspector (FIBO grounding, attributes, adjacent triples, lakehouse provenance), SPARQL box |
| | Documents | `DocViewer` | Source filings + chunks; every edge traces to doc/chunk/page/span |
| **Pipeline** | Ingest Pipeline | `IngestPanel` | Real upload (file / text / EDGAR) → live SSE pipeline events |
| | Resolution Queue | `ResolutionQueue` | Steward Merge / Keep-new / Reject, wired to `/resolve/*` |
| | Master Data | `MasterDataManagement` | The 5-step MDM wizard + 4-agent strip → golden record |
| **Analytics** | Risk Dashboard | `RiskDashboard` | Concentration/HHI, heatmap, "explain this" Cypher drawers (**recharts**) |
| | Change Impact | `ImpactFeed` | Versioned-filing diff propagated through the graph |
| | Report Center | `ReportCenter` | 13F drafts, coverage checks, report packs |
| | Evaluation | `EvalPanel` | Offline quality-gate scorecards (extraction precision/recall, grounding, vector-vs-graph, wall leakage) |

Heavy panels (Graph Explorer / Risk Dashboard, which pull cytoscape / recharts) are **code-split**
(`React.lazy`) so the initial bundle stays lean.

### The "fixture-first" data layer (`web/src/api.ts`)

Every getter **tries the live endpoint first and falls back to a committed JSON fixture**, so panels
light up immediately (fixture) and auto-wire the moment the backend endpoint is reachable (live). The
UI **badges which source answered** (`live` vs `fixture`). Key rules:

- A firm-scoped getter for a **real** (non-demo) firm never falls back to the demo fixture — it shows
  a typed empty/offline state instead, so a backend hiccup can't resurface demo data under the wrong
  firm.
- **MDM, FIBO, and onboarding have no fixtures** — they are live-or-empty (the wizard shows "no master
  entities yet" offline; the Add-firm modal surfaces "search unavailable").
- SSE endpoints (`onboard`, `enrich`, `uploadDocument`) are consumed over `fetch` + `ReadableStream`
  (EventSource is GET-only), parsing each frozen `SSEEvent` envelope as it lands.

---

## 5. Tech stack & key decisions

| Layer | Choice |
|---|---|
| Backend | **FastAPI** + SSE (`sse-starlette`), Python ≥ 3.11, managed by `uv` |
| Frontend | **React + Vite + TypeScript**, **cytoscape** (graph), **recharts** (charts), served by nginx |
| Graph + vector | **Neo4j 5 Community** (native 384-d vector index + full-text) |
| App state | **SQLite** (jobs, firms registry, resolution queue, `lh_*` lakehouse tables) |
| LLM | **Claude via the Anthropic API** behind an `LLMProvider` abstraction (Sonnet heavy / Haiku light); `FakeProvider` + cassettes offline |
| Embeddings | `fastembed` + `BAAI/bge-small-en-v1.5` (384-d) full mode; `HashEmbedder` (deterministic) stub |
| Ontology / reasoning | **rdflib + owlrl** (OWL-RL) over a **vendored curated FIBO slice** with real IRIs |
| MDM matching | **rapidfuzz** (base dep), reusing the resolver's `normalize` |
| PDF parsing | **pypdf** (base dep); EDGAR HTML/XBRL via bs4/lxml (`ingest` extra) |
| Discovery | SEC **EDGAR** (`edgartools`) + **GLEIF** (LEI) — free, no key |
| Packaging | **Docker** (multi-stage api + nginx web images) + Docker Compose |

**Settled decisions (do not re-litigate):**

- **Reasoner = rdflib + owlrl**, *not* owlready2/HermiT — HermiT needs a JRE the slim image lacks;
  owlrl gives genuine subclass/domain-range/disjointness entailment, offline and in-process.
- **FIBO = a vendored curated slice of the real ontology** (verified IRIs), import-free so it reasons
  in milliseconds. A fuller vendored FIBO (module import-closure + reasoned snapshot) can drop in
  behind the same API.
- **Lakehouse = its own `lh_*` SQLite tables** in the app DB (like the resolution queue); the frozen
  `stores/sqlite.py` schema is untouched.
- **Dedup = additive canonical-key resolution at write time** — `Company.key` and its uniqueness
  constraint are unchanged (seed + NPORT onboarding keep working); variants snap onto the existing
  node via resolved CIK / normalized name (`Company.norm` index).
- **PDF = pypdf**; the heavy `docling`/torch path stays an opt-in `ingest-tables` extra.
- **Ontology as SSOT + codegen** — one spec generates JSON Schema, DDL, and the prompt card, so
  nothing drifts.
- **Deterministic gates around every LLM step** — structured output → grounding gate → steward; the
  graph stays stable even when LLM prose varies.
- **Offline, free, deterministic CI** — `FakeProvider` + `HashEmbedder` run the whole stack with no
  key and no network; real Claude + fastembed are opt-in (`full` mode).

---

## 6. Feature status

Built and committed on `main` (verified green through `make ci`):

| Feature | Status |
|---|---|
| Ontology SSOT (19 entities / 27 relations → JSON Schema + DDL + prompt card) | ✅ Done |
| Grounded extraction pipeline (classify→parse→chunk→extract→ground→resolve→write, SSE) | ✅ Done |
| FIBO OWL grounding + owlrl reasoner + SPARQL (`/fibo/*`) | ✅ Done |
| Simulated medallion lakehouse (`lh_*`, 4 conflicting source systems) | ✅ Done |
| MDM matching + attribute-level survivorship + golden-record publish (`/mdm/*`) | ✅ Done |
| Canonical-identity dedup at write time + PDF ingest (`pypdf`) | ✅ Done |
| Resolution-queue reconcile — `/resolve/merge` repoints Neo4j + `/reject` + `/promote` | ✅ Done |
| Firm registry + onboarding (EDGAR/GLEIF search → live NPORT) + opt-in enrichment | ✅ Done |
| Risk Lens · Change Impact · Regulatory Reporting (N-fund generalized, firm-scoped) | ✅ Done |
| Analyst Chat / GraphRAG (text-to-Cypher vs. vector, entitlement wall) | ✅ Done |
| **Phase 2 — interactive FIBO Graph Explorer** (search, filters, layouts, minimap, inspector, SPARQL) | ✅ Done |
| **Phase 3 — responsive UI shell** (off-canvas nav + hamburger, fluid graph canvas) | ✅ Done |
| Containerization (multi-stage images, compose stub/full/prebuilt, snapshot restore) | ✅ Done |
| **Evaluation panel backend** (`/eval` harness) | ⏳ Not wired — panel is **fixture-only** (nginx reserves the prefix; no API route yet) |
| **In-flight fixes pass** (eval / resolve / mdm / ingest / graph polish) | 🔧 In progress on branch `feature/financeonto-fixes` |

> Branch note: `feature/financeonto-fixes` is currently even with `main` (all FinanceOnto phases are
> already merged there). The remaining work is a **fixes/polish pass** across eval, resolution, MDM,
> ingest, and the graph explorer, being made on this branch. The untracked `HANDOVER_NEXT_SESSION.md`
> predates these merges and is **stale** — treat the code as the source of truth.

**Guardrail:** all data is neutral/fictional — a repo-wide scan for real client names (excluding the
untracked handover) returns **0** matches.

---

## 7. How to run

### 7.1 Docker (the intended one-command demo)

Prerequisite: Docker Desktop running. Nothing else on the host.

```bash
make bootstrap                 # stub mode (offline, no API key) → http://localhost:5173
make bootstrap MODE=full       # real Claude + fastembed (needs ANTHROPIC_API_KEY in .env)
```

`make bootstrap` runs `docker compose up --build --wait`, starting `neo4j → api → web`; the api
restores a committed graph snapshot on first boot. Everyday commands: `make ps`, `make logs`,
`make down`, `make reset` (wipe volumes), `make snapshot` (re-freeze the seed graph), `make ci`
(offline gate: ruff + pytest). Prebuilt GHCR images:
`docker compose -f docker-compose.yml -f docker-compose.prebuilt.yml up --wait`.

| | **stub** (default) | **full** |
|---|---|---|
| API key | none | `ANTHROPIC_API_KEY` in `.env` |
| LLM | `FakeProvider` (offline) | real Claude (Sonnet / Haiku) |
| Embeddings | `HashEmbedder` | `fastembed` bge-small |
| Graph | restored from committed snapshot | snapshot seed re-embedded; grows via `POST /documents` / onboarding |
| Network | none | Anthropic API (+ EDGAR/GLEIF for live pulls) |

### 7.2 Host dev loop (no Docker for the app itself)

```bash
docker compose up -d neo4j                       # Neo4j only
# clean demo graph + restore the snapshot:
docker exec firm-ontology-neo4j-1 cypher-shell -u neo4j -p firmontology "MATCH (n) DETACH DELETE n;"
rm -f data/app.db
NEO4J_URI=bolt://localhost:7687 PROVIDER_MODE=stub EMBED_BACKEND=hash uv run python -m api.snapshot --restore
# API (all routers) + web:
NEO4J_URI=bolt://localhost:7687 PROVIDER_MODE=stub EMBED_BACKEND=hash SQLITE_PATH="$PWD/data/app.db" \
  EDGAR_USER_AGENT="Firm Ontology Demo demo@example.com" \
  uv run uvicorn api.main:app --host 127.0.0.1 --port 8000 &      # http://localhost:8000
( cd web && npm run dev -- --port 5173 & )                        # http://localhost:5173
make ci                                                           # ruff + offline pytest (keep green)
```

**Gotchas:** the Vite proxy is an allow-list — a new API path prefix must be added to
`web/vite.config.ts` and vite restarted. New base deps (`rdflib`, `owlrl`, `pypdf`) require a Docker
image rebuild. Before a `make ci` run, keep the demo graph pristine (wipe MDM golden nodes:
`MATCH (n {mdm_golden:true}) DETACH DELETE n;`).

---

## 8. Repository layout

```
api/            FastAPI backend
  ontology/     SSOT schema (schema.py) → JSON Schema + DDL + prompt card; models; policies
  providers/    LLMProvider (anthropic / fake + cassettes) + embeddings (fastembed / hash) + factory
  stores/       neo4j.py (driver + DDL + vector index) · sqlite.py (frozen app-state schema)
  extract/      chunking + grounded extraction + the grounding gate
  fibo/         vendored FIBO slice · tbox (rdflib+owlrl) · grounding · reasoner · sparql · routes
  lakehouse/    lh_* medallion store + routes (source systems, gold dims)
  mdm/          matching · survivorship (+ policy.yaml) · models · routes (the 5-step wizard)
  resolution/   resolver · GLEIF · SEC spine · graph_reconcile · provisional queue + routes
  ingest/       POST /documents pipeline + sources (EDGAR/HTML/PDF)
  onboarding/   EDGAR/GLEIF discovery · onboard orchestrator · enrichment · routes
  firms/        firm registry (SQLite) + graph ops + scope + routes
  modules/      risk_lens · change_impact · reg_reports (13F, coverage, packs)
  query/        router · text-to-Cypher agent · synthesizer · GraphRAG
  main.py       app assembly + router wiring + startup registry sync · snapshot.py
web/            React + Vite SPA (App.tsx, api.ts fixture-first layer, components/, fixtures/)
docker/         api.Dockerfile · web.Dockerfile · nginx.conf · api-entrypoint.sh · certs/
corpus/snapshot/ committed demo graph (seed cypher + hash embeddings), restored on stub boot
fixtures/       FakeProvider outputs · lakehouse seed · resolution demo · golden files
docs/           ARCHITECTURE.md · FEATURES.md · PROJECT_SUMMARY.md · QA_REPORT.md (some pre-date FinanceOnto)
docker-compose*.yml   base (stub) · .full.yml (real Claude) · .prebuilt.yml (GHCR images)
```
