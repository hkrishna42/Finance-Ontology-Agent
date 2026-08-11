import { useEffect, useState } from 'react'
import { getResolutionQueue, queueIdOf, resolveMerge, resolvePromote, resolveReject } from '../api'
import type { ProvisionalEntity, ResolutionCandidate } from '../types'
import { useLoaded } from '../lib/useLoaded'
import { EntityTag, PanelHead, SourceBadge } from '../lib/ui'
import { Icon } from '../lib/icons'

export function ResolutionQueue() {
  const { data, source, loading } = useLoaded<ProvisionalEntity[]>(getResolutionQueue)
  const [items, setItems] = useState<ProvisionalEntity[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  useEffect(() => { if (data) setItems(data) }, [data])

  const setStatus = (id: string, status: ProvisionalEntity['status']) =>
    setItems((prev) => prev.map((p) => (p.id === id ? { ...p, status } : p)))

  // Apply a steward decision: update the UI optimistically, then persist for live queue rows
  // (demo-fixture rows have no backend id, so they stay local — the panel is still demoable offline).
  const decide = async (
    p: ProvisionalEntity, status: ProvisionalEntity['status'], candidate?: ResolutionCandidate,
  ) => {
    setStatus(p.id, status)
    const qid = queueIdOf(p.id)
    if (qid === null) return
    setBusy(p.id)
    try {
      if (status === 'merged' && candidate) {
        await resolveMerge(qid, { canonical_key: candidate.existing_id, canonical_label: candidate.label })
      } else if (status === 'kept_new') {
        await resolvePromote(qid)
      } else if (status === 'rejected') {
        await resolveReject(qid)
      }
    } finally {
      setBusy((b) => (b === p.id ? null : b))
    }
  }

  const pending = items.filter((p) => (p.status ?? 'pending') === 'pending').length

  return (
    <div>
      <PanelHead
        title="Resolution Queue"
        sub="Provisional entities the resolver could not confidently merge. A steward confirms the canonical match (or keeps it as a new node). Merges are the human-in-the-loop gate before facts join the graph."
        right={
          <div className="row" style={{ gap: 8 }}>
            <span className="pill accent">{pending} pending</span>
            {source && <SourceBadge source={source} />}
          </div>
        }
      />
      {loading ? (
        <div className="loading"><span className="spinner" />Loading queue…</div>
      ) : items.length === 0 ? (
        <div className="card card-pad" style={{ textAlign: 'center', padding: '40px 24px' }}>
          <div className="brand-mark" style={{ width: 34, height: 34, borderRadius: 9, margin: '0 auto 12px', background: 'linear-gradient(135deg,#2f9e44,#37b24d)' }}><Icon name="check" size={18} /></div>
          <strong style={{ fontSize: 14 }}>Resolution queue is clear</strong>
          <p className="faint" style={{ fontSize: 13, lineHeight: 1.6, maxWidth: 440, margin: '8px auto 0' }}>
            Every extracted mention has been confidently merged to a canonical entity. New provisional
            entities appear here whenever ingestion finds a mention the resolver can’t auto-merge.
          </p>
        </div>
      ) : (
        <div className="stack">
          {items.map((p) => {
            const status = p.status ?? 'pending'
            // Confidence is a real 0..1 score or null/0 (no score). Render a % only when meaningful —
            // never the "0.00" artifact that a null/placeholder score used to produce.
            const conf = typeof p.confidence === 'number' && p.confidence > 0 ? Math.round(p.confidence * 100) : null
            // Span / doc may be empty strings from the live resolver — guard so we never render the
            // stray “” · artifact; only show the italic quote / mono doc-id when actually present.
            const span = p.span?.trim() ? p.span.trim() : null
            const docId = p.doc_id?.trim() ? p.doc_id.trim() : null
            return (
              <div key={p.id} className={`res-item ${status !== 'pending' ? 'merged' : ''}`}>
                <div className="row" style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
                  <div className="row" style={{ gap: 10, minWidth: 0, flexWrap: 'wrap' }}>
                    <EntityTag type={p.label} />
                    <strong style={{ fontSize: 15 }}>{p.name}</strong>
                    {p.aliases.length > 0 && <span className="faint" style={{ fontSize: 12 }}>aka {p.aliases.join(', ')}</span>}
                  </div>
                  {conf !== null
                    ? <span className="pill" style={{ fontSize: 11 }}>conf {conf}%</span>
                    : <span className="pill" style={{ fontSize: 11 }} title="The resolver has no confidence score for this mention">no score</span>}
                </div>

                {(span || docId) && (
                  <div className="faint" style={{ fontSize: 12.5, margin: '8px 0 12px' }}>
                    {span && <span style={{ fontStyle: 'italic' }}>“{span}”</span>}
                    {docId && <span className="mono">{span ? ' · ' : ''}{docId}</span>}
                  </div>
                )}
                {!span && !docId && <div style={{ height: 8 }} />}

                {status !== 'pending' ? (
                  <div className={`pill ${status === 'merged' ? 'good' : ''}`}>
                    <Icon name={status === 'merged' ? 'merge' : status === 'rejected' ? 'close' : 'check'} size={13} />
                    {status === 'merged'
                      ? 'Merged to canonical entity'
                      : status === 'rejected'
                        ? 'Rejected — discarded'
                        : 'Kept as a new node'}
                    <button className="btn btn-ghost btn-sm" style={{ marginLeft: 8 }} onClick={() => setStatus(p.id, 'pending')}>undo</button>
                  </div>
                ) : (
                  <div className="stack" style={{ gap: 8 }}>
                    {p.candidates.length === 0 && (
                      <div className="faint" style={{ fontSize: 12.5, display: 'flex', alignItems: 'center', gap: 7 }}>
                        <Icon name="info" size={13} />
                        No candidate matches — the resolver suggests keeping this as a new canonical node.
                      </div>
                    )}
                    {p.candidates.map((c) => (
                      <div className="candidate" key={c.existing_id}>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
                            <EntityTag type={c.label} />
                            <strong style={{ fontSize: 13 }}>{c.name}</strong>
                            <span className="mono faint" style={{ fontSize: 11 }}>{c.existing_id}</span>
                          </div>
                          {c.reason && <div className="faint" style={{ fontSize: 11.5, marginTop: 3 }}>{c.reason}</div>}
                        </div>
                        <div className="confbar" style={{ maxWidth: 90 }}><span style={{ width: `${Math.round(c.score * 100)}%` }} /></div>
                        <span className="score" style={{ fontSize: 12, minWidth: 40, textAlign: 'right' }}>{Math.round(c.score * 100)}%</span>
                        <button className="btn btn-primary btn-sm" disabled={busy === p.id} onClick={() => decide(p, 'merged', c)}>
                          <Icon name="merge" size={13} />Merge
                        </button>
                      </div>
                    ))}
                    <div className="row" style={{ gap: 8 }}>
                      <button className="btn btn-sm" disabled={busy === p.id} onClick={() => decide(p, 'kept_new')}>Keep as new node</button>
                      <button className="btn btn-ghost btn-sm" disabled={busy === p.id} onClick={() => decide(p, 'rejected')}>
                        <Icon name="close" size={13} />Reject
                      </button>
                    </div>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
