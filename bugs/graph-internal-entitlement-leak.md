# GET /graph leaks internal-sensitivity chunk content (entitlement wall bypassed at the data layer)
status: RESOLVED (re-verified 2026-07-28) — GET /graph default excludes the internal Chunk (0 internal nodes); ?entitlements=internal includes it with text; /documents hides internal_note by default.
severity: high
owning_workstream: orchestrator
component: api/graph_view.py (GET /graph)
repro: |
  curl -s http://localhost:8000/graph \
    | python3 -c "import sys,json; d=json.load(sys.stdin); \
      print([{k:n['props'].get(k) for k in ('chunk_id','sensitivity','text')} \
             for n in d['nodes'] if n.get('props',{}).get('sensitivity')=='internal'])"
  # -> [{'chunk_id': 'internal_note_c1', 'sensitivity': 'internal',
  #      'text': 'One hyperscale customer is negotiating multi-year committed capacity that would
  #               push NVDA concentration above the level disclosed in the 10-K.'}]
expected: |
  The internal-information wall is a headline reliability claim of the platform. A read of the
  graph by an unentitled caller must NOT return the text of a sensitivity='internal' :Chunk (the
  synthetic analyst note). Either the node's `text` should be redacted (as `embedding` already is
  in `_clean_props`), or internal-sensitivity nodes/props should be filtered unless the caller
  presents an `internal` entitlement — the same predicate the /query synth already enforces
  (`coalesce(c.sensitivity,'public') IN $ent`).
actual: |
  GET /graph runs `MATCH (n)-[r]->(m) WHERE coalesce(r.confidence,1.0) >= $min_conf` with NO
  sensitivity filter and returns every connected node's full `properties(n)`. The internal chunk
  `internal_note_c1` (connected via MENTIONS to NVIDIA and a RiskFactor) is returned verbatim,
  including its `text`. The exact content the /query wall withholds is freely readable here.
  Confirmed on the live demo DB (7687): the /query filter withholds internal_note_c1 for
  entitlements=['public'], but /graph exposes it to anyone.
severity_rationale: |
  HIGH: this is a data-governance / entitlement bypass, not a cosmetic issue. The demo's core
  "internal-information wall" narrative is falsifiable by one unauthenticated GET. Any UI panel or
  external caller hitting /graph sees the internal note.
fix_hint: |
  In api/graph_view.py: (a) strip `text` (and any sensitive prop) from internal nodes in
  `_clean_props`, and/or (b) add an entitlement predicate to `_SUBGRAPH_CYPHER`
  (`WHERE coalesce(n.sensitivity,'public') IN $ent AND coalesce(m.sensitivity,'public') IN $ent`)
  driven by a request entitlement param defaulting to ['public']. Mirror the synth wall so the two
  code paths cannot diverge.
artifact: scratchpad probe api_results/GET__graph.body (internal_note_c1 present with text)
