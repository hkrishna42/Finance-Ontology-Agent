# QA Report — firm-scoping every feature

QA of the "onboarded firm still shows demo data" defect, driven feature-by-feature with a
find → fix → re-verify loop (QA orchestrator + backend + frontend agents). Running QA env: stub
mode, Neo4j with two firms loaded — `Demo Investment Management` (the committed demo) and a live
**onboarded** firm `PGIM Global Real Estate Fund` (9 funds, 396 holdings, structural-only).

## The defect

After onboarding a real firm and selecting it, **Analyst Chat, Graph Explorer, Documents, and
Change Impact still showed the demo's NVIDIA/TSMC data.** Root cause: only `/risk` and `/reports`
were firm-scoped; the other endpoints read the whole graph or committed demo fixtures. `POST /query`
in particular fell back to a vector search over the demo `:Chunk` set and returned NVIDIA/TSMC
citations regardless of the active firm. Onboarded firms are structural-only (holdings, **no chunks
/ risk-factors / documents**), so the text features had no firm data and leaked the demo.

## Feature-by-feature status

| Feature (endpoint) | Before | After Phase A | Evidence (PGIM active) |
|---|---|---|---|
| **Analyst Chat** (`/query`) | demo NVIDIA/TSMC citations (`source:text_vector`) regardless of firm | firm-scoped; **structural answer** + "run enrichment" note when the firm has no filings; no demo chunks | `source:structural`, 0 citations, answer = *"PGIM… manages 9 funds and holds 396 positions. Top holdings: … NVIDIA CORP (13.7%) …"* — real PGIM data, **0 demo citations** |
| **Graph Explorer** (`/graph`) | whole graph (demo + PGIM mixed, 437 nodes) | `?firm=` → the firm's subgraph; defaults to active firm | PGIM subgraph only (406 nodes at high limit), **no Demo-fund nodes**; `?firm=Demo…` → demo subgraph w/ NVIDIA/TSMC |
| **Documents** (`/documents`) | 3 demo filings for any firm | `?firm=` → the firm's docs; empty pre-enrich | PGIM → `[]` (no filings yet); demo → its held-company docs |
| **Risk Dashboard** (`/risk`) | already scoped (prior phase) | unchanged; helpers de-duplicated into `firms/scope.py` | PGIM funds' concentration/HHI |
| **Report Center** (`/reports`) | already scoped (prior phase) | unchanged | 9 PGIM report packs |
| **Change Impact** (`/impact`) | demo-fixture diff (NVIDIA 10-K/A) for any firm | **deferred to Phase C** (firm-specific impact) | still demo — tracked, next phase |
| **Resolution Queue** (`/resolve`) | global steward queue | unchanged (global by design) | n/a |
| **Evaluation** (`/eval`) | static fixture (no backend route) | unchanged (global) | n/a |

## Root-cause fix (Phase A)

- **`api/firms/scope.py`** (new, shared): `active_firm_name` / `resolve_firm` / `firm_fund_names` /
  `firm_company_names`, lifted from the duplicated `risk_routes`/`reg_routes` helpers.
- **`/query`** (`api/query/*`): a `firm` field; the vector fallback + evidence gather are restricted
  to `:Chunk`s that `MENTIONS` a company the firm holds; a deterministic **structural** answer when
  the firm has no such chunks (never the global demo fallback); an analytics-in-firm guard.
- **`/graph`, `/documents`** (`api/graph_view.py`): `?firm=` scoping to the firm's subgraph / docs.
- **Frontend** (`web/src`): the active firm is threaded to Chat/Graph/Documents; the fixture fallback
  is guarded so a non-demo firm can never resurface the committed NVIDIA fixtures; graceful
  empty/structural states.

## Verification

- Live (PGIM active): Chat/Graph/Documents reflect PGIM with **zero demo fund-names / demo document
  citations / demo-fund graph nodes** (`grep` = 0). Confirmed in the browser — Analyst Chat renders
  PGIM's holdings summary, not the previous NVIDIA 10-K citations.
- Demo regression: `?firm=Demo Investment Management` (and selecting it) restores the demo subgraph +
  documents; the analytics hero queries ("share a critical supplier", "second-order chokepoint")
  still route to structured answers. (The "TSMC disruption" / board-interlock phrasings were
  `text_vector` before and after — pre-existing router behavior, not a regression.)
- Offline gate: `ruff` clean, **370 passed / 60 skipped**.

## Known / deferred

- **Change Impact** firm-scoping → **Phase C**.
- **Text depth for onboarded firms**: pre-enrichment they answer structurally; **Phase B** auto-runs
  enrichment on onboard so Chat cites the firm's own filings and Documents/Risk-factors populate.
- `/documents` scopes to docs mentioning a *held* company (leak-safe); it deliberately does **not**
  broaden to suppliers, which would re-leak demo supply-chain docs via shared issuer nodes.

_Phases B (auto-enrich) and C (firm-specific impact) append below as they land._
