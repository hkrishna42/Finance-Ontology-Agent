# Impact Feed is fixture-locked: no GET /impact, /impact/run shape != types.ts ImpactBriefing[], default narrate=true 500s
status: RESOLVED (re-verified 2026-07-28) — GET /impact returns 200 ImpactBriefing[] matching types.ts field-for-field (added/removed/changed, affected_funds{fund,reason,hops}, stale_sections).
severity: high
owning_workstream: ui
also_affects: apps (impact_routes.py — no bare /impact; /impact/run nests fields; narrate default)
repro: |
  curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/impact              # -> 404 (UI calls this)
  curl -s -o /dev/null -w '%{http_code}\n' "http://localhost:8000/impact/run"         # -> 500 (narrate=true default; credits)
  curl -s "http://localhost:8000/impact/run?narrate=false" | python3 -c "import sys,json;d=json.load(sys.stdin);print(list(d));print('diff:',list(d['diff']));print('impact:',list(d['impact']))"
  # top: ['narrative','llm_note','role','diff','impact']
  # diff:  ['added','removed','changed','counts']
  # impact:['affected_funds','stale_sections','out_of_date_reports','summary']
expected: |
  web/src/api.ts getImpact() fetches GET /impact and expects types.ts ImpactBriefing[]:
    ImpactBriefing{ id, trigger_doc_id, trigger_title, created_at, rule, summary,
      added: FactTriple[], removed: FactTriple[], changed: FactTriple[],
      affected_funds: AffectedFund[]{fund,reason,hops}, stale_sections: StaleSection[]{form_ref,item,title,reason} }
actual: |
  1) PATH: no GET /impact (only /impact/policy, /impact/run, /impact/stream). GET /impact -> 404 ->
     fixtures/impact.json (FIXTURE).
  2) DEFAULT 500: /impact/run defaults narrate=true -> LLM -> 500 on zero credits (see
     bugs/llm-endpoints-500-no-graceful-degradation.md). narrate=false returns 200.
  3) SHAPE: /impact/run?narrate=false != ImpactBriefing (types.ts field <- backend location):
       ImpactBriefing.added/removed/changed  <- nested under `diff.added/removed/changed` (not top-level)
       ImpactBriefing.affected_funds/stale_sections <- nested under `impact.affected_funds/stale_sections`
       ImpactBriefing.summary                <- present as `impact.summary` (also top-level `narrative`)
       ImpactBriefing.id/trigger_doc_id/trigger_title/created_at/rule <- ABSENT
       AffectedFund{fund,reason,hops} <- backend impact.affected_funds[] is
           {fund,series_id,via[],rules[],holdings[],citations[]} — no `reason`/`hops` fields.
       Response is a single object; UI expects an array (ImpactBriefing[]).
decision_needed: |
  Either (apps) add GET /impact returning ImpactBriefing[] (flatten diff+impact to top-level, add
  id/trigger/created_at/rule, map affected_funds -> {fund,reason,hops}), and default narrate=false
  (or degrade), OR (ui) repoint getImpact() at /impact/run?narrate=false and adapt types.ts to the
  {narrative,diff,impact} shape.
artifact: scratchpad probe api_results/GET__impact_run_narrate_false_narrate_false.body, GET__impact.body (404)
