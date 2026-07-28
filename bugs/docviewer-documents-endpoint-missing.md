# Doc Viewer is fixture-locked: GET /documents endpoint does not exist anywhere in the backend
status: RESOLVED (re-verified 2026-07-28) — GET /documents returns 200 DocRecord[] (3 public docs) and hides internal_note by default; included only with the internal entitlement.
severity: medium
owning_workstream: ui
also_affects: orchestrator/apps (no /documents route implemented; Document/Chunk data only via /graph)
ui_panel: web/src/components/DocViewer.tsx
repro: |
  curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/documents     # -> 404
expected: |
  web/src/api.ts getDocuments() fetches GET /documents and expects types.ts DocRecord[]:
    DocRecord{ doc_id, title, doc_type, sensitivity, filing_date?, url?, text,
      spans: DocSpan[]{chunk_id, quote, label?, sensitivity?} }
actual: |
  No backend router serves /documents (grep of api/ for a documents route: none). GET /documents
  -> 404 -> fixtures/documents.json (FIXTURE). The seed DOES contain Document + Chunk nodes with the
  needed data (doc_id, title, doc_type, sensitivity, filing_date, chunk text/page), so the endpoint
  is implementable; it just was never built. NOTE: whoever builds it MUST apply the entitlement wall
  (the internal Document `internal_note` / Chunk `internal_note_c1` must be redacted/withheld for
  unentitled callers — see bugs/graph-internal-entitlement-leak.md).
decision_needed: |
  Either (apps/orchestrator) add GET /documents returning DocRecord[] from the Document/Chunk graph
  with entitlement filtering, OR (ui) treat DocViewer as an intentional fixture-only panel for now
  and badge it as such.
artifact: scratchpad probe api_results (GET /documents -> 404)
