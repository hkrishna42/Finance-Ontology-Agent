// Contract types mirrored from the Python backend (do NOT import Python — mirror only).
// Sources:
//   api/contracts/events.py      — SSE envelope + EventType
//   api/ontology/schema.py       — OntologyInfo, entity/relation vocab
//   events.py ANSWER/IMPACT docs — query + change-impact response shapes
// Shapes marked "UI contract (proposed)" are not yet emitted by a backend endpoint; they mirror
// what is documented in the build brief / seed graph and will auto-wire when the endpoint lands.

// ---------------------------------------------------------------------------------------------
// SSE envelope (api/contracts/events.py)
// ---------------------------------------------------------------------------------------------

export type EventType =
  | 'job.started'
  | 'job.completed'
  | 'error'
  | 'classified'
  | 'parsed'
  | 'chunked'
  | 'extracted'
  | 'resolved'
  | 'written'
  | 'impact'
  | 'token'
  | 'answer'

export interface SSEEvent<D = Record<string, unknown>> {
  event: EventType
  job_id: string
  seq: number
  data: D
}

// Per-event data payloads (from events.py inline docs + seed/demo sequence).
export interface JobStartedData { doc_id: string; source: string }
export interface ClassifiedData { doc_type: string; sensitivity: 'public' | 'internal' }
export interface ParsedData { pages: number }
export interface ChunkedData { chunks: number }
export interface ExtractedData { entities: number; relations: number; dropped: number }
export interface ResolvedData { merged: number; provisional: number }
export interface WrittenData { nodes: number; edges: number; rejected: number }
export interface JobCompletedData { ok: boolean }
export interface ErrorData { message: string }

// ---------------------------------------------------------------------------------------------
// Ontology (GET /ontology/info — api/ontology/schema.py::OntologyInfo)
// ---------------------------------------------------------------------------------------------

export interface OntologyInfo {
  entity_count: number
  relation_count: number
  extractable_entities: number
  extractable_relations: number
  vector_dim: number
  embed_model: string
}

export interface Health {
  status: string
  mode: string
}

// ---------------------------------------------------------------------------------------------
// Query / answer (POST /query — events.py ANSWER: {answer, citations, graph_paths, plan, cypher, withheld_count})
// ---------------------------------------------------------------------------------------------

export type QueryMode = 'graph' | 'side_by_side'

export interface Citation {
  id: string
  doc_id: string
  chunk_id: string
  page?: string | number
  span: string
  title?: string
  sensitivity?: 'public' | 'internal'
}

/** A single retrieved fragment from the vector-RAG baseline (left column of side-by-side). */
export interface VectorFragment {
  chunk_id: string
  doc_id: string
  score: number
  text: string
  title?: string
}

/** One hop of a highlighted graph path used to justify the answer. */
export interface GraphPathStep {
  from: string
  rel: string
  to: string
  qualifiers?: Record<string, string>
}

export interface GraphPath {
  label?: string
  steps: GraphPathStep[]
}

export interface QueryPlan {
  strategy: string
  steps: string[]
}

export interface QueryResponse {
  question: string
  mode: QueryMode
  answer: string
  citations: Citation[]
  graph_paths: GraphPath[]
  plan: QueryPlan
  cypher?: string
  withheld_count: number
  /** Present when mode === 'side_by_side': the vector-RAG baseline the graph answer is compared against. */
  vector_fragments?: VectorFragment[]
  vector_answer?: string
}

// ---------------------------------------------------------------------------------------------
// Graph explorer (GET /graph — UI contract (proposed), mirrors the seed graph)
// ---------------------------------------------------------------------------------------------

export type EntityLabel =
  | 'Company' | 'Person' | 'Product' | 'BusinessSegment' | 'Region' | 'RiskFactor'
  | 'MetricObservation' | 'Event' | 'DisclosureSection' | 'Fund' | 'RegulatoryForm'
  | 'GeneratedReport' | 'Document' | 'Chunk'

export interface GraphNode {
  id: string
  label: string       // display name
  type: EntityLabel   // ontology label
  props?: Record<string, string | number | boolean>
}

export interface GraphEdge {
  id: string
  source: string
  target: string
  type: string        // relation type, e.g. SUPPLIES_TO
  confidence: number  // 0..1
  props?: Record<string, string | number | boolean>
}

export interface GraphData {
  nodes: GraphNode[]
  edges: GraphEdge[]
  /** Optional named paths that panels can highlight (e.g. hero query chokepoint). */
  paths?: { label: string; node_ids: string[] }[]
}

// ---------------------------------------------------------------------------------------------
// Doc viewer (GET /documents — UI contract (proposed), mirrors seed Document/Chunk)
// ---------------------------------------------------------------------------------------------

export interface DocSpan {
  chunk_id: string
  quote: string         // verbatim source sentence (located in `text` at render time — mirrors the grounding gate)
  label?: string        // relation/entity this span grounds
  sensitivity?: 'public' | 'internal'
}

export interface DocRecord {
  doc_id: string
  title: string
  doc_type: string
  sensitivity: 'public' | 'internal'
  filing_date?: string
  url?: string
  text: string
  spans: DocSpan[]
}

// ---------------------------------------------------------------------------------------------
// Resolution queue (GET /resolve — UI contract (proposed))
// ---------------------------------------------------------------------------------------------

export interface ResolutionCandidate {
  existing_id: string
  name: string
  label: EntityLabel
  score: number         // similarity 0..1
  reason?: string
}

export interface ProvisionalEntity {
  id: string
  label: EntityLabel
  name: string
  aliases: string[]
  span: string
  doc_id: string
  chunk_id: string
  confidence: number
  candidates: ResolutionCandidate[]
  status?: 'pending' | 'merged' | 'kept_new'
}

// ---------------------------------------------------------------------------------------------
// Risk dashboard (GET /risk — UI contract (proposed))
// ---------------------------------------------------------------------------------------------

/** A read-only Cypher + the edges it returns, powering the "explain this number" drawer. */
export interface ExplainRef {
  cypher: string
  edges: GraphPathStep[]
  note?: string
}

export interface ConcentrationRow {
  fund: string
  issuer: string
  ticker?: string
  weight_pct: number
  value_usd: number
}

export interface HHIRow {
  fund: string
  hhi: number
  top_weight_pct: number
  interpretation: string
  explain?: ExplainRef
}

export interface HeatmapCell {
  company: string
  category: string       // RiskCategory
  severity: number       // 0..3 (0 = none)
  severity_language?: string
}

export interface HeatmapData {
  companies: string[]
  categories: string[]
  cells: HeatmapCell[]
}

/** A single point of failure: one supplier that many held issuers critically depend on. */
export interface SingleSourceFlag {
  supplier: string
  criticality: string
  dependents: string[]     // issuers that depend on the supplier
  exposed_funds: string[]  // funds holding those issuers
  aggregate_weight_pct: number
  explain?: ExplainRef
}

export interface RiskData {
  concentration: ConcentrationRow[]
  hhi: HHIRow[]
  heatmap: HeatmapData
  single_source: SingleSourceFlag[]
}

// ---------------------------------------------------------------------------------------------
// Impact feed (GET /impact — events.py IMPACT: {added, removed, changed, affected_funds, stale_sections})
// ---------------------------------------------------------------------------------------------

export interface FactTriple {
  subject: string
  predicate: string
  object: string
  detail?: string
}

export interface AffectedFund {
  fund: string
  reason: string
  hops: number
}

export interface StaleSection {
  form_ref: string
  item: string
  title: string
  reason: string
}

export interface ImpactBriefing {
  id: string
  trigger_doc_id: string
  trigger_title: string
  created_at: string
  rule: string            // impact_policy rule name
  summary: string
  added: FactTriple[]
  removed: FactTriple[]
  changed: FactTriple[]
  affected_funds: AffectedFund[]
  stale_sections: StaleSection[]
}

// ---------------------------------------------------------------------------------------------
// Report center (GET /reports — UI contract (proposed))
// ---------------------------------------------------------------------------------------------

export interface ProvenanceRow {
  claim: string
  doc_id: string
  chunk_id?: string
  span?: string
  cypher?: string
}

export interface ReportSection {
  heading: string
  body: string
  stale?: boolean
}

export interface ReportPack {
  report_id: string
  title: string
  period: string
  created_at: string
  sha256: string
  status: 'final' | 'draft' | 'out_of_date'
  sections: ReportSection[]
  provenance: ProvenanceRow[]
}

// ---------------------------------------------------------------------------------------------
// Eval panel (M8 — placeholder shapes)
// ---------------------------------------------------------------------------------------------

export interface EvalMetric {
  label: string
  value: number | null
  unit?: string
  target?: number
}

export interface EvalCard {
  id: string
  title: string
  description: string
  status: 'wired' | 'placeholder'
  metrics: EvalMetric[]
}

// ---------------------------------------------------------------------------------------------
// Firm registry (GET /firms, /firms/active — mirrors api/firms/store.py::FirmDict)
// ---------------------------------------------------------------------------------------------

export type FirmStatus = 'demo' | 'onboarding' | 'ready' | 'failed'
export type FirmSource = 'demo' | 'edgar'

/** One fund managed by a firm (Fund node MANAGED_BY the firm Company). */
export interface FirmFund {
  name: string
  series_id?: string | null
  cik?: string | null
  lei?: string | null
}

/** Mirror of the backend FirmDict returned by the /firms registry endpoints. */
export interface Firm {
  firm_id: string          // slugify(name)
  name: string
  lei: string | null
  cik: string | null
  status: FirmStatus       // demo | onboarding | ready | failed
  source: FirmSource       // demo | edgar
  is_active: boolean       // at most one firm is active
  fund_count: number
  funds: FirmFund[]
  identifiers: Record<string, unknown>
  created_at: string
  updated_at: string
}

// ---------------------------------------------------------------------------------------------
// Firm onboarding — discovery candidates + onboard pick (frozen Phase-2 contract §C)
//   POST /firms/search  {query}                         → FirmCandidate[]
//   POST /firms/onboard {name, cik?, lei?, series[]}     → SSE stream (SSEEvent envelopes)
// Mirrors api/onboarding/discovery.py::FirmCandidate (dict). Do NOT import Python — mirror only.
// ---------------------------------------------------------------------------------------------

/** One fund series under a candidate fund family (from EDGAR). May be `[]` for a bare company/LEI hit. */
export interface FirmCandidateSeries {
  series_id: string
  name: string
  class_tickers: string[]
}

/** A firm-discovery hit — an EDGAR fund family / company merged with its GLEIF LEI, ranked by score. */
export interface FirmCandidate {
  name: string
  cik: string | null
  lei: string | null
  source: 'edgar' | 'gleif'
  kind: 'fund_family' | 'company'
  series: FirmCandidateSeries[]
  score: number
}

/** Body POSTed to /firms/onboard: the picked candidate reduced to what the pipeline needs. */
export interface OnboardPick {
  name: string
  cik?: string | null
  lei?: string | null
  series: { series_id: string; name: string }[]
}
