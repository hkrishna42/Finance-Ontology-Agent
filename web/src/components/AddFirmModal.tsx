import { useEffect, useRef, useState } from 'react'
import { onboardFirm, searchFirms } from '../api'
import type { FirmCandidate, OnboardPick, SSEEvent } from '../types'
import { Icon } from '../lib/icons'
import type { IconName } from '../lib/icons'

// "Add firm" onboarding modal. Opened from the FirmSelector's "+ Add firm…" entry.
//   1. text query → POST /firms/search {query} → candidate list (name, CIK, #series, LEI, source)
//   2. pick a candidate → POST /firms/onboard → stream the deterministic pipeline (SSE) as a
//      live progress list (job.started → parsed/written per series → resolved → job.completed)
//   3. on job.completed → onDone(name) so App refreshes /firms + selects the newly-active firm.
// The onboard endpoint is POST-with-body, so we consume it via api.onboardFirm (fetch +
// ReadableStream), which mirrors the IngestPanel SSE approach but over POST. Offline/degraded
// backends surface as an inline "search unavailable" note (search) or an error state (onboard).

type Phase = 'search' | 'onboard'
type Metric = [string, string | number]

interface AddFirmModalProps {
  open: boolean
  onClose: () => void
  /** Called once when onboarding completes (job.completed). App refreshes /firms + selects the firm. */
  onDone: (firmName: string) => void
}

export function AddFirmModal({ open, onClose, onDone }: AddFirmModalProps) {
  const [phase, setPhase] = useState<Phase>('search')
  // search state
  const [query, setQuery] = useState('')
  const [searching, setSearching] = useState(false)
  const [searchError, setSearchError] = useState(false)
  const [results, setResults] = useState<FirmCandidate[] | null>(null) // null = not searched yet
  const [enrich, setEnrich] = useState(true) // opt-in semantic enrichment (default ON), passed to onboard
  // onboard state
  const [picked, setPicked] = useState<FirmCandidate | null>(null)
  const [events, setEvents] = useState<SSEEvent[]>([])
  const [onboardError, setOnboardError] = useState<string | null>(null)
  const [done, setDone] = useState(false)

  const doneRef = useRef(false)          // guard: onDone fires exactly once
  const closeTimer = useRef<number | null>(null)
  const streamId = useRef(0)             // invalidates events from a superseded/closed stream

  const clearTimer = () => {
    if (closeTimer.current != null) { clearTimeout(closeTimer.current); closeTimer.current = null }
  }

  // Reset everything each time the modal opens; invalidate any in-flight stream on close.
  useEffect(() => {
    streamId.current += 1
    clearTimer()
    if (open) {
      setPhase('search')
      setQuery(''); setSearching(false); setSearchError(false); setResults(null); setEnrich(true)
      setPicked(null); setEvents([]); setOnboardError(null); setDone(false)
      doneRef.current = false
    }
  }, [open])

  // Cleanup the auto-close timer if the modal unmounts.
  useEffect(() => clearTimer, [])

  // Escape closes the modal.
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  const runSearch = async () => {
    const q = query.trim()
    if (!q || searching) return
    setSearching(true); setSearchError(false); setResults(null)
    try {
      setResults(await searchFirms(q))
    } catch {
      setSearchError(true)
    } finally {
      setSearching(false)
    }
  }

  const pick = async (c: FirmCandidate) => {
    streamId.current += 1
    const myStream = streamId.current
    setPicked(c); setPhase('onboard'); setEvents([]); setOnboardError(null); setDone(false)

    const payload: OnboardPick = {
      name: c.name,
      cik: c.cik ?? undefined,
      lei: c.lei ?? undefined,
      series: c.series.map((s) => ({ series_id: s.series_id, name: s.name })),
      enrich,
    }
    try {
      await onboardFirm(payload, (ev) => {
        if (streamId.current !== myStream) return // stream superseded (modal closed / re-picked)
        setEvents((prev) => [...prev, ev])
        const d = ev.data as Record<string, unknown>
        // Only a FATAL error aborts. The enrichment stage reports per-holding failures as non-fatal
        // `error` events (data.fatal === false) and keeps going — those render inline, not as an abort.
        if (ev.event === 'error' && d.fatal !== false) {
          setOnboardError(String(d.message ?? 'Onboarding failed'))
        } else if (ev.event === 'job.completed' && !doneRef.current) {
          // Fire onDone once, at the first completion (the firm is onboarded + active by now). The
          // "done" visual + auto-dismiss are deferred to stream close below, so the modal keeps
          // showing the enrichment stage that streams AFTER this event rather than closing early.
          doneRef.current = true
          onDone(c.name) // App refreshes /firms + selects the newly-active firm
        }
      })
      // Stream fully closed. If we saw a terminal completion, settle into the done state and
      // auto-dismiss; otherwise flag the truncated stream so the user isn't stuck on a spinner.
      if (streamId.current !== myStream) return
      if (doneRef.current) {
        setDone(true)
        clearTimer()
        closeTimer.current = window.setTimeout(onClose, 1600)
      } else {
        setOnboardError((prev) => prev ?? 'Onboarding stream ended before completion.')
      }
    } catch (e) {
      if (streamId.current === myStream) {
        setOnboardError(e instanceof Error ? e.message : 'Onboarding request failed')
      }
    }
  }

  const backToSearch = () => {
    streamId.current += 1
    clearTimer()
    setPhase('search'); setPicked(null); setEvents([]); setOnboardError(null); setDone(false)
  }

  return (
    <div
      role="presentation"
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}
      style={{
        position: 'fixed', inset: 0, zIndex: 60, background: 'rgba(8, 11, 20, 0.42)',
        display: 'flex', alignItems: 'flex-start', justifyContent: 'center',
        padding: '9vh 16px 16px', animation: 'fade 0.15s ease',
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Add a firm"
        onMouseDown={(e) => e.stopPropagation()}
        style={{
          width: 580, maxWidth: '96vw', maxHeight: '82vh', display: 'flex', flexDirection: 'column',
          background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--r-lg)',
          boxShadow: 'var(--shadow-lg)', overflow: 'hidden',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12, padding: '16px 18px', borderBottom: '1px solid var(--border)' }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <h2 style={{ fontSize: 16 }}>Add a firm</h2>
            <p className="panel-sub" style={{ marginTop: 2 }}>
              Search SEC EDGAR &amp; GLEIF, then onboard a fund family into the graph.
            </p>
          </div>
          <button className="icon-btn" onClick={onClose} aria-label="Close"><Icon name="close" /></button>
        </div>

        <div style={{ padding: '16px 18px', overflowY: 'auto' }}>
          {phase === 'search'
            ? <SearchStep
                query={query} setQuery={setQuery} searching={searching} searchError={searchError}
                results={results} onSearch={runSearch} onPick={pick} enrich={enrich} setEnrich={setEnrich}
              />
            : <OnboardStepView
                firm={picked} events={events} error={onboardError} done={done}
                onBack={backToSearch} onClose={onClose}
              />}
        </div>
      </div>
    </div>
  )
}

// -- Search step -------------------------------------------------------------------------------

function SearchStep({ query, setQuery, searching, searchError, results, onSearch, onPick, enrich, setEnrich }: {
  query: string
  setQuery: (v: string) => void
  searching: boolean
  searchError: boolean
  results: FirmCandidate[] | null
  onSearch: () => void
  onPick: (c: FirmCandidate) => void
  enrich: boolean
  setEnrich: (v: boolean) => void
}) {
  return (
    <div>
      <form onSubmit={(e) => { e.preventDefault(); onSearch() }} style={{ display: 'flex', gap: 10 }}>
        <input
          className="chat-input"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Firm or fund family — e.g. Vanguard, Fidelity Contrafund"
          aria-label="Firm search query"
          autoFocus
        />
        <button type="submit" className="btn btn-primary" disabled={!query.trim() || searching}>
          {searching ? <><span className="spinner" />Searching…</> : <><Icon name="search" size={14} />Search</>}
        </button>
      </form>

      {/* Onboarding option — carried into the /firms/onboard body when a candidate is picked. */}
      <label
        style={{
          display: 'flex', alignItems: 'flex-start', gap: 9, marginTop: 12, padding: '10px 12px',
          border: '1px solid var(--border)', borderRadius: 'var(--r-md)', cursor: 'pointer', userSelect: 'none',
        }}
      >
        <input
          type="checkbox"
          checked={enrich}
          onChange={(e) => setEnrich(e.target.checked)}
          style={{ marginTop: 1, width: 15, height: 15, flex: 'none', accentColor: 'var(--accent)', cursor: 'pointer' }}
        />
        <span style={{ fontSize: 12.5, lineHeight: 1.5 }}>
          <span style={{ fontWeight: 650, display: 'inline-flex', alignItems: 'center', gap: 5 }}>
            <span style={{ color: 'var(--accent)', display: 'inline-flex' }}><Icon name="impact" size={13} /></span>
            Enrich filings
          </span>{' '}
          <span className="faint">
            — after building the holdings graph, pull each top holding’s 10-K risk factors and extract the
            semantic layer (RiskFactor / EXPOSED_TO edges). Adds a little time; you can also run it later per firm.
          </span>
        </span>
      </label>

      <div style={{ marginTop: 16 }}>
        {searching && (
          <div className="empty" style={{ padding: 28 }}><span className="spinner" />Searching EDGAR + GLEIF…</div>
        )}

        {!searching && searchError && (
          <div className="card card-pad" style={{ borderColor: 'var(--warn)', display: 'flex', gap: 10, alignItems: 'flex-start' }}>
            <span style={{ color: 'var(--warn)', display: 'inline-flex', marginTop: 1 }}><Icon name="alert" size={17} /></span>
            <div style={{ fontSize: 13, color: 'var(--text-muted)', lineHeight: 1.5 }}>
              <strong style={{ color: 'var(--text)' }}>Search unavailable.</strong>{' '}
              The firm-discovery service (EDGAR + GLEIF) couldn’t be reached. Onboarding needs a live
              backend — try again once the API is online.
            </div>
          </div>
        )}

        {!searching && !searchError && results && results.length === 0 && (
          <div className="empty">
            No firms matched “{query.trim()}”. Try a fund-family name or ticker.
          </div>
        )}

        {!searching && !searchError && results && results.length > 0 && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            <div className="faint" style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 700 }}>
              {results.length} {results.length === 1 ? 'match' : 'matches'}
            </div>
            {results.map((c, i) => (
              <CandidateRow key={`${c.source}:${c.cik ?? c.lei ?? c.name}:${i}`} c={c} onPick={() => onPick(c)} />
            ))}
          </div>
        )}

        {!searching && !searchError && results === null && (
          <div className="faint" style={{ fontSize: 12.5, lineHeight: 1.55 }}>
            Search SEC EDGAR (fund families + companies) and GLEIF (legal-entity identifiers) by name.
            Pick a result to stream it through the deterministic onboarding pipeline.
          </div>
        )}
      </div>
    </div>
  )
}

function CandidateRow({ c, onPick }: { c: FirmCandidate; onPick: () => void }) {
  return (
    <button
      type="button"
      className="btn"
      onClick={onPick}
      style={{
        width: '100%', textAlign: 'left', flexDirection: 'column', alignItems: 'stretch',
        gap: 5, padding: '11px 13px',
      }}
    >
      <span className="row" style={{ justifyContent: 'space-between', gap: 8 }}>
        <span className="nowrap" style={{ flex: 1, minWidth: 0, fontWeight: 650, fontSize: 13.5, overflow: 'hidden', textOverflow: 'ellipsis' }}>
          {c.name}
        </span>
        <SourceTag source={c.source} />
      </span>
      <span className="row" style={{ gap: 12, flexWrap: 'wrap', fontSize: 11.5, color: 'var(--text-muted)' }}>
        {c.cik && <span>CIK <span className="mono" style={{ color: 'var(--text)' }}>{c.cik}</span></span>}
        {c.lei && <span>LEI <span className="mono" style={{ color: 'var(--text)' }}>{c.lei}</span></span>}
        <span><b style={{ color: 'var(--text)', fontWeight: 650 }}>{c.series.length}</b> {c.series.length === 1 ? 'series' : 'series'}</span>
        <span className="faint">{c.kind === 'fund_family' ? 'fund family' : 'company'}</span>
      </span>
    </button>
  )
}

function SourceTag({ source }: { source: 'edgar' | 'gleif' }) {
  const edgar = source === 'edgar'
  return (
    <span
      className="tag"
      style={{
        flex: 'none', textTransform: 'uppercase', letterSpacing: '0.04em', fontSize: 9.5, fontWeight: 700,
        border: '1px solid transparent',
        color: edgar ? 'var(--accent)' : 'var(--good)',
        background: edgar ? 'var(--accent-weak)' : 'var(--good-bg)',
      }}
    >
      {source}
    </span>
  )
}

// -- Onboard step ------------------------------------------------------------------------------

/** True when an SSE event belongs to the (optional) enrichment stage rather than the structural
 *  onboarding stage. The onboard pipeline stamps every enrichment-stage event with `stage:"enriching"`
 *  (authoritative); we also treat the per-holding/per-fund re-tag (`holding`/`fund`), the ingest-only
 *  steps (classified/chunked/extracted), and the standalone enrich announcement (a job.started with a
 *  holdings list / top) as enrichment — so this holds for both the onboard and manual-enrich streams. */
function isEnrichEvent(ev: SSEEvent): boolean {
  const d = ev.data as Record<string, unknown>
  if (d.stage === 'enriching') return true
  if (d.holding != null || d.fund != null) return true
  if (ev.event === 'classified' || ev.event === 'chunked' || ev.event === 'extracted') return true
  if (ev.event === 'job.started' && (d.top != null || Array.isArray(d.holdings))) return true
  return false
}

function OnboardStepView({ firm, events, error, done, onBack, onClose }: {
  firm: FirmCandidate | null
  events: SSEEvent[]
  error: string | null
  done: boolean
  onBack: () => void
  onClose: () => void
}) {
  const seriesCount = firm?.series.length ?? 0

  // Partition the stream into the structural onboarding events and the (optional) enrichment stage.
  // An event belongs to the enrichment stage when it is re-tagged with a `holding`, is an ingest-only
  // step (classified/chunked/extracted), or is the enrichment announcement (job.started with top/holdings).
  const structural = events.filter((e) => e.event !== 'job.completed' && !isEnrichEvent(e))
  const enriching = events.filter((e) => e.event !== 'job.completed' && isEnrichEvent(e))
  const completions = events.filter((e) => e.event === 'job.completed')
  // Merge every completion's data so the summary shows onboard counts (funds/holdings) AND enrichment
  // counts (enriched/risk_factors_added) regardless of which terminal event carried them.
  const summary: Record<string, unknown> = Object.assign({}, ...completions.map((c) => c.data))
  const terminal: SSEEvent | null = completions.length
    ? { ...completions[completions.length - 1], data: summary }
    : null

  return (
    <div>
      <div className="row" style={{ justifyContent: 'space-between', gap: 10, marginBottom: 12 }}>
        <div className="row" style={{ gap: 10, minWidth: 0 }}>
          <FirmGlyph name={firm?.name ?? ''} />
          <div style={{ minWidth: 0 }}>
            <div className="nowrap" style={{ fontWeight: 650, fontSize: 14, overflow: 'hidden', textOverflow: 'ellipsis' }}>{firm?.name}</div>
            <div className="faint" style={{ fontSize: 11.5 }}>
              {seriesCount} {seriesCount === 1 ? 'series' : 'series'} · onboarding into the graph
            </div>
          </div>
        </div>
        {error
          ? <span className="pill bad" style={{ flex: 'none' }}><Icon name="alert" size={13} />failed</span>
          : done
            ? <span className="pill good" style={{ flex: 'none' }}><Icon name="check" size={13} />ready</span>
            : <span className="pill accent" style={{ flex: 'none' }}><span className="spinner" style={{ margin: 0, width: 13, height: 13 }} />streaming</span>}
      </div>

      <div className="card card-pad">
        {events.length === 0 && !error && (
          <div className="empty" style={{ padding: 22 }}><span className="spinner" />Starting onboarding…</div>
        )}
        {structural.map((ev, i) => <OnboardEventRow key={`s${i}`} ev={ev} />)}
        {enriching.length > 0 && <EnrichSection rows={enriching} settled={done || !!error} />}
        {/* The merged terminal summary lands once the stream fully closes (`done`), so an intermediate
            onboard completion never flashes a premature "complete" row above the enrichment section. */}
        {done && terminal && <OnboardEventRow ev={terminal} />}
        {error && (
          <div className="row" style={{ alignItems: 'flex-start', gap: 10, padding: '9px 0 2px' }}>
            <span style={{ marginTop: 1, color: 'var(--bad)' }}><Icon name="alert" size={15} /></span>
            <div style={{ fontSize: 12.5, color: 'var(--bad)' }}>{error}</div>
          </div>
        )}
      </div>

      <div className="row" style={{ justifyContent: 'flex-end', gap: 8, marginTop: 14 }}>
        {(error || (!done && events.length === 0)) && (
          <button type="button" className="btn" onClick={onBack}>Back to search</button>
        )}
        {done
          ? <button type="button" className="btn btn-primary" onClick={onClose}>Done</button>
          : error
            ? <button type="button" className="btn" onClick={onClose}>Close</button>
            : null}
      </div>
    </div>
  )
}

/** The "Enriching filings…" sub-section: a labelled divider (with a live count of the holdings seen
 *  + a spinner until the run settles) followed by the re-tagged per-holding ingest rows. */
function EnrichSection({ rows, settled }: { rows: SSEEvent[]; settled: boolean }) {
  const subjects = new Set<string>()
  for (const e of rows) {
    const d = e.data as Record<string, unknown>
    const s = d.holding ?? d.fund
    if (s != null) subjects.add(String(s))
  }
  const n = subjects.size
  return (
    <div style={{ marginTop: 6, paddingTop: 8, borderTop: '1px solid var(--border)' }}>
      <div className="row" style={{ gap: 8, alignItems: 'center' }}>
        <span style={{ color: 'var(--accent)', display: 'inline-flex' }}><Icon name="impact" size={15} /></span>
        <span style={{ fontSize: 11.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Enriching filings…</span>
        {n > 0 && <span className="faint" style={{ fontSize: 11.5 }}>{n} {n === 1 ? 'filing' : 'filings'}</span>}
        {!settled && <span className="spinner" style={{ margin: 0, width: 12, height: 12 }} />}
      </div>
      <div className="faint" style={{ fontSize: 11, marginTop: 1 }}>
        Pulling top holdings’ 10-K risk factors and funds’ prospectus risks into the graph.
      </div>
      {rows.map((ev, i) => <OnboardEventRow key={`e${i}`} ev={ev} />)}
    </div>
  )
}

function OnboardEventRow({ ev }: { ev: SSEEvent }) {
  const m = describeEvent(ev)
  return (
    <div className="row" style={{ alignItems: 'flex-start', gap: 10, padding: '7px 0' }}>
      <span style={{ marginTop: 1, color: m.tone, display: 'inline-flex' }}><Icon name={m.icon} size={15} /></span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 13, fontWeight: 600 }}>{m.title}</div>
        {m.detail && <div className="faint" style={{ fontSize: 11.5, marginTop: 1 }}>{m.detail}</div>}
        {m.metrics.length > 0 && (
          <div className="row" style={{ gap: 12, marginTop: 3, flexWrap: 'wrap' }}>
            {m.metrics.map(([k, v]) => (
              <span key={k} style={{ fontSize: 11.5 }}><b>{v}</b> <span className="faint">{k}</span></span>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

/** Map an SSE onboarding/enrichment event to a friendly row. Structural onboarding steps and the
 *  re-tagged enrichment-stage steps (which carry a `holding`/`ticker`) share this renderer; it falls
 *  through to a generic renderer for any event type the pipeline emits that isn't in the known set. */
function describeEvent(ev: SSEEvent): { title: string; detail?: string; metrics: Metric[]; icon: IconName; tone: string } {
  const d = ev.data as Record<string, unknown>
  const str = (k: string): string | undefined => (d[k] == null ? undefined : String(d[k]))
  const numMetric = (label: string, k: string): Metric[] => (typeof d[k] === 'number' ? [[label, d[k] as number]] : [])
  // Enrichment-stage events are re-tagged with the holding (10-K) or fund (prospectus) they came from.
  const holding = str('holding')
  const fund = str('fund')
  const subject = holding ?? fund           // the enriched entity, when this is an enrichment-stage event
  const who = str('ticker') ?? subject      // preferred label (ticker for a holding, else the name)
  const kind = holding ? '10-K' : fund ? 'prospectus' : 'filing'

  switch (ev.event) {
    case 'job.started': {
      // The standalone enrich stream announces itself with a holdings list / top count (no per-holding
      // tag yet). In the onboard stream this event is folded away, so only the structural start shows.
      const holdingsArr = Array.isArray(d.holdings) ? (d.holdings as unknown[]) : null
      if (holdingsArr || d.top != null) {
        const cnt = holdingsArr ? holdingsArr.length : (typeof d.top === 'number' ? (d.top as number) : undefined)
        return {
          title: 'Fetching top-holding filings',
          detail: 'Pulling 10-K Item 1A (Risk Factors) from EDGAR',
          metrics: cnt != null ? [['holdings', cnt]] : [], icon: 'ingest', tone: 'var(--text-muted)',
        }
      }
      return { title: 'Job started', detail: str('firm') ? `Registered ${str('firm')}` : 'Firm record created', metrics: [], icon: 'play', tone: 'var(--accent)' }
    }
    case 'classified':
      return {
        title: who ? `Classified ${who} ${kind}` : 'Classified filing',
        detail: [str('doc_type'), str('sensitivity')].filter(Boolean).join(' · ') || undefined,
        metrics: [], icon: 'doc', tone: 'var(--text-muted)',
      }
    case 'chunked':
      return { title: who ? `Chunked ${who} ${kind}` : 'Chunked filing', metrics: numMetric('chunks', 'chunks'), icon: 'ingest', tone: 'var(--text-muted)' }
    case 'extracted':
      return {
        title: who ? `Extracted from ${who} ${kind}` : 'Extracted risk factors',
        metrics: [...numMetric('entities', 'entities'), ...numMetric('relations', 'relations')],
        icon: 'resolve', tone: 'var(--text-muted)',
      }
    case 'parsed':
      // Enrichment-stage parse (re-tagged with a holding/fund) vs the structural per-series parse.
      if (subject) {
        return { title: `Parsed ${who} ${kind}`, detail: str('pages') ? `${str('pages')} pages` : undefined, metrics: [], icon: 'doc', tone: 'var(--text-muted)' }
      }
      return {
        title: `Parsed ${str('fund_name') ?? str('series_id') ?? 'series'}`,
        detail: d.skipped ? (str('reason') ?? 'skipped') : (str('series_id') ? `Series ${str('series_id')}` : undefined),
        metrics: numMetric('holdings', 'holdings'), icon: 'doc', tone: d.skipped ? 'var(--warn)' : 'var(--text-muted)',
      }
    case 'written':
      return {
        title: subject ? `Wrote ${who} risk factors` : `Wrote ${str('fund_name') ?? 'fund'} to the graph`,
        metrics: [...numMetric('nodes', 'nodes'), ...numMetric('edges', 'edges')],
        icon: 'ingest', tone: 'var(--text-muted)',
      }
    case 'resolved':
      return {
        title: subject ? `Resolved ${who} issuers` : 'Resolved issuers',
        metrics: [...numMetric('merged', 'merged'), ...numMetric('provisional', 'provisional')],
        icon: 'merge', tone: 'var(--text-muted)',
      }
    case 'job.completed': {
      // Auto-enrich onboard nests its counts under `enrichment`; the standalone enrich endpoint puts
      // them top-level. Read whichever is present so the summary mentions enrichment either way.
      const enr = d.enrichment && typeof d.enrichment === 'object' ? (d.enrichment as Record<string, unknown>) : null
      const src = enr ?? d
      const enriched = typeof src.enriched === 'number' ? src.enriched : undefined
      const rf = typeof src.risk_factors_added === 'number' ? src.risk_factors_added : undefined
      const ranEnrich = enr ? enr.ran !== false : enriched != null
      const metrics: Metric[] = [...numMetric('funds', 'funds'), ...numMetric('holdings', 'holdings')]
      let detail = 'Firm added to the registry and set active'
      if (ranEnrich && enriched != null) {
        metrics.push(['enriched', enriched])
        if (rf != null) metrics.push(['risk factors', rf])
        detail = `Firm set active · ${enriched} ${enriched === 1 ? 'holding' : 'holdings'} enriched, ${rf ?? 0} risk ${rf === 1 ? 'factor' : 'factors'} added`
      } else if (enr && enr.ran === false) {
        detail = 'Firm added and set active · enrichment did not run'
      }
      return { title: 'Onboarding complete', detail, metrics, icon: 'check', tone: 'var(--good)' }
    }
    case 'error': {
      // The enrichment stage reports per-holding/per-fund failures as NON-fatal warnings (fatal === false).
      const nonFatal = d.fatal === false
      return {
        title: nonFatal ? `Skipped ${who ?? 'filing'}` : 'Error',
        detail: str('message') ?? 'Onboarding failed',
        metrics: [], icon: 'alert', tone: nonFatal ? 'var(--warn)' : 'var(--bad)',
      }
    }
    default: {
      const detail = Object.entries(d).map(([k, v]) => `${k}: ${v}`).join(' · ')
      return { title: ev.event, detail: detail || undefined, metrics: [], icon: 'info', tone: 'var(--text-muted)' }
    }
  }
}

function FirmGlyph({ name }: { name: string }) {
  const initial = name.trim().charAt(0).toUpperCase() || 'F'
  return (
    <span
      aria-hidden="true"
      style={{
        width: 30, height: 30, flex: 'none', borderRadius: 8, display: 'grid', placeItems: 'center',
        color: '#fff', fontWeight: 700, fontSize: 15, background: 'var(--accent)',
        boxShadow: 'inset 0 0 0 1px rgba(255,255,255,0.15)',
      }}
    >
      {initial}
    </span>
  )
}
