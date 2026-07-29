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
| **Change Impact** (`/impact`) | demo-fixture diff (NVIDIA 10-K/A) for **any** firm | `?firm=` → only the demo shows the illustrative fixture; a real firm shows an empty "no change events yet" feed, never the demo fixture; `funds_holding` firm-filtered | demo → the NVIDIA 10-K/A briefing (● live); VANGUARD → the empty state (● live) |
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

## Phase C — firm-scope Change Impact

`GET /impact` loaded a committed v1→v2 **fixture** (the demo's NVIDIA 10-K/A change), diffed +
propagated it against the live graph, and returned it for **any** active firm — the last demo leak.

- **`api/modules/impact_routes.py`** — `impact_collection` gains `?firm=` and resolves the active
  firm via `firms/scope`. Only the **demo** firm (or a firm-less install) shows the illustrative
  fixture briefing; a real onboarded firm returns `[]` (a clean "no change events yet" feed), never
  the demo's NVIDIA fixture.
- **`api/modules/change_impact.py`** — `Neo4jResolver(store, firm=…)`: `funds_holding` gains a firm
  filter (funds `MANAGED_BY` the firm), so a change's downstream fund set can't bleed across firms
  via a shared issuer. The demo path scopes to the demo firm.
- **Frontend** (`web/src`): `getImpact(firm)` sends `?firm=` and firm-guards the fixture fallback
  (a real firm never falls back to the committed NVIDIA fixture); `ImpactFeed` takes the active firm
  and renders a "No change events yet" empty state.

**Verified in the browser** (stub): with **VANGUARD EXPLORER FUND** active, Change Impact shows the
"No change events yet" empty state with a **● live** badge (the backend's `[]`, not a fixture); with
**Demo Investment Management** active, it shows the full NVIDIA 10-K/A briefing (added `NVIDIA
SUPPLIES_TO SK Hynix`; changed `NVIDIA EXPOSED_TO Customer concentration`; affected Demo Growth /
Focused Growth funds at 1 hop; two 485BPOS sections stale) — also **● live**. `GET /impact?firm=…`
confirms it at the API: demo → 1 briefing, VANGUARD / any other firm → `[]`.

Firm-specific change *detection* (diffing a firm's own re-ingested filing versions) needs a
re-onboard-with-newer-version flow that records `SUPERSEDES` + a versioned fact set — the current
model ingests one version per firm, so real firms are legitimately empty until then (follow-up).

## Concurrency fix found during Phase C QA — `/firms` 500 under load

Live QA surfaced a **pre-existing** bug unrelated to firm-scoping: `GET /firms` and `/firms/active`
returned **500** for *every* concurrent request (`sqlite3.ProgrammingError: SQLite objects created in
a thread can only be used in that same thread`) — so the UI's firm selector, which fires both on
load, fell back to "registry offline — selection disabled", silently breaking firm switching. Root
cause: FastAPI runs sync routes and their SQLite generator-dependency setup/teardown across anyio
threadpool threads, tripping SQLite's default same-thread guard. Fix: open connections with
`check_same_thread=False` in `api/stores/sqlite.py::connect` (each request still gets its own
connection used serially, so the guard is safe to relax). *Verified: 40/40 concurrent `/firms`
requests now 200 (was 40/40 → 500); the firm selector loads and switches firms live.*

_All three phases (A firm-scope reads · B auto-enrich · C firm-scoped impact) are complete and
verified; the offline gate (`make ci`) is green and the private-firm-name scrub guardrail is clean._

---

# FinanceOnto program — FIBO-grounded MDM (branch `feature/financeonto-mdm`)

A separate, larger program (approved plan `~/.claude/plans/let-s-continue-with-the-iridescent-tower.md`):
evolve the platform toward the client "FinanceOnto" mockup — FIBO OWL grounding, a simulated internal
lakehouse, Master Data Management golden records, upload→graph with dedup, an interactive graph, and a
responsive UI. Built with **neutral/fictional data only** (the private-firm-name guardrail stays clean). Phase 1 (the
MDM core) is complete; Phases 2–3 remain. See `HANDOVER_NEXT_SESSION.md` for the full next-session brief.

## Phase 1 — MDM core (complete; 8 commits, `make ci` green, 475 passed)

| Capability | What landed | Evidence |
|---|---|---|
| **FIBO grounding + reasoner** (`api/fibo/`) | Vendored real FIBO/OMG-Commons OWL slice (verified IRIs + subclass + `owl:disjointWith`), `rdflib` load + `owlrl` OWL-RL reasoning, deterministic grounding, SPARQL; `/fibo/*` | `GET /fibo/classes` → 14 classes; reasoner flags a deliberate disjointness violation; genuine OWL, offline |
| **Real-estate ontology** (`schema.py`) | Additive `EntitySpec.fibo_class` + RealProperty/Portfolio/Lease/Loan/Valuation + RE relations; `Company` → `cmns-org:LegalEntity`; `Company.norm` index | extraction JSON schema unchanged for the structural types; `neo4j_ddl` covers the new labels |
| **Simulated lakehouse** (`api/lakehouse/`) | SQLite medallion (source systems + trust, bronze/silver/gold dims, per-attribute lineage) seeded with 4 systems + deliberately conflicting records | `GET /lakehouse/source-systems` → 4 systems (trust 95/88/86/80) |
| **MDM golden record** (`api/mdm/`) | Matching (blocking + normalize + rapidfuzz → confidence %) + declarative attribute-survivorship engine + publish (Gold dim + lineage + canonical Neo4j node); 5-step `/mdm/*` wizard | `POST /mdm/merge {RealProperty:harborview_tower}` → **96.2%**, golden `PROP-1001`, `Lakehouse.dim_property (PK: PROP-1001)`, FIBO-grounded node written |
| **Pipeline FIBO grounding** (Agent B) | `_entity_node_props` stamps `fibo_class`/`fibo_grounded`/`reasoning_valid` on every ingested entity | ingested Company → `cmns-org:LegalEntity`; bond-issuer role → `CorporateDebtIssuer` |
| **Canonical-identity dedup** (`pipeline.py`) | Variant Company mentions snap onto one node by resolved CIK / normalized name (node write + MENTIONS + steward triples); `Company.key` unchanged | seed "NVIDIA" (cik 0001045810) + "NVIDIA Corporation" → one node |
| **PDF ingest** (`sources.py`) | `pypdf` born-digital text in `source_from_bytes`; scanned → points at the opt-in OCR extra | born-digital PDF fixture extracts text through the stub pipeline |
| **MDM wizard + upload UI** (`web/src`) | The 5-step golden-record wizard (4-agent strip, entity cards, conflicting sources + trust, matching %, survivorship table, golden record) + a real file/text upload streaming the pipeline | browser-verified at localhost:5173 → **Master Data** |

## Resolution-queue reconcile · Phase 2 · Phase 3 — complete (3 commits, `make ci` green, 487 passed)

The three items left after the MDM core are done, each its own `make ci`-green commit, live-verified
in the browser (stub mode, demo firm). The guardrail grep stays at **0**.

| Capability | What landed | Evidence |
|---|---|---|
| **Resolution-queue reconcile** (`89a79f5`) | `api/resolution/graph_reconcile.py::repoint_entity` moves a duplicate mention node's MENTIONS + semantic edges onto the canonical node, merges props (canonical identity wins), `DETACH DELETE`s the dup — **no APOC** (relation types discovered at runtime + substituted only as ontology-validated literals). `/resolve/merge` repoints when a `canonical_key` is given (best-effort, never 500s the audit write; `cik` now optional); new `/resolve/promote` + `/resolve/reject` routes; `ResolutionQueue.tsx` Merge/Keep/**Reject** buttons call them for live rows (demo-fixture rows stay local). Upload path threads a request-scoped `queue_conn` + stamps `d.source` provenance. | Live: merging provisional "Acme Corp" onto "Acme Corporation" moved both edges + `cik` onto the canonical node and removed the duplicate (`moved:2, dup_deleted:true`). Offline: repoint asserted against a recording fake store; +12 tests. |
| **Phase 2 — interactive FIBO graph** (`a856ac3`) | `GraphExplorer.tsx` rebuilt: search (name/type/CIK/ticker/ISIN/FIBO class), domain filter chips w/ counts + colour legend, Force/Tree/Radial layouts (cose/breadthfirst/concentric — no dagre dep) + zoom + a custom SVG minimap (viewport rect + click-to-pan), a rich node inspector (FIBO grounding via `/fibo/ground`, extracted attributes, adjacent triples, lakehouse provenance), and a SPARQL box over the reasoned TBox (`POST /fibo/sparql`). | Live: search→NVIDIA; Radial re-layout; inspector showed `cmns-org:LegalEntity` + 8 adjacent triples; RiskFactor domain hidden; SPARQL returned 16 TBox rows. |
| **Phase 3 — responsive UI** (`c8ca683`) | Off-canvas sidebar drawer + hamburger + dimmed scrim + close-on-nav (`navOpen` in `App.tsx`); topbar pills hide at 860/680px, title truncates then hides at 520px, firm selector narrows so the theme toggle never clips; fluid graph canvas (`clamp(360px,60vh,600px)`); new `menu` icon. | Live-verified at mobile (375), tablet (768), desktop (unchanged: full sidebar, all pills, no hamburger). |

**FinanceOnto program status: Phase 1 (MDM core) + resolution reconcile + Phases 2–3 all complete.**
`make ci` green (487 passed, 2 skipped); the client-name scrub guardrail grep = 0; neutral/fictional data only.
