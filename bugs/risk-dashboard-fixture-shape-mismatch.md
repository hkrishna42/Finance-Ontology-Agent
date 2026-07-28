# Risk Dashboard is fixture-locked: no GET /risk, and /risk/{fund}/report shape != types.ts RiskData
status: RESOLVED (re-verified 2026-07-28) — GET /risk returns 200 and matches types.ts RiskData field-for-field (concentration/hhi/heatmap.cells{severity:int}/single_source).
severity: high
owning_workstream: ui
also_affects: apps (risk_routes.py has no bare /risk collection endpoint)
repro: |
  curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/risk        # -> 404 (UI calls this)
  curl -s "http://localhost:8000/risk/Demo%20Growth%20Fund/report" | python3 -c "import sys,json;print(list(json.load(sys.stdin)))"
  # -> ['fund','supplier_concentration','shared_supplier_clusters','second_order_chokepoints',
  #     'risk_heatmap','single_source_flags','coverage','hhi','n_holdings']
expected: |
  web/src/api.ts getRisk() fetches GET /risk and expects types.ts RiskData:
    { concentration: ConcentrationRow[], hhi: HHIRow[],
      heatmap: {companies[], categories[], cells: HeatmapCell[]}, single_source: SingleSourceFlag[] }
actual: |
  1) PATH: there is no GET /risk. risk_routes.py exposes /risk/metrics, /risk/{fund}/report,
     /risk/{fund}/metric/{metric}, /risk/{fund}/narrative, /risk/compare. GET /risk -> 404, so
     api.ts always falls back to fixtures/risk.json (badge: FIXTURE). Confirmed by coordinator.
  2) SHAPE: even the closest endpoint, /risk/{fund}/report, does not match RiskData. Field-by-field
     (types.ts field  <-  backend field):
       RiskData.concentration[] (ConcentrationRow{fund,issuer,ticker,weight_pct,value_usd})
           <- NO EQUIVALENT. backend.supplier_concentration.table[] is supplier-DEPENDENCY scores
              {supplier,score,dependent_holdings,n_holdings,edges}, a different concept.
       RiskData.hhi (HHIRow[]{fund,hhi,top_weight_pct,interpretation,explain})
           <- backend.hhi is a bare float (e.g. 412.5); no per-row/interpretation wrapper.
       RiskData.heatmap {companies[],categories[],cells[HeatmapCell{company,category,severity:0..3,severity_language}]}
           <- backend.risk_heatmap.heatmap[] is category-major {category,mass,n_holdings,contributors[]};
              no companies[]/categories[] axes, no numeric severity (0..3) — severity is prose only.
       RiskData.single_source[] (SingleSourceFlag{supplier,criticality,dependents[],exposed_funds[],aggregate_weight_pct})
           <- backend.single_source_flags.flags[] is {holding,component,sole_supplier,citations};
              keyed by holding not supplier; no dependents[]/exposed_funds[]/aggregate_weight_pct.
decision_needed: |
  Either (apps) add GET /risk returning a RiskData-shaped projection (concentration = top holdings
  by weight; hhi = [{fund,hhi,top_weight_pct,interpretation}] for both funds; heatmap = pivot of
  risk_heatmap into companies x categories cells with numeric severity; single_source = pivot of
  shared_supplier_clusters/single_source_flags into supplier-major flags), OR (ui) repoint the panel
  at /risk/compare + /risk/{fund}/report + /risk/{fund}/metric/risk_heatmap and adapt types.ts.
artifact: scratchpad probe api_results/GET__risk_Demo_20Growth_20Fund_report.body, GET__risk.body (404)
