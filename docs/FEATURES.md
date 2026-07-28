# Features

What the Firm Ontology Platform does. See [ARCHITECTURE.md](ARCHITECTURE.md) for how it's built.

## Firm onboarding (the platform is firm-agnostic)

The firm is a **runtime variable** — the repo ships an anonymized demo, and any real firm is added on
demand.

- **Search** — type a firm name; `POST /firms/search` queries SEC EDGAR (fund families + their series)
  and GLEIF (LEI), returning ranked candidates to disambiguate (handles namesakes).
- **Onboard** — pick a candidate; `POST /firms/onboard` streams live progress (SSE) while a
  deterministic, **no-API-key** pipeline pulls each fund's latest **NPORT-P** holdings from EDGAR,
  writes Fund + weighted `HOLDS` + `MANAGED_BY` into the graph, resolves holding issuers to the
  SEC/GLEIF spine, and saves the firm to the registry.
- **Select / manage** — the header **firm selector** lists onboarded firms (`GET /firms`), switches
  the active firm (`POST /firms/{id}/select`), and removes one (`DELETE /firms/{id}`, which also
  cleans its graph nodes). The active firm scopes every analytics panel; `?firm=<name>` overrides.
- **Add-firm UI** — "+ Add firm…" opens a modal: name → candidate list → live onboarding progress →
  auto-select.
- Verified live against real EDGAR (e.g. Vanguard Explorer Fund → 719 holdings).

## The three analytics apps

Each reads only from the provenance-carrying graph and is **N-fund generalized** (works for a firm
with any number of funds, not a hardcoded two).

### Risk Lens (`/risk`)
Concentration + Herfindahl-Hirschman Index (HHI) per fund, a risk-factor exposure heatmap,
single-source supplier flags, and top-shared-supplier detection. Every number has an "explain this"
drawer showing the exact read-only Cypher and the edges it traversed. Cross-fund comparison (each fund
vs. the firm average). Endpoints: `GET /risk`, `/risk/{fund}/report`, `/risk/{fund}/metric/{m}`,
`/risk/{fund}/narrative`, `/risk/compare`.

### Change Impact (`/impact`)
`SUPERSEDES` document versioning, fact-diffing, and propagation of a new filing's changes through the
graph per `impact_policy.yaml` — a live 8-K ripples to the affected funds and flags stale disclosure
sections/reports, with an Impact Narrator briefing.

### Regulatory Reporting (`/reports`)
13F draft generation (XML + CSV, golden-file tested) per fund, a principal-risks coverage check, and
report packs (Jinja templates; WeasyPrint PDF) with a provenance appendix + SHA-256.

## Analyst Chat & query (`/query`)
Natural-language questions answered from the graph, grounded in cited spans and a reviewable Cypher
plan (text-to-Cypher with a read-only guard + self-correction retries), compared **side-by-side against
a plain vector-RAG baseline** to show where graph traversal beats vector similarity on multi-hop
questions. A deterministic pre-router answers the hero queries with **zero credits**. An
**entitlement wall** toggle demonstrates access control (see below).

## Graph Explorer (`/graph`)
The ontology-consistent knowledge subgraph, filterable by extraction confidence, with the hero-query
paths (shared critical supplier, second-order chokepoint, board interlock) highlightable. Entity types
are colour-coded; structural edges are confidence 1.0, semantic edges carry their extracted confidence.

## Documents & provenance (`/documents`)
The source filings with their chunks; every graph edge traces back to a doc_id + chunk_id + page +
verbatim span. Extraction is span-grounded (a claim must fuzzy-match its chunk ≥ 0.9) and steward-
validated before it is written.

## Ontology enrichment (opt-in, `/firms/{id}/enrich`)
Layers the **semantic** ontology onto an onboarded firm's **structural** graph: runs the existing LLM
extraction pipeline over the firm's top-N holdings' 10-K risk factors to add RiskFactor /
DisclosureSection / supply-chain (`EXPOSED_TO`, `SUPPLIES_TO`) edges — what the Risk/Impact tasks
consume. Bounded spend (top clamped ≤ 5, one shared provider). Full mode (real Claude); works offline
with FakeProvider.

## Resolution queue (`/resolve`)
Entity resolution pins issuer mentions to a CIK/LEI spine (exact ticker → normalized alias → embedding
cosine → LLM adjudication); genuinely ambiguous mentions land in a provisional review queue rather than
guessing.

## Entitlement wall
Reads are entitlement-aware at the data layer: nodes/edges/documents tagged `sensitivity: internal`
are excluded unless `internal` is in the caller's entitlements (default `["public"]`). The demo
includes one clearly-synthetic internal analyst note to show the wall hiding/revealing content.

## Two run modes

| | **stub** (default) | **full** |
|---|---|---|
| API key | none | `ANTHROPIC_API_KEY` |
| LLM | FakeProvider (offline, deterministic) | real Claude (Sonnet 5 / Haiku 4.5) |
| Embeddings | HashEmbedder | fastembed bge-small (baked into the image) |
| Onboarding | structural pull needs network only (no key) in both modes | + LLM enrichment |
| Graph | committed anonymized demo snapshot | same seed, re-embedded with fastembed; grows via onboarding/ingest |
