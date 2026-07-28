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
      setQuery(''); setSearching(false); setSearchError(false); setResults(null)
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
    }
    try {
      await onboardFirm(payload, (ev) => {
        if (streamId.current !== myStream) return // stream superseded (modal closed / re-picked)
        setEvents((prev) => [...prev, ev])
        if (ev.event === 'error') {
          setOnboardError(String((ev.data as Record<string, unknown>).message ?? 'Onboarding failed'))
        } else if (ev.event === 'job.completed') {
          setDone(true)
          if (!doneRef.current) {
            doneRef.current = true
            onDone(c.name)                                   // App refreshes /firms + selects it
            closeTimer.current = window.setTimeout(onClose, 1300) // then dismiss automatically
          }
        }
      })
      // Stream closed without a terminal event → flag it so the user isn't stuck on a spinner.
      if (streamId.current === myStream && !doneRef.current) {
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
                results={results} onSearch={runSearch} onPick={pick}
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

function SearchStep({ query, setQuery, searching, searchError, results, onSearch, onPick }: {
  query: string
  setQuery: (v: string) => void
  searching: boolean
  searchError: boolean
  results: FirmCandidate[] | null
  onSearch: () => void
  onPick: (c: FirmCandidate) => void
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

function OnboardStepView({ firm, events, error, done, onBack, onClose }: {
  firm: FirmCandidate | null
  events: SSEEvent[]
  error: string | null
  done: boolean
  onBack: () => void
  onClose: () => void
}) {
  const seriesCount = firm?.series.length ?? 0
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
        {events.map((ev, i) => <OnboardEventRow key={i} ev={ev} />)}
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

/** Map an SSE onboarding event to a friendly row. Falls through to a generic renderer for any
 *  event type the pipeline emits that isn't in the known set (e.g. a per-series warning). */
function describeEvent(ev: SSEEvent): { title: string; detail?: string; metrics: Metric[]; icon: IconName; tone: string } {
  const d = ev.data as Record<string, unknown>
  const str = (k: string): string | undefined => (d[k] == null ? undefined : String(d[k]))
  const numMetric = (label: string, k: string): Metric[] => (typeof d[k] === 'number' ? [[label, d[k] as number]] : [])

  switch (ev.event) {
    case 'job.started':
      return { title: 'Job started', detail: str('firm') ? `Registered ${str('firm')}` : 'Firm record created', metrics: [], icon: 'play', tone: 'var(--accent)' }
    case 'parsed':
      return {
        title: `Parsed ${str('fund_name') ?? str('series_id') ?? 'series'}`,
        detail: str('series_id') ? `Series ${str('series_id')}` : undefined,
        metrics: numMetric('holdings', 'holdings'), icon: 'doc', tone: 'var(--text-muted)',
      }
    case 'written':
      return {
        title: `Wrote ${str('fund_name') ?? 'fund'} to the graph`,
        metrics: [...numMetric('nodes', 'nodes'), ...numMetric('edges', 'edges')],
        icon: 'ingest', tone: 'var(--text-muted)',
      }
    case 'resolved':
      return {
        title: 'Resolved issuers',
        metrics: [...numMetric('merged', 'merged'), ...numMetric('provisional', 'provisional')],
        icon: 'merge', tone: 'var(--text-muted)',
      }
    case 'job.completed':
      return {
        title: 'Onboarding complete', detail: 'Firm added to the registry and set active',
        metrics: [...numMetric('funds', 'funds'), ...numMetric('holdings', 'holdings')],
        icon: 'check', tone: 'var(--good)',
      }
    case 'error':
      return { title: 'Error', detail: str('message') ?? 'Onboarding failed', metrics: [], icon: 'alert', tone: 'var(--bad)' }
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
