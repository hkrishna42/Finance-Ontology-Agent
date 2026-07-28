# ENVIRONMENTAL BLOCKER: Anthropic account has zero credit balance — all real-Claude calls fail
status: OPEN (environmental, expected) — MITIGATED 2026-07-28: hero query answers live via the deterministic analytics pre-router; LLM prose degrades gracefully (200 + narration_unavailable). Still 400 credit error on real-Claude calls.
severity: high
owning_workstream: user-billing
type: environmental blocker (NOT a code bug)
repro: |
  curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8000/query \
    -H 'Content-Type: application/json' \
    -d '{"question":"Which holdings depend on TSMC?","mode":"side_by_side","entitlements":[]}'
  # -> 500 (underlying provider error: 400 invalid_request_error "Your credit balance is too low")
expected: |
  PROVIDER_MODE=full with a funded ANTHROPIC_API_KEY so LLM endpoints return real completions.
actual: |
  Every real-Claude call returns 400 invalid_request_error "Your credit balance is too low". This
  blocks end-to-end verification of all LLM-dependent features:
    - POST /query (all modes: auto/graph_only/vector_only/side_by_side; entitlement-wall behavior)
    - GET /risk/{fund}/narrative
    - GET /impact/run (narrate=true default)
    - live ingest extraction (any real-LLM extraction path)
  Deterministic (Cypher/analytics-only) endpoints are UNAFFECTED and were verified green (see the
  QA test matrix in the report).
blocked_verifications: |
  - /query answer quality, citations, graph_paths, side-by-side vector baseline.
  - The entitlement WALL end-to-end via /query (withheld_count in the HTTP response). NOTE: the
    underlying data-layer wall was verified independently by direct store query on 7687
    (entitlements=['public'] withholds internal_note_c1; ['public','internal'] reveals it) and by
    code review of synth.gather_evidence_chunks — so the wall LOGIC is confirmed; only the HTTP
    round-trip is blocked.
action: |
  user-billing: add credit to the Anthropic account (or point PROVIDER_MODE=full at a funded
  key / Bedrock). Independently, see bugs/llm-endpoints-500-no-graceful-degradation.md for the code
  gap that turns this environmental failure into an opaque HTTP 500 instead of a graceful degrade.
