import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import cytoscape from 'cytoscape'
import type { Core, ElementDefinition, NodeSingular } from 'cytoscape'
import { getGraph, groundLabel, runSparql } from '../api'
import type { FiboGrounding, GraphData, GraphNode, SparqlResult } from '../types'
import { useLoaded } from '../lib/useLoaded'
import { entityColor, EntityTag, PanelHead, Segmented, SourceBadge } from '../lib/ui'
import { Icon } from '../lib/icons'
import type { NavTarget } from '../App'
import {
  buildIndex, computeReveal, fiboClassOf, nodeMatches, relationCategory, RELATION_CATEGORIES,
  type GraphIndex,
} from '../lib/graphModel'
import '../styles/graph-explorer.css'

type LayoutMode = 'force' | 'tree' | 'radial'
type ViewMode = 'focus' | 'full'

/** Max neighbours revealed per expanded/focused hub before we stop (and tell the user to search).
 *  Keeps one 100+-holding fund from re-creating the hairball. */
const EXPAND_CAP = 70
/** Show every label when ≤ this many nodes are on screen … */
const LABEL_LIMIT = 26
/** … or once the user has zoomed in past this level (below it, only priority labels show). */
const LABEL_ZOOM = 1.35

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

/** cytoscape layout options per mode — all built-in (no dagre dep): cose / breadthfirst / concentric.
 *  Run against the *visible* sub-graph (not all ~300 nodes), so they stay fast and legible. */
function layoutFor(mode: LayoutMode): cytoscape.LayoutOptions {
  if (mode === 'tree')
    return { name: 'breadthfirst', directed: true, padding: 24, spacingFactor: 1.15, animate: false } as cytoscape.LayoutOptions
  if (mode === 'radial')
    return {
      name: 'concentric', padding: 24, minNodeSpacing: 26, animate: false,
      concentric: (n: NodeSingular) => n.degree(false),
      levelWidth: () => 2,
    } as cytoscape.LayoutOptions
  return { name: 'cose', animate: false, nodeRepulsion: 9000, idealEdgeLength: 95, padding: 24 } as cytoscape.LayoutOptions
}

/** Level-of-detail labels: hide the bulk of labels when the view is crowded and the user hasn't
 *  zoomed in — but always keep hubs, selection, search hits, focus and the hovered node labelled. */
function applyLod(cy: Core, count: number) {
  const hideBulk = count > LABEL_LIMIT && cy.zoom() < LABEL_ZOOM
  cy.batch(() => {
    cy.nodes().forEach((n) => {
      const priority = n.hasClass('hub') || n.hasClass('sel') || n.hasClass('hit')
        || n.hasClass('hovered') || n.hasClass('hl')
      n.toggleClass('lod-hide', hideBulk && !priority)
    })
  })
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
  const sigRef = useRef('')                              // last laid-out (layout|visible-node) signature
  const visibleCountRef = useRef(0)
  const renderLodRef = useRef<() => void>(() => {})

  const [minConf, setMinConf] = useState(0)
  const [activePath, setActivePath] = useState<number | null>(null)
  const [layout, setLayout] = useState<LayoutMode>('force')
  const [view, setView] = useState<ViewMode>('focus')
  const [search, setSearch] = useState('')
  const [hidden, setHidden] = useState<Set<string>>(new Set())      // hidden node types
  const [hiddenRels, setHiddenRels] = useState<Set<string>>(new Set()) // hidden relation categories
  const [expanded, setExpanded] = useState<Set<string>>(new Set())  // hub ids whose neighbourhood is shown
  const [focusId, setFocusId] = useState<string | null>(null)       // a focused (drilled-in) node
  const [selected, setSelected] = useState<GraphNode | null>(null)

  // ---- derived model -------------------------------------------------------------------------
  const index = useMemo<GraphIndex>(() => buildIndex(data), [data])
  const indexRef = useRef(index)
  useEffect(() => { indexRef.current = index }, [index])

  const elements = useMemo<ElementDefinition[]>(() => {
    if (!data) return []
    // Stamp the entry-view (backbone) classes up front so the FIRST paint is already the legible
    // hubs-only view — never a flash of all ~300 nodes before the render effect collapses them.
    const inBackbone = (id: string) => index.hubs.has(id) || index.managers.has(id) || index.connectors.has(id)
    const nodes = data.nodes.map((n) => {
      const hub = index.hubs.has(n.id)
      return {
        data: { id: n.id, label: n.label, type: n.type, props: n.props ?? {}, hub },
        classes: [hub ? 'hub' : '', inBackbone(n.id) ? '' : 'collapsed'].filter(Boolean).join(' '),
      }
    })
    const edges = data.edges.map((e) => {
      const cat = relationCategory(e.type)
      const shown = inBackbone(e.source) && inBackbone(e.target)
      return {
        data: { id: e.id, source: e.source, target: e.target, label: e.type, conf: e.confidence, cat: cat.key, catColor: cat.color },
        classes: shown ? '' : 'collapsed',
      }
    })
    return [...nodes, ...edges]
  }, [data, index])

  // Per-type counts (node-type legend + filter).
  const typeCounts = useMemo(() => {
    const m = new Map<string, number>()
    for (const n of data?.nodes ?? []) m.set(n.type, (m.get(n.type) ?? 0) + 1)
    return [...m.entries()].sort((a, b) => b[1] - a[1])
  }, [data])

  // Per-relation-category counts (relationship legend + filter).
  const relCounts = useMemo(() => {
    const m = new Map<string, number>()
    for (const e of data?.edges ?? []) { const k = relationCategory(e.type).key; m.set(k, (m.get(k) ?? 0) + 1) }
    return m
  }, [data])

  // Search matches over the WHOLE graph (name / type / CIK / ticker / ISIN / LEI …), not just what's drawn.
  const searchMatchIds = useMemo(() => {
    const q = search.trim().toLowerCase()
    if (!q || !data) return [] as string[]
    return data.nodes.filter((n) => nodeMatches(n, q)).map((n) => n.id)
  }, [search, data])
  const searchHitSet = useMemo(() => new Set(searchMatchIds), [searchMatchIds])

  // Nodes we explicitly highlight (a focused node / a navigated node / a named path).
  const hlSet = useMemo(() => {
    const s = new Set<string>()
    if (focusId) s.add(focusId)
    if (focus?.node_id) s.add(focus.node_id)
    if (activePath !== null && data?.paths?.[activePath]) for (const id of data.paths[activePath].node_ids) s.add(id)
    return s
  }, [focusId, focus, activePath, data])

  // The full set of "attention" nodes (highlights + search hits) that drive reveal + dimming.
  const focusSet = useMemo(() => {
    const s = new Set(hlSet)
    for (const id of searchHitSet) s.add(id)
    return s
  }, [hlSet, searchHitSet])

  // Which nodes make up the current view (backbone + expanded/focused neighbourhoods; null ⇒ all).
  const reveal = useMemo(
    () => computeReveal(index, { full: view === 'full', expanded, focus: focusSet, cap: EXPAND_CAP }),
    [index, view, expanded, focusSet],
  )

  // When something is focused/searched, everything outside that neighbourhood dims (bright set).
  const emphasisIds = useMemo(() => {
    if (focusSet.size === 0) return null
    const s = new Set<string>()
    for (const id of focusSet) { s.add(id); for (const nb of index.neighbors.get(id) ?? []) s.add(nb) }
    return s
  }, [focusSet, index])

  const isVisibleId = useCallback((id: string, type: string) =>
    (reveal.ids === null || reveal.ids.has(id)) && !hidden.has(type), [reveal, hidden])

  const visibleCount = useMemo(() => {
    if (!data) return 0
    if (reveal.ids === null) return data.nodes.filter((n) => !hidden.has(n.type)).length
    let c = 0
    for (const id of reveal.ids) { const n = index.byId.get(id); if (n && !hidden.has(n.type)) c++ }
    return c
  }, [reveal, data, hidden, index])

  // ---- minimap: draw visible node dots + a viewport rectangle into a small canvas ----
  const drawMini = useCallback(() => {
    const cv = miniRef.current
    const cy = cyRef.current
    if (!cv || !cy || cy.nodes().length === 0) return
    const ctx = cv.getContext('2d')
    if (!ctx) return
    const W = cv.width, H = cv.height, pad = 6
    ctx.clearRect(0, 0, W, H)
    const shown = cy.nodes().filter((n) => !n.hasClass('collapsed'))
    if (shown.length === 0) return
    const bb = shown.boundingBox()
    const gw = Math.max(bb.w, 1), gh = Math.max(bb.h, 1)
    const s = Math.min((W - 2 * pad) / gw, (H - 2 * pad) / gh)
    const ox = pad + ((W - 2 * pad) - gw * s) / 2
    const oy = pad + ((H - 2 * pad) - gh * s) / 2
    miniXf.current = { ox, oy, s, x1: bb.x1, y1: bb.y1 }
    const mx = (x: number) => ox + (x - bb.x1) * s
    const my = (y: number) => oy + (y - bb.y1) * s
    shown.forEach((n) => {
      const p = n.position()
      ctx.fillStyle = entityColor(n.data('type'))
      ctx.beginPath()
      ctx.arc(mx(p.x), my(p.y), n.hasClass('hub') ? 2.6 : 1.7, 0, 2 * Math.PI)
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
            'text-valign': 'bottom', 'text-margin-y': 3, 'text-max-width': '110px', 'text-wrap': 'ellipsis',
            'text-outline-width': 2, 'text-outline-color': c.surface, 'text-outline-opacity': 1,
            'min-zoomed-font-size': 8, 'text-opacity': 1,
            width: 18, height: 18, 'border-width': 2, 'border-color': c.surface,
          },
        },
        {
          selector: 'node.hub',
          style: {
            width: 34, height: 34, 'border-width': 3, 'border-color': c.accent,
            'font-size': '12px', 'font-weight': 700, 'text-max-width': '130px',
          },
        },
        {
          selector: 'edge',
          style: {
            width: 1.4, 'line-color': 'data(catColor)', 'target-arrow-color': 'data(catColor)',
            'target-arrow-shape': 'triangle', 'arrow-scale': 0.85, 'curve-style': 'bezier', opacity: 0.7,
            label: 'data(label)', 'font-size': '8px', color: c.muted, 'text-opacity': 0,
            'text-rotation': 'autorotate', 'text-background-color': c.surface, 'text-background-opacity': 0.9,
            'text-background-padding': '2px', 'min-zoomed-font-size': 7,
          },
        },
        { selector: 'node.lod-hide', style: { 'text-opacity': 0 } },
        { selector: 'node.hovered', style: { 'text-opacity': 1, 'border-color': c.accent, 'z-index': 30 } },
        { selector: 'edge.hovered-edge', style: { 'text-opacity': 1, width: 2.6, opacity: 1, 'z-index': 20 } },
        { selector: 'node.hl', style: { 'text-opacity': 1, 'border-color': c.accent, 'border-width': 3, 'z-index': 25 } },
        { selector: 'edge.hl', style: { 'text-opacity': 1, width: 3, opacity: 1, 'z-index': 22 } },
        { selector: 'node.hit', style: { 'border-color': c.accent, 'border-width': 4, 'text-opacity': 1, 'z-index': 26 } },
        { selector: 'node.sel', style: { 'border-color': c.accent, 'border-width': 4, width: 24, height: 24, 'text-opacity': 1, 'z-index': 40 } },
        { selector: 'node.faded', style: { opacity: 0.12, 'text-opacity': 0 } },
        { selector: 'edge.faded', style: { opacity: 0.05, 'text-opacity': 0 } },
        { selector: '.collapsed', style: { display: 'none' } },
      ],
      layout: { name: 'grid' },   // cheap mount layout; the render effect immediately re-lays out the visible view
      wheelSensitivity: 0.2, minZoom: 0.15, maxZoom: 3,
    })
    cyRef.current = cy
    sigRef.current = ''

    cy.on('tap', 'node', (evt) => {
      const n = evt.target as NodeSingular
      const id = n.id()
      setSelected({ id, type: n.data('type'), label: n.data('label'), props: n.data('props') })
      if (indexRef.current.hubs.has(id)) {
        // A hub: expand / collapse its neighbourhood (no dimming — you're surveying, not tracing).
        setFocusId(null)
        setExpanded((prev) => { const next = new Set(prev); if (next.has(id)) next.delete(id); else next.add(id); return next })
      } else {
        // A leaf: focus it — reveal + highlight its immediate neighbourhood, dim the rest.
        setFocusId(id)
      }
    })
    cy.on('tap', (evt) => { if (evt.target === cy) { setSelected(null); setFocusId(null) } })

    cy.on('mouseover', 'node', (evt) => {
      const n = evt.target as NodeSingular
      if (n.hasClass('collapsed')) return
      n.addClass('hovered')
      n.connectedEdges().filter((e) => !e.hasClass('collapsed')).addClass('hovered-edge')
      n.neighborhood().nodes().filter((m) => !m.hasClass('collapsed')).addClass('hovered')
    })
    cy.on('mouseout', 'node', () => {
      cy.batch(() => cy.elements().removeClass('hovered hovered-edge'))
      renderLodRef.current()
    })

    const applyLodNow = () => applyLod(cy, visibleCountRef.current)
    renderLodRef.current = applyLodNow

    let raf = 0
    const schedule = () => { if (raf) return; raf = requestAnimationFrame(() => { raf = 0; drawMini(); applyLodNow() }) }
    cy.on('pan zoom resize', schedule)
    cy.on('position', 'node', schedule)
    cy.on('layoutstop', schedule)
    cy.ready(() => drawMini())

    return () => { cancelAnimationFrame(raf); cy.destroy(); cyRef.current = null }
  }, [elements, themeKey, drawMini])

  // ---- core render: visibility (collapse), FIBO edge/relation filtering, focus dimming, layout, camera ----
  useEffect(() => {
    const cy = cyRef.current
    if (!cy) return
    const emph = emphasisIds
    const visSet = new Set<string>()

    cy.batch(() => {
      cy.nodes().forEach((n) => {
        const id = n.id(), type = n.data('type') as string
        const vis = isVisibleId(id, type)
        n.toggleClass('collapsed', !vis)
        n.toggleClass('hub', index.hubs.has(id))
        if (!vis) { n.removeClass('faded hl hit'); return }
        visSet.add(id)
        n.toggleClass('faded', !!emph && !emph.has(id))
        n.toggleClass('hl', hlSet.has(id))
        n.toggleClass('hit', searchHitSet.has(id))
      })
      cy.edges().forEach((e) => {
        const s = e.source(), t = e.target()
        const shown = visSet.has(s.id()) && visSet.has(t.id())
          && !hiddenRels.has(e.data('cat') as string)
          && (e.data('conf') as number) >= minConf
        e.toggleClass('collapsed', !shown)
        if (!shown) { e.removeClass('faded hl hovered-edge'); return }
        const touches = focusSet.has(s.id()) || focusSet.has(t.id())
        e.toggleClass('hl', !!emph && touches)
        e.toggleClass('faded', !!emph && !touches)
      })
    })

    visibleCountRef.current = visibleCount
    applyLod(cy, visibleCount)

    // Re-lay-out only when the visible node-set or the layout mode actually changed.
    const sig = layout + '|' + [...visSet].sort().join(',')
    let didRelayout = false
    if (sig !== sigRef.current) {
      sigRef.current = sig
      const visNodes = cy.nodes().filter((n) => visSet.has(n.id()))
      if (visNodes.nonempty()) {
        const eles = visNodes.union(visNodes.edgesWith(visNodes).filter((e) => !e.hasClass('collapsed')))
        eles.layout(layoutFor(layout)).run()
        didRelayout = true
      }
    }

    // Camera: frame the focused/searched node together with its neighbourhood, else fit the fresh
    // view after a re-layout.
    if (emph) {
      const cam = cy.nodes().filter((n) => visSet.has(n.id()) && emph.has(n.id()))
      if (cam.nonempty()) cy.animate({ fit: { eles: cam, padding: 70 }, duration: 280 })
    } else if (didRelayout) {
      const visNodes = cy.nodes().filter((n) => visSet.has(n.id()))
      if (visNodes.nonempty()) cy.animate({ fit: { eles: visNodes, padding: 36 }, duration: 280 })
    }
    drawMini()
  }, [index, reveal, emphasisIds, focusSet, hlSet, searchHitSet, hidden, hiddenRels, minConf, layout,
      visibleCount, isVisibleId, elements, drawMini])

  // keep the selected node's ring in sync (and make sure its label wins over LOD)
  useEffect(() => {
    const cy = cyRef.current
    if (!cy) return
    cy.nodes().removeClass('sel')
    if (selected) cy.getElementById(selected.id).addClass('sel')
    applyLod(cy, visibleCountRef.current)
  }, [selected, elements])

  // A navigation focus (from another panel) opens the inspector + focuses that node.
  useEffect(() => {
    if (!focus?.node_id) return
    const n = index.byId.get(focus.node_id)
    if (n) { setFocusId(n.id); setSelected({ id: n.id, type: n.type, label: n.label, props: n.props }) }
  }, [focus, index])

  const zoomBy = (f: number) => { const cy = cyRef.current; if (cy) cy.zoom({ level: cy.zoom() * f, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } }) }
  const fit = () => {
    const cy = cyRef.current
    if (!cy) return
    const shown = cy.nodes().filter((n) => !n.hasClass('collapsed'))
    cy.animate({ fit: { eles: shown.nonempty() ? shown : cy.elements(), padding: 36 }, duration: 250 })
  }
  const resetView = () => {
    setActivePath(null); setSearch(''); setHidden(new Set()); setHiddenRels(new Set())
    setExpanded(new Set()); setFocusId(null); setSelected(null); setView('focus')
  }

  const toggleType = (t: string) => setHidden((prev) => { const next = new Set(prev); next.has(t) ? next.delete(t) : next.add(t); return next })
  const toggleRel = (k: string) => setHiddenRels((prev) => { const next = new Set(prev); next.has(k) ? next.delete(k) : next.add(k); return next })

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

  const relLegend = RELATION_CATEGORIES.filter((c) => (relCounts.get(c.key) ?? 0) > 0)
  const hasNodes = (data?.nodes.length ?? 0) > 0
  const entryView = view === 'focus' && !focusId && !search && expanded.size === 0 && activePath === null
  const focusNode = focusId ? index.byId.get(focusId) : null
  const expandedLabel = expanded.size === 1
    ? (index.byId.get([...expanded][0])?.label ?? '1 expanded')
    : `${expanded.size} expanded`

  return (
    <div>
      <PanelHead
        title="Graph Explorer"
        sub="Start from the funds, expand into a holding's neighbourhood, and trace FIBO-typed relationships. Search jumps to any entity by name, CIK, ticker or ISIN; click a node for its FIBO OWL grounding, extracted attributes, adjacent triples and lakehouse provenance — or query the reasoned TBox with SPARQL."
        right={source && <SourceBadge source={source} />}
      />

      <div className="graph-toolbar">
        <div className="graph-search">
          <Icon name="search" size={14} />
          <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search name, type, CIK, ticker, ISIN…" />
          {search && <button className="btn btn-ghost btn-sm" onClick={() => setSearch('')} aria-label="clear"><Icon name="close" size={13} /></button>}
        </div>
        <Segmented<ViewMode>
          value={view}
          onChange={setView}
          options={[{ value: 'focus', label: 'Focus' }, { value: 'full', label: 'Full graph' }]}
        />
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

          {/* Contextual focus bar: what you're looking at + how to get back */}
          {(focusNode || search || (view === 'focus' && expanded.size > 0) || reveal.capped.length > 0) && (
            <div className="gx-focusbar">
              {focusNode && (
                <button className="gx-chip accent gx-chip-btn" onClick={() => { setFocusId(null); setSelected(null) }} title="Clear focus">
                  <Icon name="graph" size={12} /><span className="gx-chip-name">{focusNode.label}</span><span className="gx-x"><Icon name="close" size={11} /></span>
                </button>
              )}
              {search && (
                <button className="gx-chip gx-chip-btn" onClick={() => setSearch('')} title="Clear search">
                  <Icon name="search" size={12} /><span className="gx-chip-name">{searchMatchIds.length} match{searchMatchIds.length === 1 ? '' : 'es'}</span><span className="gx-x"><Icon name="close" size={11} /></span>
                </button>
              )}
              {view === 'focus' && !focusNode && expanded.size > 0 && (
                <button className="gx-chip gx-chip-btn" onClick={() => setExpanded(new Set())} title="Collapse all">
                  <Icon name="merge" size={12} /><span className="gx-chip-name">{expandedLabel}</span><span className="gx-x"><Icon name="close" size={11} /></span>
                </button>
              )}
              {reveal.capped.length > 0 && (
                <span className="gx-chip" title="Deep neighbourhoods are capped for legibility — search to reach any specific node.">
                  <Icon name="info" size={12} />
                  <span className="gx-chip-name">
                    {reveal.capped.length === 1
                      ? `Top ${reveal.capped[0].shown} of ${reveal.capped[0].total} — search for more`
                      : `Top ${EXPAND_CAP} per hub — search for more`}
                  </span>
                </span>
              )}
            </div>
          )}

          {hasNodes && (
            <canvas ref={miniRef} width={148} height={104} className="graph-minimap" onClick={onMiniClick} title="Overview — click to pan" />
          )}
          {loading && <div className="loading" style={{ position: 'absolute', inset: 0 }}><span className="spinner" />Loading graph…</div>}
          {!loading && !hasNodes && (
            <div style={{ position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 8, textAlign: 'center', padding: 24, color: 'var(--text-muted)' }}>
              <Icon name="graph" size={22} />
              <div style={{ fontWeight: 600 }}>No subgraph for {firm ?? 'this firm'} yet</div>
              <div className="faint" style={{ fontSize: 12, maxWidth: 340, lineHeight: 1.5 }}>
                This firm's funds and holdings appear here once its graph is populated — run enrichment to add filing-derived nodes and edges.
              </div>
            </div>
          )}
          {hasNodes && entryView && index.hubs.size > 0 && (
            <div className="gx-emptyhint"><Icon name="graph" size={13} /> Click a fund to expand its holdings, or search to jump to any entity</div>
          )}
          {hasNodes && <div className="graph-hint">scroll to zoom · click a fund to expand · click a node to focus · hover for relations</div>}
        </div>

        <div className="graph-side">
          {selected && <NodeInspector node={selected} adjacency={adjacency} onClose={() => setSelected(null)} />}

          <div className="card card-pad">
            <div className="gx-card-head">
              <strong>Node types</strong>
              {hidden.size > 0 && <button className="btn btn-ghost btn-sm" onClick={() => setHidden(new Set())}>show all</button>}
            </div>
            <p className="gx-card-sub">Coloured by FIBO class. Click a type to hide it.</p>
            <div className="gx-legend">
              {typeCounts.map(([t, n]) => {
                const off = hidden.has(t)
                const fibo = fiboClassOf(t)
                return (
                  <button key={t} className={`gx-legend-row ${off ? 'off' : ''}`} onClick={() => toggleType(t)} title={off ? 'Show' : 'Hide'}>
                    <span className="gx-legend-swatch" style={{ background: entityColor(t) }} />
                    <span className="gx-legend-txt">
                      <span className="gx-legend-name">{fibo.label}</span>
                      <span className="gx-legend-curie">{fibo.curie || t}</span>
                    </span>
                    <span className="gx-legend-count">{n}</span>
                  </button>
                )
              })}
            </div>
          </div>

          {relLegend.length > 0 && (
            <div className="card card-pad">
              <div className="gx-card-head">
                <strong>Relationships</strong>
                {hiddenRels.size > 0 && <button className="btn btn-ghost btn-sm" onClick={() => setHiddenRels(new Set())}>show all</button>}
              </div>
              <p className="gx-card-sub">FIBO-aligned edge categories. Labels show on hover or focus; click to hide a category.</p>
              <div className="gx-legend">
                {relLegend.map((c) => {
                  const off = hiddenRels.has(c.key)
                  return (
                    <button key={c.key} className={`gx-legend-row ${off ? 'off' : ''}`} onClick={() => toggleRel(c.key)} title={off ? 'Show' : 'Hide'}>
                      <span className="gx-legend-line" style={{ color: c.color }} />
                      <span className="gx-legend-txt">
                        <span className="gx-legend-name">{c.label}</span>
                        <span className="gx-legend-curie">{c.fibo}</span>
                      </span>
                      <span className="gx-legend-count">{relCounts.get(c.key)}</span>
                    </button>
                  )
                })}
              </div>
            </div>
          )}

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
