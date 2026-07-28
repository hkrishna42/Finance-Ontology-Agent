# EXPOSED_TO edges lack the canonical `span` provenance field
status: RESOLVED (fixed on main, verified 2026-07-27)
severity: medium
owning_workstream: graph
resolution: |
  Fixed in the integrated seed (fixtures/seed_graph.cypher). The EXPOSED_TO write now sets
  `e.span = x.sev` alongside `e.severity_language`:
      MERGE (co)-[e:EXPOSED_TO]->(r)
      SET e.severity_language = x.sev, e.span = x.sev, e.doc_id = 'nvda_10k',
          e.confidence = 0.9, e.sensitivity = 'public';
  Verified against the live seeded demo graph (bolt://localhost:7687): all 12 EXPOSED_TO edges
  now carry a non-null `span`, and span == severity_language on every edge. The QA harness marker
  was flipped from strict-xfail to a normal passing assertion; the full four-field provenance
  invariant (doc_id + span + confidence + sensitivity) is now enforced on both SUPPLIES_TO and
  EXPOSED_TO in tests/adversarial/test_provenance_invariants.py.
repro: |
  export NEO4J_URI=bolt://localhost:7687
  uv run python - <<'PY'
  import os
  from neo4j import GraphDatabase
  d = GraphDatabase.driver(os.environ["NEO4J_URI"], auth=("neo4j","firmontology"))
  with d.session() as s:
      row = s.run("MATCH ()-[r:EXPOSED_TO]->() RETURN count(r) AS total, "
                  "sum(CASE WHEN r.span IS NULL THEN 1 ELSE 0 END) AS no_span").single()
      print(dict(row))   # -> {'total': 12, 'no_span': 0}
  d.close()
  PY
expected: |
  Every SUPPLIES_TO / EXPOSED_TO edge carries the PROVENANCE_FIELDS-declared `span` (verbatim
  source sentence), so a citation/grounding renderer can read `.span` uniformly across semantic
  edges.
actual_before_fix: |
  All 12 EXPOSED_TO edges were missing `span` (verbatim text lived only under `severity_language`).
actual_after_fix: |
  All 12 EXPOSED_TO edges carry `span` (== `severity_language`). Invariant green.
artifact: |
  tests/adversarial/test_provenance_invariants.py
