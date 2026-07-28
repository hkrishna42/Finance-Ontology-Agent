import { useEffect, useMemo, useRef, useState } from 'react'
import { getDocuments } from '../api'
import type { DocRecord, DocSpan } from '../types'
import { useLoaded } from '../lib/useLoaded'
import { PanelHead, SourceBadge } from '../lib/ui'
import { Icon } from '../lib/icons'
import type { NavTarget } from '../App'

interface Range { start: number; end: number; span: DocSpan }

/** Locate each span's verbatim quote in the text and build non-overlapping highlight ranges. */
function buildRanges(text: string, spans: DocSpan[]): Range[] {
  const ranges: Range[] = []
  for (const span of spans) {
    const idx = text.indexOf(span.quote)
    if (idx >= 0) ranges.push({ start: idx, end: idx + span.quote.length, span })
  }
  ranges.sort((a, b) => a.start - b.start)
  // Drop overlaps (keep first).
  const out: Range[] = []
  let cursor = -1
  for (const r of ranges) {
    if (r.start >= cursor) { out.push(r); cursor = r.end }
  }
  return out
}

export function DocViewer({ focus, firm }: { focus?: NavTarget['focus']; firm?: string | null }) {
  const { data, source, loading } = useLoaded<DocRecord[]>(() => getDocuments(firm ?? undefined), [firm])
  const [activeId, setActiveId] = useState<string | null>(null)
  const activeSpanRef = useRef<HTMLElement>(null)

  const docs = useMemo(() => data ?? [], [data])
  const selected = useMemo(() => docs.find((d) => d.doc_id === (activeId ?? focus?.doc_id)) ?? docs[0], [docs, activeId, focus])

  // When navigated here from a citation, select that doc.
  useEffect(() => { if (focus?.doc_id) setActiveId(focus.doc_id) }, [focus])
  // Scroll the focused span into view once rendered.
  useEffect(() => { activeSpanRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' }) }, [selected, focus])

  const ranges = useMemo(() => (selected ? buildRanges(selected.text, selected.spans) : []), [selected])

  const rendered = useMemo(() => {
    if (!selected) return null
    const nodes: React.ReactNode[] = []
    let pos = 0
    ranges.forEach((r, i) => {
      if (r.start > pos) nodes.push(<span key={`t${i}`}>{selected.text.slice(pos, r.start)}</span>)
      const isActive = focus?.chunk_id === r.span.chunk_id
      const cls = `hl${r.span.sensitivity === 'internal' ? ' internal' : ''}${isActive ? ' active' : ''}`
      nodes.push(
        <mark key={`m${i}`} className={cls} title={r.span.label} ref={isActive ? activeSpanRef : undefined}>
          {selected.text.slice(r.start, r.end)}
        </mark>,
      )
      pos = r.end
    })
    if (pos < selected.text.length) nodes.push(<span key="tail">{selected.text.slice(pos)}</span>)
    return nodes
  }, [selected, ranges, focus])

  return (
    <div>
      <PanelHead
        title="Document Viewer"
        sub="Source filings with each grounding span highlighted — the verbatim sentence that supports an extracted fact. Internal-sensitivity spans are flagged; the grounding gate drops any span not found in the source text."
        right={source && <SourceBadge source={source} />}
      />
      {loading ? (
        <div className="loading"><span className="spinner" />Loading documents…</div>
      ) : docs.length === 0 ? (
        <div className="card card-pad" style={{ textAlign: 'center', padding: '48px 24px', color: 'var(--text-muted)' }}>
          <Icon name="doc" size={26} />
          <div style={{ fontWeight: 600, marginTop: 10 }}>No filings ingested for {firm ?? 'this firm'} yet — run enrichment.</div>
          <div className="faint" style={{ fontSize: 12.5, marginTop: 6, maxWidth: 420, marginLeft: 'auto', marginRight: 'auto', lineHeight: 1.5 }}>
            Once this firm's filings are ingested, each source document appears here with its grounding spans highlighted.
          </div>
        </div>
      ) : (
        <div className="doc-layout">
          <div className="doc-list">
            {docs.map((d) => (
              <div key={d.doc_id} className={`doc-list-item ${selected?.doc_id === d.doc_id ? 'active' : ''}`} onClick={() => setActiveId(d.doc_id)}>
                <div className="dli-title">{d.title}</div>
                <div className="row" style={{ gap: 6, marginTop: 5 }}>
                  <span className="tag">{d.doc_type}</span>
                  {d.sensitivity === 'internal'
                    ? <span className="pill bad" style={{ fontSize: 10, padding: '1px 6px' }}><Icon name="lock" size={11} />internal</span>
                    : <span className="pill" style={{ fontSize: 10, padding: '1px 6px' }}>public</span>}
                </div>
              </div>
            ))}
          </div>

          <div className="card">
            {selected && (
              <>
                <div className="card-head" style={{ justifyContent: 'space-between' }}>
                  <div>
                    <h3>{selected.title}</h3>
                    <div className="card-head-sub">{selected.doc_type} · {selected.filing_date ?? '—'} · {selected.spans.length} grounded span{selected.spans.length !== 1 ? 's' : ''}</div>
                  </div>
                  {selected.url && <a className="btn btn-ghost btn-sm" href={selected.url} target="_blank" rel="noreferrer">EDGAR <Icon name="external" size={13} /></a>}
                </div>
                <div className="card-pad">
                  <div className="doc-text">{rendered}</div>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
