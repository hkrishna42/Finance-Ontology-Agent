import { lazy, Suspense, useEffect, useMemo, useState } from 'react'
import { getActiveFirm, getHealth, getOntologyInfo } from './api'
import type { Health, OntologyInfo } from './types'
import { useTheme } from './lib/theme'
import { Icon } from './lib/icons'
import type { IconName } from './lib/icons'
import { ChatPanel } from './components/ChatPanel'
import { DocViewer } from './components/DocViewer'
import { IngestPanel } from './components/IngestPanel'
import { ResolutionQueue } from './components/ResolutionQueue'
import { ImpactFeed } from './components/ImpactFeed'
import { ReportCenter } from './components/ReportCenter'
import { EvalPanel } from './components/EvalPanel'
import { FirmSelector } from './components/FirmSelector'
import { AddFirmModal } from './components/AddFirmModal'

// Code-split the heavy panels (cytoscape / recharts) so the initial bundle stays lean.
const GraphExplorer = lazy(() => import('./components/GraphExplorer').then((m) => ({ default: m.GraphExplorer })))
const RiskDashboard = lazy(() => import('./components/RiskDashboard').then((m) => ({ default: m.RiskDashboard })))

export type PanelId = 'chat' | 'graph' | 'docs' | 'ingest' | 'resolve' | 'risk' | 'impact' | 'reports' | 'eval'
export interface NavTarget {
  panel: PanelId
  focus?: { doc_id?: string; chunk_id?: string; node_id?: string }
}

interface NavDef { id: PanelId; label: string; icon: IconName; section: string }
const NAV: NavDef[] = [
  { id: 'chat', label: 'Analyst Chat', icon: 'chat', section: 'Ask & Explore' },
  { id: 'graph', label: 'Graph Explorer', icon: 'graph', section: 'Ask & Explore' },
  { id: 'docs', label: 'Documents', icon: 'doc', section: 'Ask & Explore' },
  { id: 'ingest', label: 'Ingest Pipeline', icon: 'ingest', section: 'Pipeline' },
  { id: 'resolve', label: 'Resolution Queue', icon: 'resolve', section: 'Pipeline' },
  { id: 'risk', label: 'Risk Dashboard', icon: 'risk', section: 'Analytics' },
  { id: 'impact', label: 'Change Impact', icon: 'impact', section: 'Analytics' },
  { id: 'reports', label: 'Report Center', icon: 'report', section: 'Analytics' },
  { id: 'eval', label: 'Evaluation', icon: 'eval', section: 'Analytics' },
]
const SECTIONS = ['Ask & Explore', 'Pipeline', 'Analytics']

export default function App() {
  const { pref, cycle } = useTheme()
  const [active, setActive] = useState<PanelId>('chat')
  const [focus, setFocus] = useState<NavTarget['focus']>()
  const [health, setHealth] = useState<Health | null>(null)
  const [info, setInfo] = useState<OntologyInfo | null>(null)
  // The active firm NAME, threaded into every scoped panel so its getter sends ?firm=/firm and the
  // server (and the fixture guard) scope to the right firm. Starts null; resolved from /firms/active
  // on mount and set eagerly on a firm switch. Panels feed it into their useLoaded deps, so the
  // null→name resolution re-triggers their fetch even without a remount.
  const [activeFirm, setActiveFirm] = useState<string | null>(null)
  // Bumped whenever the active firm changes → remounts the visible panel (its useLoaded re-runs
  // against the newly-active firm, which the server scopes every panel to) and refreshes the chips.
  const [reloadKey, setReloadKey] = useState(0)
  // Controls the "Add firm" onboarding modal, opened from the FirmSelector's "+ Add firm…" entry.
  const [addFirmOpen, setAddFirmOpen] = useState(false)

  useEffect(() => {
    getHealth().then((h) => setHealth(h?.data ?? null))
    getOntologyInfo().then((i) => setInfo(i.data))
    getActiveFirm().then((r) => setActiveFirm(r.data?.name ?? null))
  }, [reloadKey])

  const navigate = (t: NavTarget) => { setActive(t.panel); setFocus(t.focus) }
  const clickNav = (id: PanelId) => { setActive(id); setFocus(undefined) }

  const themeKey = useMemo(() => {
    if (pref !== 'system') return pref
    return typeof matchMedia !== 'undefined' && matchMedia('(prefers-color-scheme: dark)').matches ? 'sys-dark' : 'sys-light'
  }, [pref])

  const themeIcon: IconName = pref === 'light' ? 'sun' : pref === 'dark' ? 'moon' : 'monitor'
  const activeDef = NAV.find((n) => n.id === active)!

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">F</div>
          <div>
            <div className="brand-name">Firm Ontology</div>
            <div className="brand-sub">Demo Investment Management · POC</div>
          </div>
        </div>
        <nav className="nav">
          {SECTIONS.map((sec) => (
            <div key={sec}>
              <div className="nav-section">{sec}</div>
              {NAV.filter((n) => n.section === sec).map((n) => (
                <button key={n.id} className={`nav-item ${active === n.id ? 'active' : ''}`} onClick={() => clickNav(n.id)}>
                  <Icon name={n.icon} className="nav-ico" />{n.label}
                </button>
              ))}
            </div>
          ))}
        </nav>
        <div className="sidebar-foot">
          Multi-agent KG over SEC filings.<br />Fixture-first UI — panels auto-wire to live endpoints.
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <Icon name={activeDef.icon} size={17} />
          <span className="topbar-title">{activeDef.label}</span>
          <span className="topbar-spacer" />

          <FirmSelector
            onSelect={(firm) => { setActiveFirm(firm.name); setReloadKey((k) => k + 1) }}
            onAddFirm={() => setAddFirmOpen(true)}
            reloadKey={reloadKey}
          />

          {info && (
            <span className="pill" title="Ontology single source of truth">
              {info.entity_count} entities · {info.relation_count} relations · {info.vector_dim}-dim
            </span>
          )}
          {health ? (
            <span className="pill good"><span className="dot" />API OK{health.mode ? ` · ${health.mode}` : ''}</span>
          ) : (
            <span className="pill" title="Live API not reachable — panels are served from committed fixtures"><span className="dot" />API offline · fixtures</span>
          )}
          <button className="icon-btn" onClick={cycle} title={`Theme: ${pref} (click to change)`} aria-label="Toggle theme">
            <Icon name={themeIcon} size={17} />
          </button>
        </header>

        <main className="content">
          <div className="content-inner" key={reloadKey}>
            <Suspense fallback={<div className="loading"><span className="spinner" />Loading…</div>}>
              {active === 'chat' && <ChatPanel onNavigate={navigate} firm={activeFirm} />}
              {active === 'graph' && <GraphExplorer focus={focus} themeKey={themeKey} firm={activeFirm} />}
              {active === 'docs' && <DocViewer focus={focus} firm={activeFirm} />}
              {active === 'ingest' && <IngestPanel />}
              {active === 'resolve' && <ResolutionQueue />}
              {active === 'risk' && <RiskDashboard />}
              {active === 'impact' && <ImpactFeed onNavigate={navigate} />}
              {active === 'reports' && <ReportCenter onNavigate={navigate} />}
              {active === 'eval' && <EvalPanel />}
            </Suspense>
          </div>
        </main>
      </div>

      <AddFirmModal
        open={addFirmOpen}
        onClose={() => setAddFirmOpen(false)}
        onDone={() => setReloadKey((k) => k + 1)}
      />
    </div>
  )
}
