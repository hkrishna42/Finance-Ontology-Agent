import { useEffect, useRef, useState } from 'react'
import { DEMO_SEQUENCE } from '../api'
import type { EventType, SSEEvent } from '../types'
import { PanelHead } from '../lib/ui'
import { Icon } from '../lib/icons'

interface Stage { key: EventType; title: string; desc: string }
const STAGES: Stage[] = [
  { key: 'job.started', title: 'Job started', desc: 'Document accepted into the ingest pipeline' },
  { key: 'classified', title: 'Classified', desc: 'Doc type + sensitivity detected (routes the pipeline)' },
  { key: 'parsed', title: 'Parsed', desc: 'Text + layout extracted from the filing' },
  { key: 'chunked', title: 'Chunked', desc: 'Split into embedded passages (bge-small, 384-dim)' },
  { key: 'extracted', title: 'Extracted', desc: 'Agent emits entities + relations; ungrounded spans dropped' },
  { key: 'resolved', title: 'Resolved', desc: 'Entity resolution — merged to canonical, rest provisional' },
  { key: 'written', title: 'Written to graph', desc: 'Steward writes nodes + edges; schema-invalid rejected' },
  { key: 'impact', title: 'Change impact', desc: 'Fact-diff propagates to funds + disclosure sections' },
  { key: 'job.completed', title: 'Completed', desc: 'Pipeline finished' },
]

type Status = 'done' | 'active' | 'pending' | 'error'

export function IngestPanel() {
  const [received, setReceived] = useState<Record<string, SSEEvent>>({})
  const [running, setRunning] = useState(false)
  const [mode, setMode] = useState<'live' | 'simulated' | null>(null)
  const [errored, setErrored] = useState(false)
  const esRef = useRef<EventSource | null>(null)
  const timers = useRef<number[]>([])
  const gotAny = useRef(false)

  const cleanup = () => {
    esRef.current?.close(); esRef.current = null
    timers.current.forEach(clearTimeout); timers.current = []
  }
  useEffect(() => cleanup, [])

  const push = (ev: SSEEvent) => setReceived((prev) => ({ ...prev, [ev.event]: ev }))

  const simulate = () => {
    setMode('simulated')
    DEMO_SEQUENCE.forEach((s, i) => {
      const t = window.setTimeout(() => {
        push({ event: s.event, job_id: 'sim', seq: i, data: s.data })
        if (s.event === 'job.completed') setRunning(false)
      }, 450 * (i + 1))
      timers.current.push(t)
    })
  }

  const run = () => {
    cleanup()
    setReceived({}); setErrored(false); setRunning(true); gotAny.current = false
    setMode('live')
    let es: EventSource
    try {
      es = new EventSource('/ingest/demo/stream')
    } catch {
      simulate(); return
    }
    esRef.current = es
    const types: EventType[] = STAGES.map((s) => s.key)
    for (const t of types) {
      es.addEventListener(t, (raw) => {
        gotAny.current = true
        try {
          const parsed = JSON.parse((raw as MessageEvent).data) as SSEEvent
          push(parsed)
        } catch { /* ignore malformed */ }
        if (t === 'job.completed') { es.close(); setRunning(false) }
      })
    }
    es.addEventListener('error', () => { push({ event: 'error', job_id: 'x', seq: 0, data: { message: 'stream error' } }); setErrored(true) })
    es.onerror = () => {
      es.close()
      // No backend reachable → fall back to the committed demo sequence so the panel still animates.
      if (!gotAny.current) simulate()
      else setRunning(false)
    }
  }

  const statusFor = (key: EventType, idx: number): Status => {
    if (errored && !received[key]) return 'pending'
    if (received[key]) return 'done'
    const doneCount = STAGES.filter((s) => received[s.key]).length
    if (running && idx === doneCount) return 'active'
    return 'pending'
  }

  const started = Object.keys(received).length > 0
  const completed = !!received['job.completed']

  return (
    <div>
      <PanelHead
        title="Ingest Pipeline"
        sub="Live server-sent events from /ingest/demo/stream, animated as a timeline. Runs against the API when reachable; otherwise replays the committed demo sequence so the pipeline still animates offline."
        right={
          <div className="row" style={{ gap: 8 }}>
            {mode && <span className={`pill ${mode === 'live' ? 'good' : ''}`}>{mode === 'live' ? 'live SSE' : 'simulated'}</span>}
            <button className="btn btn-primary" onClick={run} disabled={running}>
              {running ? <><span className="spinner" />Streaming…</> : <><Icon name="play" size={14} />Run demo ingest</>}
            </button>
          </div>
        }
      />

      <div className="grid grid-4" style={{ marginBottom: 18 }}>
        <Stat label="Chunks" value={received['chunked']?.data.chunks as number} />
        <Stat label="Entities + relations" value={num(received['extracted']?.data.entities) + num(received['extracted']?.data.relations)} sub={received['extracted'] ? `${num(received['extracted']?.data.dropped)} dropped (ungrounded)` : undefined} />
        <Stat label="Graph writes" value={num(received['written']?.data.nodes) + num(received['written']?.data.edges)} sub={received['written'] ? `${num(received['written']?.data.rejected)} rejected` : undefined} />
        <Stat label="Affected funds" value={received['impact']?.data.affected_funds as number} sub={received['impact'] ? `${num(received['impact']?.data.stale_sections)} stale sections` : undefined} />
      </div>

      <div className="card card-pad">
        {!started && !running && (
          <div className="empty">Click <strong>Run demo ingest</strong> to stream the NVIDIA 8-K through the pipeline.</div>
        )}
        {(started || running) && (
          <div className="timeline">
            {STAGES.map((s, i) => {
              const st = statusFor(s.key, i)
              const ev = received[s.key]
              return (
                <div key={s.key} className={`tl-item ${st}`}>
                  <div className="tl-rail">
                    <div className="tl-dot">{st === 'done' ? <Icon name="check" size={13} /> : st === 'active' ? '' : i + 1}</div>
                    {i < STAGES.length - 1 && <div className="tl-line" />}
                  </div>
                  <div className="tl-body">
                    <div className="tl-title">{s.title}{st === 'active' && <span className="pill accent" style={{ fontSize: 10, padding: '1px 7px' }}>running</span>}</div>
                    <div className="faint" style={{ fontSize: 12 }}>{s.desc}</div>
                    {ev && <StageData ev={ev} />}
                  </div>
                </div>
              )
            })}
          </div>
        )}
        {completed && <div className="pill good" style={{ marginTop: 8 }}><Icon name="check" size={13} />Ingest complete — graph updated, change-impact briefing generated</div>}
      </div>
    </div>
  )
}

function num(v: unknown): number { return typeof v === 'number' ? v : 0 }

function Stat({ label, value, sub }: { label: string; value?: number; sub?: string }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value === undefined || Number.isNaN(value) ? '—' : value}</div>
      {sub && <div className="stat-foot">{sub}</div>}
    </div>
  )
}

function StageData({ ev }: { ev: SSEEvent }) {
  const d = ev.data
  if (ev.event === 'extracted') return <Metrics items={[['entities', d.entities], ['relations', d.relations], ['dropped', d.dropped]]} />
  if (ev.event === 'written') return <Metrics items={[['nodes', d.nodes], ['edges', d.edges], ['rejected', d.rejected]]} />
  if (ev.event === 'resolved') return <Metrics items={[['merged', d.merged], ['provisional', d.provisional]]} />
  if (ev.event === 'impact') return <Metrics items={[['added', d.added], ['changed', d.changed], ['affected funds', d.affected_funds], ['stale sections', d.stale_sections]]} />
  const entries = Object.entries(d)
  if (!entries.length) return null
  return <div className="tl-data">{entries.map(([k, v]) => `${k}: ${v}`).join('  ·  ')}</div>
}

function Metrics({ items }: { items: [string, unknown][] }) {
  return (
    <div className="tl-metrics">
      {items.map(([k, v]) => (
        <span className="tl-metric" key={k}><b>{String(v ?? '—')}</b> <span className="faint">{k}</span></span>
      ))}
    </div>
  )
}
