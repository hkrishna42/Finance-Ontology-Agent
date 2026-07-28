// Data layer. Every getter tries the real endpoint first and falls back to a committed fixture,
// so panels light up now (fixture) and auto-wire the moment the backend endpoint lands (live).
// It also reports which source answered, so the UI can badge fixture vs live.

import type {
  DocRecord, Firm, FirmCandidate, GraphData, Health, ImpactBriefing, OntologyInfo, OnboardPick,
  ProvisionalEntity, QueryMode, QueryResponse, ReportPack, RiskData, EvalCard, SSEEvent,
} from './types'

import ontologyFx from './fixtures/ontology.json'
import firmsFx from './fixtures/firms.json'
import graphFx from './fixtures/graph.json'
import queryFx from './fixtures/query.json'
import documentsFx from './fixtures/documents.json'
import resolveFx from './fixtures/resolve.json'
import riskFx from './fixtures/risk.json'
import impactFx from './fixtures/impact.json'
import reportsFx from './fixtures/reports.json'
import evalFx from './fixtures/eval.json'

export type Source = 'live' | 'fixture'
export interface Loaded<T> { data: T; source: Source }

/** Fetch JSON, returning null on any network/HTTP failure so callers can fall back. */
async function tryFetch<T>(path: string, init?: RequestInit): Promise<T | null> {
  try {
    const r = await fetch(path, init)
    if (!r.ok) return null
    return (await r.json()) as T
  } catch {
    return null
  }
}

/** The one firm permitted to fall back to the committed demo fixtures when the API is unreachable.
 *  Every other (real) firm must show a live/empty state instead, so a backend hiccup never resurfaces
 *  the demo's NVIDIA/TSMC data under a firm it doesn't belong to. */
export const DEMO_FIRM_NAME = 'Demo Investment Management'
const isDemoFirm = (firm?: string | null): boolean => (firm ?? '') === DEMO_FIRM_NAME

/** Resolve a getter result. Live always wins. Offline, `fixture` is served only for firm-agnostic
 *  getters (no `firmGuard`) or the demo firm; a firm-scoped getter for a real firm gets the typed
 *  `empty` value instead — tagged 'live' because it is emphatically NOT the committed demo fixture,
 *  and SourceBadge renders anything non-'live' as the word "fixture" (which would mislead here). */
function loaded<T>(live: T | null, fixture: T, firmGuard?: { firm?: string | null; empty: T }): Loaded<T> {
  if (live !== null) return { data: live, source: 'live' }
  if (!firmGuard) return { data: fixture, source: 'fixture' }
  if (isDemoFirm(firmGuard.firm)) return { data: fixture, source: 'fixture' }
  return { data: firmGuard.empty, source: 'live' }
}

// -- Health / ontology ------------------------------------------------------------------------

export async function getHealth(): Promise<Loaded<Health> | null> {
  const live = await tryFetch<Health>('/health')
  return live ? { data: live, source: 'live' } : null
}

export async function getOntologyInfo(): Promise<Loaded<OntologyInfo>> {
  return loaded(await tryFetch<OntologyInfo>('/ontology/info'), ontologyFx as OntologyInfo)
}

// -- Graph ------------------------------------------------------------------------------------

const EMPTY_GRAPH: GraphData = { nodes: [], edges: [], paths: [] }

export async function getGraph(firm?: string): Promise<Loaded<GraphData>> {
  const qs = firm ? `?firm=${encodeURIComponent(firm)}` : ''
  const live = await tryFetch<GraphData>(`/graph${qs}`)
  return loaded(live, graphFx as unknown as GraphData, { firm, empty: EMPTY_GRAPH })
}

// -- Query ------------------------------------------------------------------------------------

interface QueryFixture {
  default: string
  keywords: Record<string, string[]>
  queries: Record<string, QueryResponse>
}
const QFX = queryFx as unknown as QueryFixture

/** Pick the closest canned answer for an offline question by keyword overlap. */
function pickFixtureQuery(question: string, mode: QueryMode): QueryResponse {
  const q = question.toLowerCase()
  let best = QFX.default
  let bestScore = 0
  for (const [slug, words] of Object.entries(QFX.keywords)) {
    const score = words.reduce((n, w) => (q.includes(w) ? n + 1 : n), 0)
    if (score > bestScore) { bestScore = score; best = slug }
  }
  const base = QFX.queries[best] ?? QFX.queries[QFX.default]
  // Honour the requested mode so the side-by-side / graph toggle always reflects the UI.
  return { ...base, question: question || base.question, mode }
}

/** A neutral, demo-free answer for a real firm when the API is unreachable — so an offline hiccup
 *  never resurfaces the demo's NVIDIA fixture answer under a firm it doesn't belong to. When the API
 *  IS reachable it returns the backend's own structural answer (empty citations) for filing-less firms. */
function offlineQueryResponse(question: string, mode: QueryMode, firm?: string): QueryResponse {
  return {
    question,
    mode,
    answer: `The analyst API is offline, so no firm-scoped answer is available for ${firm ?? 'this firm'} right now. Reconnect to ask a grounded, cited question.`,
    citations: [],
    graph_paths: [],
    plan: { strategy: 'offline — no fixture for this firm', steps: [] },
    withheld_count: 0,
  }
}

export async function runQuery(
  question: string,
  mode: QueryMode,
  entitlementWall: boolean,
  firm?: string,
): Promise<Loaded<QueryResponse>> {
  const live = await tryFetch<QueryResponse>('/query', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    // `firm` is dropped from the JSON when undefined, so the server falls back to its active firm.
    body: JSON.stringify({ question, mode, entitlement_wall: entitlementWall, firm }),
  })
  if (live) return { data: live, source: 'live' }

  // Guard the fixture fallback: only the demo firm may fall back to the committed NVIDIA fixtures.
  // A real firm gets a neutral offline placeholder (tagged 'live') — never the demo answer.
  if (!isDemoFirm(firm)) return { data: offlineQueryResponse(question, mode, firm), source: 'live' }

  // Fixture fallback: when the wall is OFF, the withheld internal source is folded back in.
  const fx = pickFixtureQuery(question, mode)
  if (!entitlementWall && fx.withheld_count > 0) {
    return {
      source: 'fixture',
      data: {
        ...fx,
        withheld_count: 0,
        answer: fx.answer.replace(
          /Note: [^.]*withheld under the internal-information wall[^.]*\./,
          'With the internal-information wall OFF, the internal analyst note is included: one hyperscale customer is negotiating multi-year committed capacity that would push NVDA concentration above the level disclosed in the 10-K.',
        ),
      },
    }
  }
  return { data: fx, source: 'fixture' }
}

// -- Documents --------------------------------------------------------------------------------

export async function getDocuments(firm?: string): Promise<Loaded<DocRecord[]>> {
  const qs = firm ? `?firm=${encodeURIComponent(firm)}` : ''
  const live = await tryFetch<DocRecord[]>(`/documents${qs}`)
  return loaded(live, documentsFx as unknown as DocRecord[], { firm, empty: [] as DocRecord[] })
}

// -- Resolution queue -------------------------------------------------------------------------

export async function getResolutionQueue(): Promise<Loaded<ProvisionalEntity[]>> {
  return loaded(await tryFetch<ProvisionalEntity[]>('/resolve'), resolveFx as unknown as ProvisionalEntity[])
}

// -- Risk -------------------------------------------------------------------------------------

export async function getRisk(): Promise<Loaded<RiskData>> {
  return loaded(await tryFetch<RiskData>('/risk'), riskFx as unknown as RiskData)
}

// -- Impact -----------------------------------------------------------------------------------

export async function getImpact(): Promise<Loaded<ImpactBriefing[]>> {
  return loaded(await tryFetch<ImpactBriefing[]>('/impact'), impactFx as unknown as ImpactBriefing[])
}

// -- Reports ----------------------------------------------------------------------------------

export async function getReports(): Promise<Loaded<ReportPack[]>> {
  return loaded(await tryFetch<ReportPack[]>('/reports'), reportsFx as unknown as ReportPack[])
}

// -- Eval -------------------------------------------------------------------------------------

export async function getEval(): Promise<Loaded<EvalCard[]>> {
  return loaded(await tryFetch<EvalCard[]>('/eval'), evalFx as unknown as EvalCard[])
}

// -- Firms (registry + active-firm selection) -------------------------------------------------
// GET /firms → FirmDict[] · GET /firms/active → FirmDict|null · POST /firms/{id}/select → FirmDict
// DELETE /firms/{id} → {deleted:true}. Falls back to the committed demo-firm fixture offline so the
// header selector still renders (read-only) when the registry endpoint is not yet live.

const FIRMS_FX = firmsFx as unknown as Firm[]
const ACTIVE_FIRM_FX: Firm | null = FIRMS_FX.find((f) => f.is_active) ?? FIRMS_FX[0] ?? null

export async function getFirms(): Promise<Loaded<Firm[]>> {
  return loaded(await tryFetch<Firm[]>('/firms'), FIRMS_FX)
}

export async function getActiveFirm(): Promise<Loaded<Firm | null>> {
  return loaded(await tryFetch<Firm | null>('/firms/active'), ACTIVE_FIRM_FX)
}

/** Make a firm active. Returns the updated FirmDict, or null when the endpoint is unreachable. */
export async function selectFirm(firmId: string): Promise<Firm | null> {
  return tryFetch<Firm>(`/firms/${encodeURIComponent(firmId)}/select`, { method: 'POST' })
}

/** Delete a firm (+ its funds in the graph). Returns true on success. */
export async function deleteFirm(firmId: string): Promise<boolean> {
  const res = await tryFetch<{ deleted: boolean }>(`/firms/${encodeURIComponent(firmId)}`, { method: 'DELETE' })
  return res?.deleted === true
}

// -- Firm onboarding (discovery + streaming onboard/enrich) -----------------------------------
// POST /firms/search {query} → FirmCandidate[] · POST /firms/onboard {name,cik?,lei?,series,enrich?}
// → SSE · POST /firms/{id}/enrich → SSE. All live under the /firms proxy allow-list (Phase 1).
// Unlike the getters above these do NOT fall back to a fixture — the AddFirmModal surfaces an inline
// "search unavailable" note when the discovery service is down, and onboarding requires a live backend.

/** Search EDGAR + GLEIF for firms to onboard. Throws on network/HTTP failure so the modal can tell
 *  "search unavailable" apart from an empty (but successful) result set. Returns [] when nothing matched. */
export async function searchFirms(query: string): Promise<FirmCandidate[]> {
  const r = await fetch('/firms/search', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query }),
  })
  if (!r.ok) throw new Error(`firm search failed (${r.status})`)
  return (await r.json()) as FirmCandidate[]
}

/** Onboard a picked firm, streaming the deterministic pipeline as SSE. Mirrors the IngestPanel's SSE
 *  consumption (`JSON.parse(ev.data)` per named event), but over POST via fetch + ReadableStream —
 *  EventSource is GET-only, and /firms/onboard needs a JSON body. Each frozen SSEEvent envelope
 *  ({event, job_id, seq, data}) is handed to `onEvent` as it lands. When `picked.enrich` is set the
 *  stream chains an extra semantic-enrichment stage (its events re-tagged with `holding`/`ticker`)
 *  before the terminal `job.completed`. Resolves when the stream ends; rejects only if the request
 *  itself fails to open (the pipeline reports failure via an `error` event). */
export async function onboardFirm(picked: OnboardPick, onEvent: (ev: SSEEvent) => void): Promise<void> {
  const r = await fetch('/firms/onboard', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(picked),
  })
  if (!r.ok || !r.body) throw new Error(`onboard failed (${r.status})`)
  await streamSSE(r.body, onEvent)
}

/** (Re-)enrich an already-onboarded firm's top holdings, streaming the ingest pipeline as SSE.
 *  POSTs to /firms/{firmId}/enrich (no body — the server defaults `top`). Mirrors `onboardFirm`:
 *  fetch + ReadableStream, each SSEEvent envelope handed to `onEvent` as it lands. The stream is
 *  `job.started` → per-holding forwarded ingest events (re-tagged `holding`/`ticker`) → a terminal
 *  `job.completed{enriched, risk_factors_added, …}`. Per-holding failures arrive as NON-fatal `error`
 *  events (`data.fatal === false`) and the run still completes. Resolves when the stream ends; rejects
 *  only if the request itself fails to open. */
export async function enrichFirm(firmId: string, onEvent: (ev: SSEEvent) => void): Promise<void> {
  const r = await fetch(`/firms/${encodeURIComponent(firmId)}/enrich`, { method: 'POST' })
  if (!r.ok || !r.body) throw new Error(`enrich failed (${r.status})`)
  await streamSSE(r.body, onEvent)
}

/** Drain a fetch `ReadableStream` of SSE frames, handing each parsed SSEEvent envelope to `onEvent`.
 *  Shared by `onboardFirm` + `enrichFirm` (both stream the same envelopes over POST — EventSource is
 *  GET-only). Normalises CRLF/CR → LF so `\n\n` frame + line splitting is uniform across servers. */
async function streamSSE(body: ReadableStream<Uint8Array>, onEvent: (ev: SSEEvent) => void): Promise<void> {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  const flush = (frame: string) => {
    const ev = parseSSEFrame(frame)
    if (ev) onEvent(ev)
  }

  let streaming = true
  while (streaming) {
    const { done, value } = await reader.read()
    if (done) { streaming = false; break }
    buf += decoder.decode(value, { stream: true }).replace(/\r\n?/g, '\n')
    let sep = buf.indexOf('\n\n')
    while (sep !== -1) {
      flush(buf.slice(0, sep))
      buf = buf.slice(sep + 2)
      sep = buf.indexOf('\n\n')
    }
  }
  buf += decoder.decode().replace(/\r\n?/g, '\n')
  if (buf.trim()) flush(buf)
}

/** Parse one SSE frame into its SSEEvent envelope. The `data:` payload already carries the full
 *  envelope (so we ignore the redundant `event:` line and comment/ping `:` lines), mirroring how
 *  IngestPanel reads `JSON.parse(messageEvent.data)`. Returns null for a frame with no usable data. */
function parseSSEFrame(frame: string): SSEEvent | null {
  const data: string[] = []
  for (const line of frame.split('\n')) {
    if (!line || line.startsWith(':')) continue
    if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''))
  }
  if (!data.length) return null
  try {
    return JSON.parse(data.join('\n')) as SSEEvent
  } catch {
    return null
  }
}

// -- Ingest SSE -------------------------------------------------------------------------------

/** The canned pipeline sequence (mirrors api/main.py::_DEMO_SEQUENCE, plus resolved/impact) used
 *  by the ingest simulator when no live EventSource is available. */
export const DEMO_SEQUENCE: { event: SSEEvent['event']; data: Record<string, unknown> }[] = [
  { event: 'job.started', data: { doc_id: 'nvda_8k_live', source: 'demo' } },
  { event: 'classified', data: { doc_type: '8-K', sensitivity: 'public' } },
  { event: 'parsed', data: { pages: 6 } },
  { event: 'chunked', data: { chunks: 18 } },
  { event: 'extracted', data: { entities: 12, relations: 7, dropped: 2 } },
  { event: 'resolved', data: { merged: 9, provisional: 3 } },
  { event: 'written', data: { nodes: 10, edges: 6, rejected: 1 } },
  { event: 'impact', data: { added: 2, changed: 1, affected_funds: 2, stale_sections: 2 } },
  { event: 'job.completed', data: { ok: true } },
]
