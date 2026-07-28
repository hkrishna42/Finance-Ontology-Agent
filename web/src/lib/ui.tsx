import type { ReactNode } from 'react'
import type { EntityLabel, GraphPath } from '../types'
import type { Source } from '../api'
import { Icon } from './icons'

// ---- Entity color palette (readable over both light & dark node fills) ----
export const ENTITY_COLORS: Record<string, string> = {
  Company: '#3b82f6',
  Fund: '#7048e8',
  RiskFactor: '#e8590c',
  Person: '#0ca678',
  DisclosureSection: '#2b8a3e',
  MetricObservation: '#f08c00',
  RegulatoryForm: '#868e96',
  Region: '#15aabf',
  Product: '#ae3ec9',
  BusinessSegment: '#4263eb',
  Event: '#e64980',
  Document: '#495057',
  Chunk: '#adb5bd',
  GeneratedReport: '#099268',
}
export const entityColor = (t: string): string => ENTITY_COLORS[t] ?? '#868e96'

// ---- Formatters ----
export function usd(n: number): string {
  if (n >= 1e9) return `$${(n / 1e9).toFixed(2)}B`
  if (n >= 1e6) return `$${(n / 1e6).toFixed(0)}M`
  if (n >= 1e3) return `$${(n / 1e3).toFixed(0)}K`
  return `$${n}`
}
export const pct = (n: number): string => `${n.toFixed(1)}%`

// ---- Source badge (fixture vs live) ----
export function SourceBadge({ source }: { source: Source }) {
  return (
    <span className={`src-badge ${source}`} title={source === 'live' ? 'Served by the live API' : 'Served by a committed fixture (endpoint not yet live)'}>
      <span className="dot" style={{ width: 6, height: 6, borderRadius: '50%', background: 'currentColor' }} />
      {source === 'live' ? 'live' : 'fixture'}
    </span>
  )
}

// ---- Panel header ----
export function PanelHead({ title, sub, right }: { title: string; sub?: string; right?: ReactNode }) {
  return (
    <div className="panel-head">
      <div className="panel-title-row">
        <h1 className="panel-title">{title}</h1>
        {right}
      </div>
      {sub && <p className="panel-sub">{sub}</p>}
    </div>
  )
}

// ---- Segmented control ----
export function Segmented<T extends string>({ value, options, onChange }: {
  value: T
  options: { value: T; label: string }[]
  onChange: (v: T) => void
}) {
  return (
    <div className="segmented" role="tablist">
      {options.map((o) => (
        <button key={o.value} className={o.value === value ? 'active' : ''} onClick={() => onChange(o.value)} role="tab" aria-selected={o.value === value}>
          {o.label}
        </button>
      ))}
    </div>
  )
}

// ---- On/off switch ----
export function Switch({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label?: string }) {
  return (
    <button className={`switch ${checked ? 'on' : ''}`} onClick={() => onChange(!checked)} aria-pressed={checked} type="button">
      <span className="track"><span className="thumb" /></span>
      {label && <span className="switch-label">{label}</span>}
    </button>
  )
}

// ---- Drawer ----
export function Drawer({ open, title, sub, onClose, children }: {
  open: boolean; title: string; sub?: string; onClose: () => void; children: ReactNode
}) {
  if (!open) return null
  return (
    <>
      <div className="drawer-scrim" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-label={title}>
        <div className="drawer-head">
          <div style={{ flex: 1 }}>
            <h2 style={{ fontSize: 16 }}>{title}</h2>
            {sub && <p className="panel-sub" style={{ marginTop: 2 }}>{sub}</p>}
          </div>
          <button className="icon-btn" onClick={onClose} aria-label="Close"><Icon name="close" /></button>
        </div>
        <div className="drawer-body">{children}</div>
      </aside>
    </>
  )
}

// ---- Cypher code block (light keyword highlight) ----
const CYPHER_KW = /\b(MATCH|OPTIONAL|WHERE|RETURN|WITH|UNWIND|ORDER BY|COLLECT|DISTINCT|NOT|EXISTS|AS|AND|OR|SUM|MAX|ROUND|CALL|YIELD)\b/g
export function Cypher({ query }: { query: string }) {
  const parts = query.split(CYPHER_KW)
  return (
    <pre className="code">
      {parts.map((p, i) => (CYPHER_KW.test(p) ? <span key={i} className="kw">{p}</span> : <span key={i}>{p}</span>))}
    </pre>
  )
}

// ---- Graph path visualization (chain of node → REL → node) ----
export function PathViz({ path, highlight }: { path: GraphPath; highlight?: boolean }) {
  const steps = path.steps
  if (!steps.length) return null
  return (
    <div>
      {path.label && <div className="faint" style={{ fontSize: 12, marginBottom: 6, fontWeight: 600 }}>{path.label}</div>}
      <div className="path-row">
        {steps.map((s, i) => (
          <span key={i} style={{ display: 'contents' }}>
            {i === 0 && <span className={`path-node ${highlight ? 'hl' : ''}`}>{s.from}</span>}
            <span className="path-arrow">
              <span className="rel-tag">{s.rel}</span>
              <Icon name="arrow" size={14} />
              {s.qualifiers && Object.keys(s.qualifiers).length > 0 && (
                <span className="faint" style={{ fontSize: 10 }}>
                  {Object.entries(s.qualifiers).map(([k, v]) => `${k}=${v}`).join(' · ')}
                </span>
              )}
            </span>
            <span className={`path-node ${highlight ? 'hl' : ''}`}>{s.to}</span>
          </span>
        ))}
      </div>
    </div>
  )
}

// ---- Entity tag ----
export function EntityTag({ type }: { type: EntityLabel | string }) {
  return (
    <span className="tag">
      <span className="swatch" style={{ background: entityColor(type) }} />
      {type}
    </span>
  )
}
