# Report Center panel is fixture-locked: no GET /reports, /reports/pack/{fund} shape != types.ts ReportPack
status: RESOLVED (re-verified 2026-07-28) — GET /reports returns 200 ReportPack[] matching types.ts field-for-field (created_at, status, sections[], provenance[]).
severity: high
owning_workstream: ui
also_affects: apps (reg_routes.py — no bare /reports collection endpoint)
ui_panel: web/src/components/ReportCenter.tsx  (the "Report Center" / Reg Reporting Assistant panel)
repro: |
  curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/reports         # -> 404 (UI calls this)
  curl -s "http://localhost:8000/reports/pack/Demo%20Growth%20Fund" | python3 -c "import sys,json;print(list(json.load(sys.stdin)))"
  # -> ['report_id','title','period','sha256','snapshot','html_bytes']
expected: |
  web/src/api.ts getReports() fetches GET /reports and expects types.ts ReportPack[]:
    ReportPack{ report_id, title, period, created_at, sha256,
      status: 'final'|'draft'|'out_of_date', sections: ReportSection[]{heading,body,stale?},
      provenance: ProvenanceRow[]{claim,doc_id,chunk_id?,span?,cypher?} }
actual: |
  1) PATH: no GET /reports (only /reports/13f/{fund}[/informationtable.xml|/reviewer.csv],
     /reports/coverage/{fund}, /reports/pack/{fund}[/html]). GET /reports -> 404 ->
     fixtures/reports.json (FIXTURE).
  2) SHAPE: /reports/pack/{fund} != ReportPack (types.ts field <- backend):
       report_id, title, period, sha256   <- present (match)
       ReportPack.created_at   <- ABSENT
       ReportPack.status       <- ABSENT
       ReportPack.sections[]   <- ABSENT (backend returns `html_bytes`: int + a `snapshot` object
                                  instead of rendered {heading,body,stale} sections; the HTML body
                                  is only at /reports/pack/{fund}/html as text/html)
       ReportPack.provenance[] <- ABSENT (provenance lives inside `snapshot`, not as ProvenanceRow[])
       backend EXTRA: `snapshot`, `html_bytes` — not in types.ts.
     Response is a single object; UI expects an array (ReportPack[]).
decision_needed: |
  Either (apps) add GET /reports returning ReportPack[] with created_at/status/sections[]/provenance[]
  (derive sections from the snapshot, provenance from DERIVED_FROM / snapshot citations), OR (ui)
  repoint getReports() at /reports/pack/{fund} + /reports/pack/{fund}/html and adapt types.ts.
artifact: scratchpad probe api_results (GET reports/pack body; GET /reports -> 404)
