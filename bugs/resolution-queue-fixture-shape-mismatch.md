# Resolution Queue is fixture-locked: GET /resolve 307->405, /resolve/queue shape != types.ts ProvisionalEntity[]
status: RESOLVED (re-verified 2026-07-28) — GET /resolve returns 200 ProvisionalEntity[] (3 demo items) matching types.ts field-for-field (label,name,aliases,span,doc_id,chunk_id,candidates{existing_id,name,label,score,reason}).
severity: high
owning_workstream: ui
also_affects: graph (api/resolution/routes.py + store.py — queue item shape; wrapped in {count,items})
ui_panel: web/src/components/ResolutionQueue.tsx
repro: |
  curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/resolve     # -> 307 -> /resolve/ -> 405
  curl -s http://localhost:8000/resolve/queue                                 # -> {"count":0,"items":[]}
expected: |
  web/src/api.ts getResolutionQueue() fetches GET /resolve and expects types.ts ProvisionalEntity[]:
    ProvisionalEntity{ id, label: EntityLabel, name, aliases[], span, doc_id, chunk_id, confidence,
      candidates: ResolutionCandidate[]{existing_id,name,label,score,reason?}, status? }
actual: |
  1) PATH: there is no GET /resolve. The router registers POST "/" (=> /resolve/), GET /resolve/queue,
     GET /resolve/provisional, POST /resolve/merge. GET /resolve -> 307 redirect to /resolve/ ->
     405 Method Not Allowed. api.ts fetch('/resolve') therefore fails -> fixtures/resolve.json (FIXTURE).
  2) SHAPE: /resolve/queue (and /provisional) return {count, items:[...]} where each item
     (store._row_to_dict) is:
       { id, mention, normalized, ticker, candidate_cik, candidate_lei, candidate_title,
         method, confidence, status, candidates, created_at, updated_at }
     vs types.ts ProvisionalEntity (field <- backend):
       ProvisionalEntity[] (bare array)   <- backend wraps in {count, items[]}
       .name        <- backend `mention`
       .label       <- ABSENT
       .aliases[]   <- ABSENT
       .span        <- ABSENT
       .doc_id      <- ABSENT
       .chunk_id    <- ABSENT
       .confidence  <- present (match)
       .candidates[] (ResolutionCandidate{existing_id,name,label,score,reason}) <- backend
           `candidates` is free-form JSON from resolver; keys differ.
       .status      <- present (match: 'provisional'|'merged'|'rejected' vs UI 'pending'|'merged'|'kept_new')
  3) EMPTY: the live queue is empty (count 0) — no provisional mentions were enqueued by the seed —
     so even with the path/shape fixed the panel would render empty until an ingest queues one.
decision_needed: |
  Either (graph) add GET /resolve returning ProvisionalEntity[] (unwrap items; map mention->name;
  surface label/span/doc_id/chunk_id from the enqueued provenance; normalize candidates to
  ResolutionCandidate), OR (ui) repoint getResolutionQueue() at /resolve/queue and adapt types.ts to
  {count,items[]} with the queue-row shape.
artifact: scratchpad probe api_results (GET /resolve -> 307; /resolve/queue -> {count:0,items:[]})
