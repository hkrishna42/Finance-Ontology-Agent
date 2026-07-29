import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import cytoscape from 'cytoscape'
import type { Core, ElementDefinition, NodeSingular } from 'cytoscape'
import { getGraph, groundLabel, runSparql } from '../api'
import type { FiboGrounding, GraphData, GraphNode, SparqlResult } from '../types'
import { useLoaded } from '../lib/useLoaded'
import { entityColor, EntityTag, PanelHead, Segmented, SourceBadge } from '../lib/ui'
import { Icon } from '../lib/icons'
import type { NavTarget } from '../App'

type LayoutMode = 'force' | 'tree' | 'radial'

function themeColors() {
  const s = getComputedStyle(document.documentElement)
  const v = (n: string) => s.getPropertyValue(n).trim()
  return {
    text: v('--text') || '#1a1f27',
    muted: v('--text-faint') || '#8b929d',
    border: v('--border-strong') || '#cdd3db',
    accent: v('--accent') || '#3b5bdb',
    surface: v('--surface') || '#fff',
  }
}

/** cytoscape layout options per mode — all built-in (no dagre dep): cose / breadthfirst / concentric. */
function layoutFor(mode: LayoutMode): cytoscape.LayoutOptions {
  if (mode === 'tree')
    return { name: 'breadthfirst', directed: true, padding: 24, spacingFactor: 1.15, animate: false } as cytoscape.LayoutOptions
  if (mode === 'radial')
    return {
      name: 'concentric', padding: 24, minNodeSpacing: 24, animate: false,
      concentric: (n: NodeSingular) => n.degree(false),
      levelWidth: () => 2,
    } as cytoscape.LayoutOptions
  return { name: 'cose', animate: false, nodeRepulsion: 9000, idealEdgeLength: 90, padding: 24 } as cytoscape.LayoutOptions
}

/** Structured attribute keys surfaced in the node inspector (a "FI / CW / …"-style domain code isn't
 *  in the data — domains are grouped by ontology entity type, the mockup's practical meaning). */
const ATTR_KEYS = ['cik', 'lei', 'ticker', 'isin', 'country', 'series_id', 'category', 'norm', 'weight_pct']

export function GraphExplorer({ focus, themeKey, firm }: { focus?: NavTarget['focus']; themeKey: string; firm?: string | null }) {
  const { data, source, loading } = useLoaded<GraphData>(() => getGraph(firm ?? undefined), [firm])
  const boxRef = useRef<HTMLDivElement>(null)
  const miniRef = useRef<HTMLCanvasElement>(null)
  const cyRef = useRef<Core | null>(null)
  const miniXf = useRef<{ ox: number; oy: number; s: number; x1: number; y1: number } | null>(null)

  const [minConf, setMinConf] = useState(0)
  const [activePath, setActivePath] = useState<number | null>(null)
  const [layout, setLayout] = useState<LayoutMode>('force')
  const [search, setSearch] = useState('')
  const [hidden, setHidden] = useState<Set<string>>(new Set())
  const [selected, setSelected] = useState<GraphNode | null>(null)

  const layoutRef = useRef(layout)
  useEffect(() => { layoutRef.current = layout }, [layout])

  const elements = useMemo<ElementDefinition[]>(() => {
    if (!data) return []
    const nodes = data.nodes.map((n) => ({ data: { id: n.id, label: n.label, type: n.type, props: n.props ?? {} } }))
    const edges = data.edges.map((e) => ({
      data: { id: e.id, source: e.source, target: e.target, label: e.type, conf: e.confidence },
    }))
    return [...nodes, ...edges]
  }, [data])

  // Per-type counts drive the domain filter chips + legend.
  const typeCounts = useMemo(() => {
    const m = new Map<string, number>()
    for (const n of data?.nodes ?? []) m.set(n.type, (m.get(n.type) ?? 0) + 1)
    return [...m.entries()].sort((a, b) => b[1] - a[1])
  }, [data])

  // ---- minimap: draw node dots + a viewport rectangle into a small canvas (no React re-render) ----
  const drawMini = useCallback(() => {
    const cv = miniRef.current
    const cy = cyRef.current
    if (!cv || !cy || cy.nodes().length === 0) return
    const ctx = cv.getContext('2d')
    if (!ctx) return
    const W = cv.width, H = cv.height, pad = 6
    ctx.clearRect(0, 0, W, H)
    const bb = cy.nodes().boundingBox()
    const gw = Math.max(bb.w, 1), gh = Math.max(bb.h, 1)
    const s = Math.min((W - 2 * pad) / gw, (H - 2 * pad) / gh)
    const ox = pad + ((W - 2 * pad) - gw * s) / 2
    const oy = pad + ((H - 2 * pad) - gh * s) / 2
    miniXf.current = { ox, oy, s, x1: bb.x1, y1: bb.y1 }
    const mx = (x: number) => ox + (x - bb.x1) * s
    const my = (y: number) => oy + (y - bb.y1) * s
    cy.nodes().forEach((n) => {
      if (n.hasClass('hidden-type')) return
      const p = n.position()
      ctx.fillStyle = entityColor(n.data('type'))
      ctx.beginPath()
      ctx.arc(mx(p.x), my(p.y), 1.7, 0, 2 * Math.PI)
      ctx.fill()
    })
    const ext = cy.extent()
    const c = themeColors()
    ctx.strokeStyle = c.accent
    ctx.lineWidth = 1.2
    ctx.strokeRect(mx(ext.x1), my(ext.y1), (ext.x2 - ext.x1) * s, (ext.y2 - ext.y1) * s)
  }, [])

  const onMiniClick = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const cv = miniRef.current
    const cy = cyRef.current
    const xf = miniXf.current
    if (!cv || !cy || !xf) return
    const rect = cv.getBoundingClientRect()
    const px = (e.clientX - rect.left) * (cv.width / rect.width)
    const py = (e.clientY - rect.top) * (cv.height / rect.height)
    const modelX = xf.x1 + (px - xf.ox) / xf.s
    const modelY = xf.y1 + (py - xf.oy) / xf.s
    const z = cy.zoom()
    cy.animate({ pan: { x: cy.width() / 2 - modelX * z, y: cy.height() / 2 - modelY * z }, duration: 200 })
  }

  // ---- build / rebuild cytoscape when data or theme changes ----
  useEffect(() => {
    if (!boxRef.current || !elements.length) return
    const c = themeColors()
    const cy = cytoscape({
      container: boxRef.current,
      elements,
      style: [
        {
          selector: 'node',
          style: {
            'background-color': (ele: NodeSingular) => entityColor(ele.data('type')),
            label: 'data(label)', color: c.text, 'font-size': '9px', 'font-weight': 600,
            'text-valign': 'bottom', 'text-margin-y': 4, 'text-max-width': '90px', 'text-wrap': 'ellipsis',
            width: 22, height: 22, 'border-width': 2, 'border-color': c.surface,
          },
        },
        {
          selector: 'edge',
          style: {
            width: 1.4, 'line-color': c.border, 'target-arrow-color': c.border, 'target-arrow-shape': 'triangle',
            'arrow-scale': 0.8, 'curve-style': 'bezier', label: 'data(label)', 'font-size': '7px', color: c.muted,
            'text-rotation': 'autorotate', 'text-background-color': c.surface, 'text-background-opacity': 0.85,
            'text-background-padding': '1px',
          },
        },
        { selector: 'node.hl', style: { 'border-color': c.accent, 'border-width': 3, width: 28, height: 28, 'font-size': '11px', color: c.text } },
        { selector: 'node.sel', style: { 'border-color': c.accent, 'border-width': 4, width: 30, height: 30, 'font-size': '11px' } },
        { selector: 'node.hit', style: { 'border-color': c.accent, 'border-width': 3 } },
        { selector: 'edge.hl', style: { 'line-color': c.accent, 'target-arrow-color': c.accent, width: 3, color: c.accent, 'font-size': '9px', 'font-weight': 700 } },
        { selector: '.dim', style: { display: 'none' } },
        { selector: '.hidden-type', style: { display: 'none' } },
        { selector: '.faded', style: { opacity: 0.12 } },
      ],
      layout: layoutFor(layoutRef.current),
      wheelSensitivity: 0.2, minZoom: 0.2, maxZoom: 3,
    })
    cyRef.current = cy

    cy.on('tap', 'node', (evt) => {
      const n = evt.target as NodeSingular
      setSelected({ id: n.id(), type: n.data('type'), label: n.data('label'), props: n.data('props') })
    })
    cy.on('tap', (evt) => { if (evt.target === cy) setSelected(null) })

    let raf = 0
    const schedule = () => { if (raf) return; raf = requestAnimationFrame(() => { raf = 0; drawMini() }) }
    cy.on('pan zoom resize', schedule)
    cy.on('position', 'node', schedule)
    cy.on('layoutstop', schedule)
    cy.ready(() => drawMini())

    return () => { cancelAnimationFrame(raf); cy.destroy(); cyRef.current = null }
  }, [elements, themeKey, drawMini])

  // ---- re-run layout when the user switches mode (skip the initial mount) ----
  const firstLayout = useRef(true)
  useEffect(() => {
    if (firstLayout.current) { firstLayout.current = false; return }
    const cy = cyRef.current
    if (cy) cy.layout(layoutFor(layout)).run()
  }, [layout])

  // ---- filters: confidence + domain (hidden types) → dim edges + hide/fade nodes ----
  useEffect(() => {
    const cy = cyRef.current
    if (!cy) return
    cy.batch(() => {
      cy.nodes().forEach((n) => { n.toggleClass('hidden-type', hidden.has(n.data('type') as string)) })
      cy.edges().forEach((e) => {
        const lowConf = (e.data('conf') as number) < minConf
        const endHidden = hidden.has(e.source().data('type') as string) || hidden.has(e.target().data('type') as string)
        e.toggleClass('dim', lowConf || endHidden)
      })
      cy.nodes().forEach((n) => {
        if (n.hasClass('hidden-type')) { n.removeClass('faded'); return }
        const visibleDeg = n.connectedEdges().filter((e) => !e.hasClass('dim')).length
        n.toggleClass('faded', visibleDeg === 0 && cy.edges().length > 0)
      })
    })
    drawMini()
  }, [minConf, hidden, elements, drawMini])

  // ---- search: highlight matching nodes (name / type / cik / ticker / isin) + fit to them ----
  useEffect(() => {
    const cy = cyRef.current
    if (!cy) return
    const q = search.trim().toLowerCase()
    cy.nodes().removeClass('hit')
    if (!q) return
    const matches = cy.nodes().filter((n) => {
      if (n.hasClass('hidden-type')) return false
      const p = (n.data('props') as Record<string, unknown>) ?? {}
      const hay = [n.data('label'), n.data('type'), p.cik, p.ticker, p.isin, p.lei, p.curie, p.fibo_class]
        .filter(Boolean).join(' ').toLowerCase()
      return hay.includes(q)
    })
    matches.addClass('hit')
    if (matches.length) cy.animate({ fit: { eles: matches, padding: 80 }, duration: 250 })
  }, [search, elements])

  // ---- highlight a named path (or a focused node from navigation) ----
  useEffect(() => {
    const cy = cyRef.current
    if (!cy || !data) return
    cy.batch(() => {
      cy.elements().removeClass('hl')
      let ids: string[] | null = null
      if (activePath !== null && data.paths?.[activePath]) ids = data.paths[activePath].node_ids
      else if (focus?.node_id) ids = [focus.node_id]
      if (ids) {
        const set = new Set(ids)
        cy.nodes().forEach((n) => { if (set.has(n.id())) n.addClass('hl'); else n.addClass('faded') })
        cy.edges().forEach((e) => { e.toggleClass('hl', set.has(e.source().id()) && set.has(e.target().id())) })
        const hl = cy.$('.hl')
        if (hl.length) cy.animate({ fit: { eles: hl, padding: 80 }, duration: 300 })
      }
    })
  }, [activePath, focus, data])

  // keep the selected node's ring in sync
  useEffect(() => {
    const cy = cyRef.current
    if (!cy) return
    cy.nodes().removeClass('sel')
    if (selected) cy.getElementById(selected.id).addClass('sel')
  }, [selected, elements])

  const zoomBy = (f: number) => { const cy = cyRef.current; if (cy) cy.zoom({ level: cy.zoom() * f, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } }) }
  const fit = () => { const cy = cyRef.current; if (cy) cy.animate({ fit: { eles: cy.elements(), padding: 24 }, duration: 250 }) }
  const resetView = () => { setActivePath(null); setSearch(''); setHidden(new Set()); fit() }

  const toggleType = (t: string) => setHidden((prev) => {
    const next = new Set(prev)
    if (next.has(t)) next.delete(t); else next.add(t)
    return next
  })

  // Adjacent triples for the inspector, resolved from the loaded graph edges.
  const adjacency = useMemo(() => {
    if (!selected || !data) return [] as { rel: string; dir: '→' | '←'; other: string; otherType: string }[]
    const byId = new Map(data.nodes.map((n) => [n.id, n]))
    const out: { rel: string; dir: '→' | '←'; other: string; otherType: string }[] = []
    for (const e of data.edges) {
      if (e.source === selected.id) { const o = byId.get(e.target); out.push({ rel: e.type, dir: '→', other: o?.label ?? e.target, otherType: o?.type ?? '' }) }
      else if (e.target === selected.id) { const o = byId.get(e.source); out.push({ rel: e.type, dir: '←', other: o?.label ?? e.source, otherType: o?.type ?? '' }) }
    }
    return out.slice(0, 40)
  }, [selected, data])

  return (
    <div>
      <PanelHead
        title="Graph Explorer"
        sub="Search, filter by domain, and re-lay out the FIBO-grounded knowledge graph. Click any node for its FIBO OWL grounding, extracted attributes, adjacent triples, and lakehouse provenance — or query the reasoned TBox with SPARQL."
        right={source && <SourceBadge source={source} />}
      />

      <div className="graph-toolbar">
        <div className="graph-search">
          <Icon name="search" size={14} />
          <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search name, type, CIK, ticker, ISIN…" />
          {search && <button className="btn btn-ghost btn-sm" onClick={() => setSearch('')} aria-label="clear"><Icon name="close" size={13} /></button>}
        </div>
        <Segmented<LayoutMode>
          value={layout}
          onChange={setLayout}
          options={[{ value: 'force', label: 'Force' }, { value: 'tree', label: 'Tree' }, { value: 'radial', label: 'Radial' }]}
        />
        <div className="graph-zoom">
          <button className="btn btn-sm" onClick={() => zoomBy(1.3)} aria-label="zoom in" title="Zoom in">+</button>
          <button className="btn btn-sm" onClick={() => zoomBy(1 / 1.3)} aria-label="zoom out" title="Zoom out">−</button>
          <button className="btn btn-sm" onClick={fit} title="Fit to view"><Icon name="refresh" size={13} /></button>
        </div>
      </div>

      <div className="graph-layout">
        <div className="graph-canvas">
          <div ref={boxRef} style={{ width: '100%', height: '100%' }} />
          {(data?.nodes.length ?? 0) > 0 && (
            <canvas ref={miniRef} width={148} height={104} className="graph-minimap" onClick={onMiniClick} title="Overview — click to pan" />
          )}
          {loading && <div className="loading" style={{ position: 'absolute', inset: 0 }}><span className="spinner" />Loading graph…</div>}
          {!loading && (data?.nodes.length ?? 0) === 0 && (
            <div style={{ position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 8, textAlign: 'center', padding: 24, color: 'var(--text-muted)' }}>
              <Icon name="graph" size={22} />
              <div style={{ fontWeight: 600 }}>No subgraph for {firm ?? 'this firm'} yet</div>
              <div className="faint" style={{ fontSize: 12, maxWidth: 340, lineHeight: 1.5 }}>
                This firm's funds and holdings appear here once its graph is populated — run enrichment to add filing-derived nodes and edges.
              </div>
            </div>
          )}
          {(data?.nodes.length ?? 0) > 0 && <div className="graph-hint">scroll to zoom · drag to pan · click a node to inspect</div>}
        </div>

        <div className="graph-side">
          {selected && <NodeInspector node={selected} adjacency={adjacency} onClose={() => setSelected(null)} />}

          <div className="card card-pad">
            <div className="row" style={{ justifyContent: 'space-between', marginBottom: 8 }}>
              <strong style={{ fontSize: 13 }}>Domains</strong>
              {hidden.size > 0 && <button className="btn btn-ghost btn-sm" onClick={() => setHidden(new Set())}>show all</button>}
            </div>
            <div className="domain-chips">
              {typeCounts.map(([t, n]) => {
                const off = hidden.has(t)
                return (
                  <button key={t} className={`domain-chip ${off ? 'off' : ''}`} onClick={() => toggleType(t)} title={off ? 'Show' : 'Hide'}>
                    <span className="legend-dot" style={{ background: entityColor(t), opacity: off ? 0.3 : 1 }} />
                    {t}<span className="domain-count">{n}</span>
                  </button>
                )
              })}
            </div>
          </div>

          <div className="card card-pad">
            <div className="row" style={{ justifyContent: 'space-between', marginBottom: 10 }}>
              <strong style={{ fontSize: 13 }}>Confidence filter</strong>
              <span className="slider-val">{minConf.toFixed(2)}</span>
            </div>
            <div className="slider-row">
              <input type="range" min={0} max={1} step={0.01} value={minConf} onChange={(e) => setMinConf(Number(e.target.value))} />
            </div>
            <p className="faint" style={{ fontSize: 11.5, marginTop: 8 }}>
              Hides edges below the threshold. Structural edges (HOLDS, COVERS…) are confidence 1.0.
            </p>
          </div>

          {(data?.paths?.length ?? 0) > 0 && (
            <div className="card card-pad">
              <strong style={{ fontSize: 13 }}>Highlight a path</strong>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 10 }}>
                {data?.paths?.map((p, i) => (
                  <button key={i} className={`btn btn-sm ${activePath === i ? 'btn-primary' : ''}`} style={{ justifyContent: 'flex-start' }} onClick={() => setActivePath(activePath === i ? null : i)}>
                    <Icon name="impact" size={13} /> {p.label}
                  </button>
                ))}
                <button className="btn btn-ghost btn-sm" style={{ justifyContent: 'flex-start' }} onClick={resetView}>
                  <Icon name="refresh" size={13} /> Reset view
                </button>
              </div>
            </div>
          )}

          <SparqlBox />
        </div>
      </div>
    </div>
  )
}

// ---- Node inspector: FIBO grounding + attributes + adjacent triples + lakehouse provenance ----

function NodeInspector({ node, adjacency, onClose }: {
  node: GraphNode
  adjacency: { rel: string; dir: '→' | '←'; other: string; otherType: string }[]
  onClose: () => void
}) {
  const props = (node.props ?? {}) as Record<string, unknown>
  const [fibo, setFibo] = useState<FiboGrounding | null>(null)

  useEffect(() => {
    let alive = true
    setFibo(null)
    const category = typeof props.category === 'string' ? props.category : undefined
    groundLabel(node.type, category).then((g) => { if (alive) setFibo(g) })
    return () => { alive = false }
  }, [node.id, node.type]) // eslint-disable-line react-hooks/exhaustive-deps

  // Prefer a per-node stamped grounding (MDM/ingest nodes carry it); else the on-demand label default.
  const curie = (typeof props.fibo_class === 'string' && props.fibo_class) || fibo?.curie || null
  const reasoningValid = props.reasoning_valid
  const lhTable = typeof props.lakehouse_table === 'string' ? props.lakehouse_table : null
  const lhPk = typeof props.lakehouse_pk === 'string' ? props.lakehouse_pk : null
  const attrs = ATTR_KEYS.filter((k) => props[k] != null && props[k] !== '')

  return (
    <div className="card card-pad node-inspector">
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
          <EntityTag type={node.type} />
          <strong style={{ fontSize: 14 }}>{node.label}</strong>
        </div>
        <button className="btn btn-ghost btn-sm" onClick={onClose} aria-label="close"><Icon name="close" size={13} /></button>
      </div>

      <div className="insp-section">
        <div className="insp-head">FIBO grounding</div>
        {curie ? (
          <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
            <span className="pill good"><Icon name="check" size={12} /> FIBO grounded</span>
            <code className="fibo-curie">{curie}</code>
            {reasoningValid !== undefined && (
              <span className={`pill ${reasoningValid ? 'good' : 'warn'}`}>{reasoningValid ? 'reasoning valid' : 'violation'}</span>
            )}
            {fibo?.refined && <span className="pill">refined</span>}
          </div>
        ) : (
          <span className="faint" style={{ fontSize: 12 }}>Not grounded to a FIBO class.</span>
        )}
      </div>

      <div className="insp-section">
        <div className="insp-head">Extracted attributes</div>
        {attrs.length ? (
          <div className="insp-attrs">
            {attrs.map((k) => (
              <div className="insp-attr" key={k}><span className="insp-k">{k}</span><span className="insp-v mono">{String(props[k])}</span></div>
            ))}
          </div>
        ) : <span className="faint" style={{ fontSize: 12 }}>No structured attributes on this node.</span>}
      </div>

      <div className="insp-section">
        <div className="insp-head">Lakehouse provenance</div>
        {lhTable && lhPk ? (
          <code className="fibo-curie">Lakehouse.{lhTable} (PK: {lhPk})</code>
        ) : <span className="faint" style={{ fontSize: 12 }}>Not mapped to a gold dimension yet — reconcile it in Master Data.</span>}
      </div>

      <div className="insp-section">
        <div className="insp-head">Adjacent triples <span className="faint">({adjacency.length})</span></div>
        {adjacency.length ? (
          <div className="insp-triples">
            {adjacency.map((a, i) => (
              <div className="insp-triple" key={i}>
                <span className="rel">{a.dir === '→' ? '' : '← '}{a.rel}{a.dir === '→' ? ' →' : ''}</span>
                <span className="legend-dot" style={{ background: entityColor(a.otherType) }} />
                <span className="other">{a.other}</span>
              </div>
            ))}
          </div>
        ) : <span className="faint" style={{ fontSize: 12 }}>No adjacent edges in the current subgraph.</span>}
      </div>
    </div>
  )
}

// ---- SPARQL box: run a read-only query over the reasoned FIBO TBox ----

const SPARQL_EXAMPLES: { label: string; query: string }[] = [
  {
    label: 'All FIBO classes',
    query: 'SELECT ?c ?label WHERE { ?c a owl:Class . OPTIONAL { ?c rdfs:label ?label } } ORDER BY ?c',
  },
  {
    label: 'Subclasses of LegalEntity',
    query: 'PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\nSELECT ?sub WHERE { ?sub rdfs:subClassOf+ ?c . ?c rdfs:label "Legal Entity" }',
  },
]

function SparqlBox() {
  const [query, setQuery] = useState(SPARQL_EXAMPLES[0].query)
  const [res, setRes] = useState<SparqlResult | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const run = async () => {
    setBusy(true); setErr(null)
    const out = await runSparql(query)
    setBusy(false)
    if ('error' in out) { setErr(out.error); setRes(null) }
    else { setRes(out); setErr(null) }
  }

  return (
    <div className="card card-pad">
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 8 }}>
        <strong style={{ fontSize: 13 }}>SPARQL over the FIBO TBox</strong>
      </div>
      <textarea className="sparql-input mono" value={query} onChange={(e) => setQuery(e.target.value)} rows={4} spellCheck={false} />
      <div className="row" style={{ gap: 6, marginTop: 8, flexWrap: 'wrap' }}>
        <button className="btn btn-primary btn-sm" onClick={run} disabled={busy}>
          <Icon name="play" size={13} />{busy ? 'Running…' : 'Run query'}
        </button>
        {SPARQL_EXAMPLES.map((ex) => (
          <button key={ex.label} className="btn btn-ghost btn-sm" onClick={() => setQuery(ex.query)}>{ex.label}</button>
        ))}
      </div>
      {err && <div className="sparql-err">{err}</div>}
      {res && !err && (
        <div className="sparql-result">
          <div className="faint" style={{ fontSize: 11.5, margin: '8px 0 4px' }}>{res.count} row{res.count === 1 ? '' : 's'}</div>
          <div className="sparql-table-wrap">
            <table className="sparql-table">
              <thead><tr>{res.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
              <tbody>
                {res.rows.slice(0, 50).map((row, i) => (
                  <tr key={i}>{row.map((cell, j) => <td key={j} className="mono">{cellText(cell)}</td>)}</tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}

/** Shorten a full IRI to its local name for compact display; leave literals as-is. */
function cellText(cell: unknown): string {
  const s = cell == null ? '' : String(cell)
  const m = /[#/]([^#/]+)$/.exec(s)
  return s.startsWith('http') && m ? m[1] : s
}
