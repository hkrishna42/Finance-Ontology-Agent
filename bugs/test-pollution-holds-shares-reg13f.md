# Test pollution: test_l2_nport writes HOLDS.shares into the shared graph, breaking test_reg_neo4j 13F golden
status: RESOLVED (re-verified 2026-07-28) — qa_check.sh full suite PASS (test_reg_neo4j 13F golden passes after test_l2_nport; no order-dependence); demo 7687 HOLDS.shares absent.
severity: medium
owning_workstream: infra
also_affects: apps (victim: tests/test_reg_neo4j.py 13F golden), graph/orchestrator (polluter: api/l2/nport.py + tests/test_l2_nport.py)
type: test isolation / order-dependent failure against a shared live Neo4j
repro: |
  export NEO4J_BOLT_PORT=7690 NEO4J_HTTP_PORT=7477 NEO4J_URI=bolt://localhost:7690 \
         NEO4J_USER=neo4j NEO4J_PASSWORD=firmontology PROVIDER_MODE=stub EMBED_BACKEND=hash
  docker compose up -d neo4j && uv run python scripts/apply_ddl.py && uv run python scripts/load_seed.py

  # (A) clean graph -> PASSES:
  uv run pytest "tests/test_reg_neo4j.py::test_13f_from_graph_matches_golden_both_funds" -q   # 1 passed

  # (B) let test_l2_nport run first (writes HOLDS.shares) -> the SAME test now FAILS:
  uv run pytest tests/test_l2_nport.py \
    "tests/test_reg_neo4j.py::test_13f_from_graph_matches_golden_both_funds" -q               # 1 failed

  # (C) the full suite reproduces it (test_l2_* is collected before test_reg_*):
  uv run pytest -q                                                                            # FAILED test_reg_neo4j
expected: |
  DB-backed tests are isolated: a test that mutates the shared seeded graph restores it (or uses an
  isolated graph), so unrelated tests are order-independent. The 13F golden fixtures encode
  sshPrnamt=0 because the seed intentionally carries NO share counts (confirmed: seed_graph.cypher
  has no `shares`; tests/_apps_seed_fixtures.py:140 "adds no `shares` — absent in the seed"; the
  builder docstring: "seed graph has no share counts, so sshPrnamt/voting are 0").
actual: |
  api/l2/nport.py `_HOLDS_MERGE` writes `SET r.shares = $shares` (an idempotent MERGE on the SAME
  HOLDS edges the seed created). tests/test_l2_nport.py loads an N-PORT fixture (NVIDIA shares =
  2,400,000, MSFT 1,600,000, ...) into the LIVE shared store (`Neo4jStore(get_settings())`,
  _store_or_skip) and does NOT clean up. When tests/test_reg_neo4j.py runs afterward in the same
  session, tf.build_13f_from_graph now reads the injected shares, so the generated 13F has
  sshPrnamt=2,400,000 while the golden has 0 -> `res["xml"] == golden` fails.
  Verified: shares is None immediately after a fresh seed and the golden test passes; it becomes
  2,400,000 after test_l2_nport runs and the golden test then fails. Note test_reg_neo4j's own
  test_report_pack_writes_derived_from_edges_with_cleanup DOES clean up (DETACH DELETE gr), so the
  cleanup discipline exists — test_l2_nport just doesn't follow it.
why_missed: |
  `make ci` runs offline with no Neo4j, so every DB-backed test SKIPs and the collision never
  happens. It only manifests when the full suite runs against a live seeded graph — i.e. the exact
  promotion-gate / integration scenario. The live demo DB (7687) is unaffected because the suite was
  never run against it (HOLDS.shares is None there; the live /reports/13f correctly emits 0).
fix_options: |
  1) (preferred) test_l2_nport writes to an isolated graph/namespace, OR resets the mutation in
     teardown (e.g. `MATCH (:Fund)-[r:HOLDS]->() REMOVE r.shares` / re-run load_seed) so it leaves
     the shared graph as it found it.
  2) Add a session/function fixture that re-seeds (or snapshots+restores) the graph between
     DB-backed test modules.
  3) If HOLDS is INTENDED to carry N-PORT shares in the demo, put `shares` in seed_graph.cypher and
     regenerate the 13F golden fixtures (fixtures/golden/13f_*.xml) with the real counts — a product
     decision (would also change the live 7687 13F to non-zero shares).
severity_rationale: |
  MEDIUM: order-dependent hard failure of the integrated DB-backed suite (breaks a clean gate run),
  not a live-endpoint defect. Also a latent-flakiness smell (shared mutable graph across tests).
artifact: |
  Reproduced this session: clean-seed HOLDS.shares=None + golden test PASS; post-test_l2_nport
  HOLDS.shares=2400000 + golden test FAIL.
