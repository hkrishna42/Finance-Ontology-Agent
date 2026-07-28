# QA Report — firm-scoping every feature

QA of the "onboarded firm still shows demo data" defect, driven feature-by-feature with a
find → fix → re-verify loop (QA orchestrator + backend/frontend changes against a frozen contract).
Running QA env: **stub mode** (free — no Anthropic credits), Neo4j with two firms — `Demo Investment
Management` (the committed demo) and a live **onboarded** firm `VANGUARD EXPLORER FUND` (1 fund, 717
holdings) used to exercise the onboard → enrich → verify loop. All evidence below is from the live
stack (api :8000, neo4j `firm-ontology-neo4j-1`).

## The defect

After onboarding a real firm and selecting it, **Analyst Chat, Graph Explorer, Documents, and
Change Impact still showed the demo's NVIDIA/TSMC data.** Root cause: only `/risk` and `/reports`
were firm-scoped; the other endpoints read the whole graph or committed demo fixtures. `POST /query`
in particular fell back to a vector search over the demo `:Chunk` set and returned NVIDIA/TSMC
citations regardless of the active firm. Onboarded firms were structural-only (holdings, **no chunks
/ risk-factors / documents**), so the text features had no firm data and leaked the demo.

## Feature-by-feature status

| Feature (endpoint) | Before | After (Phases A + B) | Evidence (VANGUARD EXPLORER FUND active, stub) |
|---|---|---|---|
| **Analyst Chat** (`/query`) | demo NVIDIA/TSMC citations regardless of firm | firm-scoped; cites the firm's **own** filings once enriched; a deterministic **structural** answer (never the demo index) when a firm has no filings | `source:text_vector`, cites the firm's **own** 10-Ks + prospectus (e.g. Bloom Energy 10-K); **0 demo citations** |
| **Graph Explorer** (`/graph`) | whole graph (demo + firm mixed) | `?firm=` → the firm's subgraph (+ its enriched 10-K chunks); defaults to the active firm | VANGUARD subgraph: 830 nodes (1 fund, 718 companies, **111 enriched 10-K chunks**), **no Demo-fund nodes**; `?firm=Demo…` → demo subgraph w/ NVIDIA/TSMC |
| **Documents** (`/documents`) | 3 demo filings for any firm | `?firm=` → the firm's docs (held-company mentions **or** `Document.firm` provenance); empty pre-enrich | VANGUARD → its **4** ingested filings (3 holdings' 10-Ks + the fund 485BPOS); demo → `nvda_10k` |
| **Risk Dashboard** (`/risk`) | already scoped (prior phase) | unchanged; helpers de-duplicated into `firms/scope.py` | the firm's funds' concentration/HHI |
| **Report Center** (`/reports`) | already scoped (prior phase) | unchanged | the firm's report packs |
| **Change Impact** (`/impact`) | demo-fixture diff (NVIDIA 10-K/A) for any firm | **deferred to Phase C** (firm-specific impact) | still demo — tracked, next phase |
| **Resolution Queue** (`/resolve`) | global steward queue | unchanged (global by design) | n/a |
| **Evaluation** (`/eval`) | static fixture (no backend route) | unchanged (global) | n/a |

## Phase A — firm-scope every read endpoint (committed `baa0f21`)

- **`api/firms/scope.py`** (new, shared): `active_firm_name` / `resolve_firm` / `firm_fund_names` /
  `firm_company_names`, lifted from the duplicated `risk_routes`/`reg_routes` helpers.
- **`/query`** (`api/query/*`): a `firm` field; the vector fallback + evidence gather are restricted
  to `:Chunk`s reachable from the firm's held companies; a deterministic **structural** answer when
  the firm has no such chunks (never the global demo fallback); an analytics-in-firm guard.
- **`/graph`, `/documents`** (`api/graph_view.py`): `?firm=` scoping to the firm's subgraph / docs.
- **Frontend** (`web/src`): the active firm is threaded to Chat/Graph/Documents; the fixture fallback
  is guarded so a non-demo firm can never resurface the committed NVIDIA fixtures.

## Phase B — auto-enrich on onboard (real filing data for onboarded firms)

Onboarding now **auto-enriches** by default (`enrich=false` to skip). `enrich_firm` pulls, hard-capped
at `MAX_FILINGS=8` / `FILING_MAX_CHARS=24000` per filing:

1. the firm's **top holdings' 10-K Item 1A** (Risk Factors), and
2. **each fund's 485BPOS prospectus** (Principal Risks),

runs each through the reused `ingest_document` pipeline (real `:Chunk` writes in **both** modes;
LLM extraction is rich only in `full`), and a deterministic (LLM-free) `link_subject` pass stamps
`Document.firm` and MENTIONS-links every chunk to its subject (the held `Company` for a 10-K, the
`Fund` for a prospectus). A `stage="enriching"` SSE stage streams progress; the Add-firm modal shows
it and offers a per-firm manual "Enrich".

Four fixes landed this session to make the enriched firm's features actually reflect it:

1. **`/documents` provenance scope** (`api/graph_view.py::_DOCS_FIRM_CYPHER`) — a Document qualifies
   if `d.firm = $firm` **OR** a chunk of it MENTIONS a held company. The `d.firm` arm is what
   surfaces a fund's own **prospectus** (its chunks MENTION the *Fund*, so the held-company arm alone
   misses it). *Verified: `/documents?firm=VANGUARD…` returns its 4 ingested filings; a fresh firm → `[]`.*
2. **Best-effort filing anchor** (`api/onboarding/enrich.py::_bounded_excerpt`) — anchor on the
   `item 1a` / `risk factors` heading, else fall back to the head of the filing, always truncated to
   `FILING_MAX_CHARS`. Keeps every filing's chunk count (== extraction calls) bounded. *Verified:
   enriched filings are 21–59 chunks each, never the ~1,400 an unbounded 10-K produces.*
3. **Name-based 10-K resolution** (`api/onboarding/enrich.py`, `api/ingest/sources.py::cik_from_name`)
   — **N-PORT identifies holdings by name + CUSIP/LEI, rarely a ticker** (VANGUARD had a ticker on 1
   of 717 holdings — a Russell-2000 futures contract). The ticker-keyed 10-K fetch therefore found no
   real equity holdings. Fix: select top holdings by weight (ticker no longer required, junk `N/A`
   rows filtered), and fetch each by ticker when present, else by the **CIK resolved from its name**
   via edgartools (`Company()` accepts a CIK). Best-effort: a holding that resolves to nothing is a
   non-fatal skip. *Verified: Bloom Energy / Comfort Systems / Credo Technology all resolved by name →
   CIK → 10-K and ingested (57 / 33 / 21 chunks); enrichment `enriched=3, failed=0`.*
4. **Enriched-firm chat vector fallback** (`api/query/graph.py`) — when graph-first (analytics /
   text-to-Cypher / entity chunks) grounds nothing for a firm that **has** ingested filings, fall
   back to the firm's **own** (firm-scoped) chunks via the vector index rather than returning an
   empty "no evidence" answer while the side-by-side panel shows them. Firm-scoped, so never a global
   (demo) search. *Verified: a "risks" question on the enriched firm now returns `source:text_vector`
   with citations to its own 10-K/prospectus, not a dead-end analytics answer.*

### Phase B verification (VANGUARD EXPLORER FUND, stub, onboard WITH enrich)

- **Enrichment ran bounded:** `enriched=3` holdings' 10-Ks + `funds_enriched=1` prospectus,
  `failed=0`; per-filing chunk counts **57 / 33 / 21 / 59** (all ≤ `FILING_MAX_CHARS` bound).
  `risk_factors_added=0` in stub (extraction is rich only in `full` — the chunks are real either way).
- **Chat cites the firm's OWN filings:** `source:text_vector`, citations to its 10-Ks + prospectus;
  `"What risks does Bloom Energy disclose?"` cites Bloom Energy's 10-K.
- **Documents / Graph reflect the firm:** `/documents` → its 4 filings; `/graph` → 830 nodes incl.
  111 enriched 10-K chunks, **no demo-fund nodes**.
- **Zero demo leak:** `grep -iE "Demo Growth Fund|Demo Focused|nvda_10k|tsmc_20f|Demo Investment
  Management"` across all VANGUARD `/query`+`/graph`+`/documents` responses = **0** (bare "NVIDIA" is a
  legitimate shared holding, not a leak).
- **Demo regression clean:** with the demo firm, the hero queries still route to structured analytics
  ("share a critical supplier" → AMD/Apple/Broadcom/NVIDIA → TSMC), `/documents` → `nvda_10k`, `/graph`
  → its 18-node NVIDIA/TSMC subgraph.
- **Offline gate:** `make ci` (ruff + pytest) green on a clean demo; new offline tests cover the
  `d.firm` docs scope, the name→CIK holding resolution, and the enriched-firm vector fallback.

## Known / deferred

- **Change Impact** firm-scoping → **Phase C** (not started).
- **Full-mode depth:** stub enrichment writes real `:Chunk`s (filing-grounded chat) but adds **0
  `RiskFactor`s** — rich extraction (risk factors / `EXPOSED_TO` / `SUPPLIES_TO`) requires a `full`-mode
  onboard (bounded Anthropic spend). Deferred by choice to keep QA free.
- **Prospectus chunks in the Graph Explorer:** the firm subgraph includes enriched **10-K** chunks
  (they MENTION a held company) but not **prospectus** chunks (they MENTION the *Fund*); the
  prospectus is still fully visible in Documents via `d.firm`. Minor completeness item.
- **Name-resolution precision:** `cik_from_name` takes edgartools' top search hit; a highly ambiguous
  holding name could resolve to the wrong issuer (best-effort, non-fatal). Fine for the POC.

_Phase C (firm-specific Change Impact) appends below when it lands._
