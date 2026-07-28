import { useEffect, useMemo, useRef, useState } from 'react'
import cytoscape from 'cytoscape'
import type { Core, ElementDefinition } from 'cytoscape'
import { getGraph } from '../api'
import type { GraphData } from '../types'
import { useLoaded } from '../lib/useLoaded'
import { ENTITY_COLORS, entityColor, PanelHead, SourceBadge } from '../lib/ui'
import { Icon } from '../lib/icons'
import type { NavTarget } from '../App'

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

export function GraphExplorer({ focus, themeKey, firm }: { focus?: NavTarget['focus']; themeKey: string; firm?: string | null }) {
  const { data, source, loading } = useLoaded<GraphData>(() => getGraph(firm ?? undefined), [firm])
  const boxRef = useRef<HTMLDivElement>(null)
  const cyRef = useRef<Core | null>(null)
  const [minConf, setMinConf] = useState(0)
  const [activePath, setActivePath] = useState<number | null>(null)

  const elements = useMemo<ElementDefinition[]>(() => {
    if (!data) return []
    const nodes = data.nodes.map((n) => ({ data: { id: n.id, label: n.label, type: n.type } }))
    const edges = data.edges.map((e) => ({
      data: { id: e.id, source: e.source, target: e.target, label: e.type, conf: e.confidence },
    }))
    return [...nodes, ...edges]
  }, [data])

  // Build / rebuild the cytoscape instance when data or theme changes.
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
            'background-color': (ele: cytoscape.NodeSingular) => entityColor(ele.data('type')),
            label: 'data(label)',
            color: c.text,
            'font-size': '9px',
            'font-weight': 600,
            'text-valign': 'bottom',
            'text-margin-y': 4,
            'text-max-width': '90px',
            'text-wrap': 'ellipsis',
            width: 22,
            height: 22,
            'border-width': 2,
            'border-color': c.surface,
          },
        },
        {
          selector: 'edge',
          style: {
            width: 1.4,
            'line-color': c.border,
            'target-arrow-color': c.border,
            'target-arrow-shape': 'triangle',
            'arrow-scale': 0.8,
            'curve-style': 'bezier',
            label: 'data(label)',
            'font-size': '7px',
            color: c.muted,
            'text-rotation': 'autorotate',
            'text-background-color': c.surface,
            'text-background-opacity': 0.85,
            'text-background-padding': '1px',
          },
        },
        { selector: 'node.hl', style: { 'border-color': c.accent, 'border-width': 3, width: 28, height: 28, 'font-size': '11px', color: c.text } },
        { selector: 'edge.hl', style: { 'line-color': c.accent, 'target-arrow-color': c.accent, width: 3, color: c.accent, 'font-size': '9px', 'font-weight': 700 } },
        { selector: '.dim', style: { display: 'none' } },
        { selector: '.faded', style: { opacity: 0.12 } },
      ],
      layout: { name: 'cose', animate: false, nodeRepulsion: 9000, idealEdgeLength: 90, padding: 24 } as cytoscape.LayoutOptions,
      wheelSensitivity: 0.2,
      minZoom: 0.3,
      maxZoom: 2.5,
    })
    cyRef.current = cy
    return () => { cy.destroy(); cyRef.current = null }
  }, [elements, themeKey])

  // Confidence filter: hide edges below threshold, fade nodes left with no visible edge.
  useEffect(() => {
    const cy = cyRef.current
    if (!cy) return
    cy.batch(() => {
      cy.edges().forEach((e) => {
        const hidden = (e.data('conf') as number) < minConf
        e.toggleClass('dim', hidden)
      })
      cy.nodes().forEach((n) => {
        const visibleDeg = n.connectedEdges().filter((e) => !e.hasClass('dim')).length
        n.toggleClass('faded', visibleDeg === 0)
      })
    })
  }, [minConf, elements])

  // Highlight a named path (or a focused node from navigation).
  useEffect(() => {
    const cy = cyRef.current
    if (!cy || !data) return
    cy.batch(() => {
      cy.elements().removeClass('hl faded-path')
      cy.elements().removeClass('faded')
      let ids: string[] | null = null
      if (activePath !== null && data.paths?.[activePath]) ids = data.paths[activePath].node_ids
      else if (focus?.node_id) ids = [focus.node_id]
      if (ids) {
        const set = new Set(ids)
        cy.nodes().forEach((n) => { if (set.has(n.id())) n.addClass('hl'); else n.addClass('faded') })
        cy.edges().forEach((e) => {
          if (set.has(e.source().id()) && set.has(e.target().id())) e.addClass('hl')
          else e.addClass('faded')
        })
        const hl = cy.$('.hl')
        if (hl.length) cy.animate({ fit: { eles: hl, padding: 80 }, duration: 300 })
      }
    })
    // Re-apply confidence fade after path clears
    if (activePath === null && !focus?.node_id) {
      cy.nodes().forEach((n) => {
        const visibleDeg = n.connectedEdges().filter((e) => !e.hasClass('dim')).length
        n.toggleClass('faded', visibleDeg === 0)
      })
    }
  }, [activePath, focus, data])

  const resetView = () => { setActivePath(null); const cy = cyRef.current; if (cy) cy.animate({ fit: { eles: cy.elements(), padding: 24 }, duration: 300 }) }

  const usedTypes = useMemo(() => {
    if (!data) return []
    const set = new Set(data.nodes.map((n) => n.type))
    return Object.keys(ENTITY_COLORS).filter((t) => set.has(t as never))
  }, [data])

  return (
    <div>
      <PanelHead
        title="Graph Explorer"
        sub="The ontology-consistent knowledge subgraph. Filter edges by extraction confidence, and highlight the hero-query paths (shared critical supplier, second-order chokepoint, board interlock)."
        right={source && <SourceBadge source={source} />}
      />
      <div className="graph-layout">
        <div className="graph-canvas">
          <div ref={boxRef} style={{ width: '100%', height: '100%' }} />
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
          {(data?.nodes.length ?? 0) > 0 && <div className="graph-hint">scroll to zoom · drag to pan · drag nodes to reposition</div>}
        </div>

        <div className="graph-side">
          <div className="card card-pad">
            <div className="row" style={{ justifyContent: 'space-between', marginBottom: 10 }}>
              <strong style={{ fontSize: 13 }}>Confidence filter</strong>
              <span className="slider-val">{minConf.toFixed(2)}</span>
            </div>
            <div className="slider-row">
              <input type="range" min={0} max={1} step={0.01} value={minConf} onChange={(e) => setMinConf(Number(e.target.value))} />
            </div>
            <p className="faint" style={{ fontSize: 11.5, marginTop: 8 }}>
              Hides edges below the threshold. Structural edges (HOLDS, COVERS…) are confidence 1.0; the lowest semantic edge is FORMERLY_AT at 0.85.
            </p>
          </div>

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

          <div className="card card-pad">
            <strong style={{ fontSize: 13 }}>Entity types</strong>
            <div style={{ marginTop: 8 }}>
              {usedTypes.map((t) => (
                <div className="legend-item" key={t}>
                  <span className="legend-dot" style={{ background: entityColor(t) }} />{t}
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
