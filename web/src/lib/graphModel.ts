// Pure model helpers for the Graph Explorer — degree/adjacency indexing, FIBO-aligned relation
// categories, and node→FIBO-class hints. No React, no cytoscape: kept side-effect-free so the
// component stays about interaction and rendering. Types are mirrored (never import Python).

import type { GraphData, GraphNode } from '../types'

// ---- FIBO-aligned relationship categories --------------------------------------------------
// The graph carries free-text relation types (HOLDS, MANAGED_BY, …). We fold them into a small
// set of FIBO-aligned categories so edges read as ontology relations, each with a stable colour
// (legible on both light and dark canvases) and a representative FIBO object-property hint.

export interface RelationCategory {
  key: string
  label: string
  color: string
  fibo: string
}

export const RELATION_CATEGORIES: RelationCategory[] = [
  { key: 'holding',    label: 'Ownership / holding',   color: '#2f9e44', fibo: 'fibo-fbc-pas-caa · holdsDuring' },
  { key: 'management', label: 'Management / advisory',  color: '#7048e8', fibo: 'fibo-fbc-fct-fse · isManagedBy' },
  { key: 'coverage',   label: 'Analyst coverage',       color: '#1c7ed6', fibo: 'fibo-fbc-fct · providesAnalysisOf' },
  { key: 'structure',  label: 'Corporate structure',    color: '#e8590c', fibo: 'fibo-be-corp · isControlledBy' },
  { key: 'supply',     label: 'Supply / dependency',    color: '#f59f00', fibo: 'fibo-fnd-rel · dependsOn' },
  { key: 'issuance',   label: 'Issuance',               color: '#ae3ec9', fibo: 'fibo-fbc-fi · isIssuedBy' },
  { key: 'related',    label: 'Related',                color: '#868e96', fibo: 'fibo-fnd-rel · relatedTo' },
]

const CATEGORY_BY_KEY: Record<string, RelationCategory> =
  Object.fromEntries(RELATION_CATEGORIES.map((c) => [c.key, c]))

// Relation type → category key. Covers the current structural vocab (HOLDS, MANAGED_BY) plus the
// semantic relations enrichment adds, so new firm subgraphs colour sensibly without code changes.
const RELATION_TO_CATEGORY: Record<string, string> = {
  HOLDS: 'holding', HELD_BY: 'holding', OWNS: 'holding', OWNED_BY: 'holding',
  HAS_POSITION_IN: 'holding', COMPRISES: 'holding', INVESTS_IN: 'holding',
  MANAGED_BY: 'management', MANAGES: 'management', ADVISES: 'management',
  ADVISED_BY: 'management', SUB_ADVISES: 'management', ADMINISTERS: 'management',
  COVERS: 'coverage', COVERED_BY: 'coverage',
  PARENT_OF: 'structure', SUBSIDIARY_OF: 'structure', CONTROLS: 'structure',
  CONTROLLED_BY: 'structure', PART_OF: 'structure', AFFILIATED_WITH: 'structure',
  SUPPLIES_TO: 'supply', SUPPLIED_BY: 'supply', DEPENDS_ON: 'supply', CUSTOMER_OF: 'supply',
  ISSUED_BY: 'issuance', ISSUES: 'issuance', GUARANTEES: 'issuance',
}

export function relationCategory(type: string | undefined): RelationCategory {
  const key = RELATION_TO_CATEGORY[(type ?? '').toUpperCase()] ?? 'related'
  return CATEGORY_BY_KEY[key] ?? CATEGORY_BY_KEY.related
}

export const relationColor = (type: string | undefined): string => relationCategory(type).color

// ---- FIBO class hint per ontology entity type ----------------------------------------------
// Node fill colour still comes from entityColor(type) (shared with the minimap + inspector); this
// only supplies the FIBO class name/CURIE shown in the legend so node types read as FIBO classes.

export const FIBO_CLASS_BY_TYPE: Record<string, { label: string; curie: string }> = {
  Fund: { label: 'Investment Fund', curie: 'fibo-fbc-fct-fse:Fund' },
  Company: { label: 'Legal Entity', curie: 'fibo-be-le-lp:LegalEntity' },
  Person: { label: 'Person', curie: 'fibo-fnd-aap-ppl:Person' },
  Product: { label: 'Product', curie: 'fibo-fbc-pas-fpas:FinancialProduct' },
  BusinessSegment: { label: 'Business Segment', curie: 'fibo-be-oac-cctl:OrganizationSubunit' },
  Region: { label: 'Geographic Region', curie: 'fibo-fnd-plc-loc:GeographicRegion' },
  RiskFactor: { label: 'Risk Factor', curie: 'fibo-fnd-utl-alx:RiskFactor' },
  RegulatoryForm: { label: 'Regulatory Filing', curie: 'fibo-fbc-fct-rga:RegulatoryFiling' },
  Event: { label: 'Business Event', curie: 'fibo-fnd-dt-oc:Occurrence' },
  MetricObservation: { label: 'Observation', curie: 'fibo-fnd-utl-alx:Value' },
}

export function fiboClassOf(type: string): { label: string; curie: string } {
  return FIBO_CLASS_BY_TYPE[type] ?? { label: type, curie: '' }
}

// ---- Graph index (degree / adjacency / structural roles) -----------------------------------

export interface GraphIndex {
  byId: Map<string, GraphNode>
  degree: Map<string, number>
  neighbors: Map<string, string[]>   // node id → distinct neighbour ids, sorted by degree desc
  hubs: Set<string>                  // high-degree entry points (funds / managers)
  managers: Set<string>              // the non-hub end of a management/advisory edge
  connectors: Set<string>            // non-hub nodes shared across ≥2 neighbours (cross-fund holdings)
  maxDegree: number
}

const HUB_MIN_DEGREE = 5
const HUB_MAX_COUNT = 12

export function buildIndex(data: GraphData | null | undefined): GraphIndex {
  const byId = new Map<string, GraphNode>()
  const degree = new Map<string, number>()
  const adj = new Map<string, Set<string>>()

  const nodes = data?.nodes ?? []
  const edges = data?.edges ?? []
  for (const n of nodes) { byId.set(n.id, n); degree.set(n.id, 0); adj.set(n.id, new Set()) }

  const bump = (a: string, b: string) => {
    if (!adj.has(a)) return
    const set = adj.get(a)!
    if (!set.has(b)) { set.add(b); degree.set(a, (degree.get(a) ?? 0) + 1) }
  }
  const managers = new Set<string>()
  for (const e of edges) {
    bump(e.source, e.target)
    bump(e.target, e.source)
    if (relationCategory(e.type).key === 'management') {
      // The managed party is the hub-like fund; its counterpart is the manager/adviser.
      const a = byId.get(e.source), b = byId.get(e.target)
      if (a && b) managers.add((degree.get(e.source) ?? 0) >= (degree.get(e.target) ?? 0) ? e.target : e.source)
    }
  }

  // neighbours sorted by degree desc then label — so the most connected (interesting) surface first.
  const neighbors = new Map<string, string[]>()
  for (const [id, set] of adj) {
    neighbors.set(id, [...set].sort((x, y) => {
      const d = (degree.get(y) ?? 0) - (degree.get(x) ?? 0)
      return d !== 0 ? d : (byId.get(x)?.label ?? '').localeCompare(byId.get(y)?.label ?? '')
    }))
  }

  const sorted = [...degree.entries()].sort((a, b) => b[1] - a[1])
  const maxDegree = sorted[0]?.[1] ?? 0
  let hubs = new Set(sorted.filter(([, d]) => d >= HUB_MIN_DEGREE).slice(0, HUB_MAX_COUNT).map(([id]) => id))
  if (hubs.size === 0) hubs = new Set(sorted.slice(0, Math.min(3, sorted.length)).map(([id]) => id))

  const connectors = new Set<string>()
  for (const [id, d] of degree) if (d >= 2 && !hubs.has(id) && !managers.has(id)) connectors.add(id)

  return { byId, degree, neighbors, hubs, managers, connectors, maxDegree }
}

// A node matches a search query on its label/type or any identifier prop (CIK, ticker, ISIN, LEI…).
export function nodeMatches(n: GraphNode, q: string): boolean {
  if (!q) return false
  const p = (n.props ?? {}) as Record<string, unknown>
  const hay = [n.label, n.type, p.cik, p.ticker, p.isin, p.lei, p.series_id, p.curie, p.fibo_class]
    .filter(Boolean).join(' ').toLowerCase()
  return hay.includes(q)
}

export interface RevealResult {
  ids: Set<string> | null            // null ⇒ everything (Full mode)
  capped: { label: string; shown: number; total: number }[]
}

/** Compute which nodes make up the current view: the always-on backbone (hubs + managers +
 *  cross-holdings), plus the neighbourhood of every expanded hub and every focused/searched node.
 *  Per-hub neighbour reveal is capped so one 100+-holding fund never re-creates the hairball. */
export function computeReveal(
  index: GraphIndex,
  opts: { full: boolean; expanded: Set<string>; focus: Set<string>; cap: number },
): RevealResult {
  if (opts.full) return { ids: null, capped: [] }
  const ids = new Set<string>()
  // backbone — the legible entry view
  for (const id of index.hubs) ids.add(id)
  for (const id of index.managers) ids.add(id)
  for (const id of index.connectors) ids.add(id)

  const capped: RevealResult['capped'] = []
  const revealNeighbours = (id: string, track: boolean) => {
    ids.add(id)
    const nbs = index.neighbors.get(id) ?? []
    const shown = nbs.slice(0, opts.cap)
    for (const nb of shown) ids.add(nb)
    if (track && nbs.length > shown.length) {
      capped.push({ label: index.byId.get(id)?.label ?? id, shown: shown.length, total: nbs.length })
    }
  }
  for (const id of opts.expanded) if (index.byId.has(id)) revealNeighbours(id, true)
  for (const id of opts.focus) if (index.byId.has(id)) revealNeighbours(id, index.hubs.has(id))
  return { ids, capped }
}
