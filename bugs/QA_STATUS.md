# QA Verification Status — Firm Ontology Platform (pass 3, re-verify after fixes)

Date: 2026-07-28 · Branch: ws-qa · Live API :8000 (full mode, **still zero credits**) · demo Neo4j 7687 · isolated Neo4j 7690 · web :5173 (not restarted).

Promotion gate `bash scripts/qa_check.sh`: **QA GATE PASS** (ruff + `tests/adversarial` + full `uv run pytest` all green against a freshly-seeded 7690; DB torn down after).

## Final status matrix

| # | Finding | Sev | Owner | Status | Evidence |
|---|---------|-----|-------|--------|----------|
| 1 | `/graph` internal-content entitlement leak | HIGH | orchestrator | **RESOLVED** | `GET /graph` default → 0 internal Chunk nodes (31 nodes); `?entitlements=public&entitlements=internal` → 1 internal Chunk incl. `text` (32 nodes). `/documents` default → 3 public docs (internal_note hidden); wall-off → 4 incl. `internal_note`. |
| 2 | LLM endpoints 500, no graceful degradation | HIGH | apps + graph | **RESOLVED** | `POST /query`, `GET /risk/{fund}/narrative`, `GET /impact/run` all **200** with `narration_unavailable` note ("Error code: 400 … credit balance") and deterministic result intact (hhi / diff.counts / affected_funds present). |
| 3 | Risk Dashboard shape ≠ RiskData | HIGH | ui + apps | **RESOLVED** | `GET /risk` 200 == RiskData field-for-field: `concentration[]{fund,issuer,ticker,weight_pct,value_usd}`, `hhi[]{fund,hhi,top_weight_pct,interpretation,explain}`, `heatmap{companies,categories,cells{company,category,severity:int,severity_language}}`, `single_source[]{supplier,criticality,dependents,exposed_funds,aggregate_weight_pct,explain}`. |
| 4 | Impact Feed shape ≠ ImpactBriefing[] | HIGH | ui + apps | **RESOLVED** | `GET /impact` 200 → `ImpactBriefing[]` field-for-field: `{id,trigger_doc_id,trigger_title,created_at,rule,summary,added/removed/changed:FactTriple[],affected_funds{fund,reason,hops},stale_sections{form_ref,item,title,reason}}`. |
| 5 | Report Center shape ≠ ReportPack | HIGH | ui + apps | **RESOLVED** | `GET /reports` 200 → `ReportPack[]` (len 2): `{report_id,title,period,created_at,sha256,status='final',sections[]{heading,body},provenance[]{claim,doc_id,cypher}}`. |
| 6 | Resolution Queue shape ≠ ProvisionalEntity[] | HIGH | ui + graph | **RESOLVED** | `GET /resolve` 200 → `ProvisionalEntity[]` (len 3): `{id,label,name,aliases,span,doc_id,chunk_id,confidence,candidates[]{existing_id,name,label,score,reason},status}`. |
| 7 | Doc Viewer: `/documents` missing | MED | ui + apps | **RESOLVED** | `GET /documents` 200 → `DocRecord[]` (3) `{doc_id,title,doc_type,sensitivity,filing_date,url,text,spans}`; `internal_note` hidden by default, present only with the internal entitlement. |
| 8 | `/query` request/response contract | MED | ui + graph | **RESOLVED** | `mode:'graph'` → 200 (no 422); echoes `question`/`mode`; `side_by_side` returns `vector_fragments[]`+`vector_answer`. Hero query "Which holdings share a critical supplier?" answers **live 200** via `source=analytics` (deterministic pre-router, zero credits): "4 holdings (AMD, Apple, Broadcom, NVIDIA) share TSMC". Wall ON→`withheld_count=1` (internal hidden, 2 cites); OFF→`withheld_count=0` (internal cited, 3 cites). |
| 9 | Test pollution (HOLDS.shares → reg 13F) | MED | infra + apps | **RESOLVED** | `qa_check.sh` full suite PASS (test_l2_nport collected before test_reg_neo4j; 13F golden passes — no order-dependence). Demo 7687 `HOLDS.shares` = None for all holdings (unpolluted). |
| 10 | `/graph` edge confidence null | LOW | ui + orchestrator | **RESOLVED** | `GET /graph` default: 0/59 edges have null confidence; structural edges numeric (e.g. COVERS=1.0). |
| 11 | Anthropic zero-credit balance | BLOCKED | user-billing | **OPEN (environmental, expected) — MITIGATED** | Still 400 "credit balance too low" on real-Claude calls. Mitigated: hero query answers live via the deterministic analytics pre-router; LLM is used only for optional prose, which now degrades gracefully (see #2). No code fix owed. |

Prior finding `exposed-to-missing-span-provenance` remains **RESOLVED** (four-field provenance invariant green).

## Minor observation (non-blocking)
On the `/query` vector-fallback path (queries the router does NOT map to an analytics tool, e.g. "Which holdings depend on TSMC?"), the entitlement wall still filters citations (wall-ON 3 cites vs wall-OFF 4) but reports `withheld_count=0` rather than ≥1. The wall's data-hiding is correct in both paths; only the vector path's `withheld_count` accounting doesn't surface the withheld internal chunk. All analytics-routed (hero) queries report `withheld_count` correctly. Filed as a note, not a blocker.

## Gate conclusion
No open HIGH (or MEDIUM) code bugs. The only OPEN item (#11) is an environmental billing blocker, expected and mitigated. **Promotion gate is GREEN.**
