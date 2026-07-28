# POST /query request contract mismatch: UI mode enum + entitlement_wall field don't match backend
status: RESOLVED (re-verified 2026-07-28) — mode:'graph' 200 (no 422); question/mode echoed; side_by_side returns vector_fragments+vector_answer; wall ON->withheld=1 / OFF->withheld=0; hero query answers live via analytics pre-router.
severity: medium
owning_workstream: ui
also_affects: graph (api/query/routes.py QueryRequest + api/query/graph.py QueryAnswer.as_dict)
component: web/src/api.ts runQuery() + web/src/types.ts QueryMode/QueryResponse  vs  api/query/routes.py
repro: |
  # (i) UI's mode value is rejected with 422 (independent of credits):
  curl -s -X POST http://localhost:8000/query -H 'Content-Type: application/json' \
    -d '{"question":"x","mode":"graph","entitlement_wall":true}'
  # -> 422 {"detail":[{"type":"literal_error","loc":["body","mode"],
  #          "msg":"Input should be 'auto', 'vector_only', 'graph_only' or 'side_by_side'","input":"graph"}]}
  # (ii) valid mode passes validation then 500s on credits; entitlement_wall is silently dropped:
  curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8000/query -H 'Content-Type: application/json' \
    -d '{"question":"x","mode":"side_by_side","entitlement_wall":false}'   # -> 500
expected: |
  UI request/response shapes (types.ts) align with the backend contract.
actual: |
  REQUEST mismatches (types.ts / api.ts  vs  QueryRequest):
    - QueryMode = 'graph' | 'side_by_side'  vs  backend Literal 'auto'|'vector_only'|'graph_only'|'side_by_side'.
      UI's 'graph' is INVALID -> 422 before any work. Backend has no 'graph'; closest is 'graph_only'/'auto'.
    - api.ts sends `entitlement_wall: boolean`; backend QueryRequest expects `entitlements: list[str]`
      and (Pydantic default) IGNORES the unknown `entitlement_wall`. => the wall toggle has NO effect
      on the live endpoint; `entitlements` always defaults to [] => internal sources always withheld,
      regardless of the UI switch. (The data-layer wall itself is correct — see synth.py — it just
      never receives the UI's intent.)
  RESPONSE mismatches (QueryAnswer.as_dict  vs  types.ts QueryResponse) — relevant once credits return:
    - backend omits `question` and `mode` (UI expects them echoed).
    - backend adds `source` and (side_by_side) `alternatives`; not in types.ts.
    - side_by_side: UI expects top-level `vector_fragments[]` + `vector_answer`; backend nests the
      vector answer under `alternatives.vector` and returns no `vector_fragments`.
    - citations: backend items are {source, tool?/cypher?} or {source:'document', doc_id, chunk_id,
      sensitivity, snippet}; types.ts Citation expects {id, doc_id, chunk_id, page?, span, title?}.
      `snippet` vs `span`, and no `id`/`title`.
decision_needed: |
  (ui) change QueryMode to the backend's 4-value enum (map the toggle to graph_only/side_by_side),
  send `entitlements: string[]` (e.g. ['public'] or ['public','internal']) instead of
  `entitlement_wall`, and align QueryResponse (question/mode/source/alternatives, citation.span vs
  snippet). OR (graph) accept `entitlement_wall` + a 'graph' alias and echo question/mode +
  vector_fragments/vector_answer in the response.
note: |
  Currently every /query call 500s on zero credits, so the UI shows the fixture regardless; these
  contract mismatches will surface the moment credits are restored.
artifact: scratchpad probe api_results/POST__query_uibody_graph.body (422), POST__query_uibody_sbs.body (500)
