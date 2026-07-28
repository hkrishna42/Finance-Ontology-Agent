# GET /graph returns edge.confidence = null for structural edges, but types.ts GraphEdge.confidence is required number
status: RESOLVED (re-verified 2026-07-28) — GET /graph edges all carry numeric confidence (0/59 null; structural edges coalesced, e.g. COVERS=1.0).
severity: low
owning_workstream: ui
also_affects: orchestrator (api/graph_view.py returns raw r.confidence)
repro: |
  curl -s http://localhost:8000/graph | python3 -c "import sys,json;d=json.load(sys.stdin); \
    print(sorted({(e['type'], e['confidence'] is None) for e in d['edges']}))"
  # structural edges (MANAGED_BY, HOLDS, SUBJECT_TO, MENTIONS, COVERS) -> confidence None
expected: |
  types.ts GraphEdge.confidence is typed `number` (0..1, required, non-nullable).
actual: |
  shape_subgraph sets `confidence: row.get("r_conf")` = r.confidence, which is null for structural
  edges that carry no confidence property. So live /graph emits edges with confidence: null. The
  Graph panel is LIVE and tolerant (it renders), so this is cosmetic, but the type contract is
  violated and any strict consumer (or a min-confidence sort) could misbehave.
decision_needed: |
  Either (orchestrator) coalesce to a default in graph_view (`coalesce(r.confidence, 1.0)` in the
  RETURN, matching the WHERE clause already used), OR (ui) widen types.ts GraphEdge.confidence to
  `number | null`.
artifact: scratchpad probe api_results/GET__graph.body (edge0 MANAGED_BY confidence:null)
