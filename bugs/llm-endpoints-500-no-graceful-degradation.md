# LLM-touching endpoints return HTTP 500 (opaque "Internal Server Error") when the provider fails
status: RESOLVED (re-verified 2026-07-28) — /query, /risk/{fund}/narrative, /impact/run all return 200 with a narration_unavailable note and the deterministic result intact.
severity: high
owning_workstream: apps
also_affects: graph (/query), user-billing (root-cause: zero credit balance)
component: api/query/routes.py, api/modules/risk_routes.py (/narrative), api/modules/impact_routes.py (/run)
repro: |
  curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8000/query \
    -H 'Content-Type: application/json' \
    -d '{"question":"Which holdings depend on TSMC?","mode":"side_by_side","entitlements":[]}'
  # -> 500   body: "Internal Server Error"  (content-type text/plain)
  curl -s -o /dev/null -w '%{http_code}\n' "http://localhost:8000/risk/Demo%20Growth%20Fund/narrative"
  # -> 500
  curl -s -o /dev/null -w '%{http_code}\n' "http://localhost:8000/impact/run"   # narrate=true default
  # -> 500
expected: |
  When the Anthropic call fails (here: 400 invalid_request_error "Your credit balance is too low"),
  the endpoint should degrade gracefully — either return 200 with the deterministic,
  Cypher-derived answer and an honest "LLM narration unavailable" note (risk/impact already compute
  all numbers without the LLM; the synth already builds a grounded answer before the optional LLM
  phrasing), or return a typed error (e.g. 503 {"error":"llm_unavailable","detail":...}) the UI can
  render as a banner. A raw 500 forces the UI's silent fixture fallback, masking that live data was
  available.
actual: |
  The provider error propagates uncaught → FastAPI returns 500 text/plain "Internal Server Error".
  - POST /query: QueryGraph.answer → Synthesizer.synthesize wraps the LLM call in try/except, BUT
    the failing call is elsewhere in the graph/cypher-agent path (the 500 confirms an uncaught
    provider error reaches the handler). /query is fully blocked.
  - /risk/{fund}/narrative: risk_lens.narrate() calls provider.complete() with no guard → 500,
    even though risk_report (all six metrics) is already computed deterministically just above it.
  - /impact/run: defaults narrate=true → get_llm_provider + LLM summary → 500. narrate=false works
    (200) and returns the full deterministic diff+impact, proving the numbers don't need the LLM.
notes: |
  Root cause of the failures right now is the ENVIRONMENTAL zero-credit balance (owner:
  user-billing, see bugs/credits-zero-balance-blocker.md) — NOT a logic bug. This ticket is the
  separate CODE bug: absence of graceful degradation around provider calls. It becomes fully
  testable for correctness once credits are restored, but the 500-instead-of-degrade behavior is
  reproducible today.
severity_rationale: |
  HIGH: /query (the flagship feature) is 100% down with an opaque 500, and /risk/narrative +
  /impact/run throw away already-computed deterministic results instead of returning them.
artifact: scratchpad probe api_results/POST__query_sbs.body ("Internal Server Error")
