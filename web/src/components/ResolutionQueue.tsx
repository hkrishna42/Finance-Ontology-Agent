import { useEffect, useState } from 'react'
import { getResolutionQueue } from '../api'
import type { ProvisionalEntity } from '../types'
import { useLoaded } from '../lib/useLoaded'
import { EntityTag, PanelHead, SourceBadge } from '../lib/ui'
import { Icon } from '../lib/icons'

export function ResolutionQueue() {
  const { data, source, loading } = useLoaded<ProvisionalEntity[]>(getResolutionQueue)
  const [items, setItems] = useState<ProvisionalEntity[]>([])
  useEffect(() => { if (data) setItems(data) }, [data])

  const setStatus = (id: string, status: ProvisionalEntity['status']) =>
    setItems((prev) => prev.map((p) => (p.id === id ? { ...p, status } : p)))

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
      ) : (
        <div className="stack">
          {items.map((p) => {
            const status = p.status ?? 'pending'
            return (
              <div key={p.id} className={`res-item ${status !== 'pending' ? 'merged' : ''}`}>
                <div className="row" style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
                  <div className="row" style={{ gap: 10 }}>
                    <EntityTag type={p.label} />
                    <strong style={{ fontSize: 15 }}>{p.name}</strong>
                    {p.aliases.length > 0 && <span className="faint" style={{ fontSize: 12 }}>aka {p.aliases.join(', ')}</span>}
                  </div>
                  <span className="pill" style={{ fontSize: 11 }}>conf {p.confidence.toFixed(2)}</span>
                </div>

                <div className="faint" style={{ fontSize: 12.5, margin: '8px 0 12px', fontStyle: 'italic' }}>
                  “{p.span}” <span className="mono" style={{ fontStyle: 'normal' }}>· {p.doc_id}</span>
                </div>

                {status !== 'pending' ? (
                  <div className={`pill ${status === 'merged' ? 'good' : ''}`}>
                    <Icon name={status === 'merged' ? 'merge' : 'check'} size={13} />
                    {status === 'merged' ? 'Merged to canonical entity' : 'Kept as a new node'}
                    <button className="btn btn-ghost btn-sm" style={{ marginLeft: 8 }} onClick={() => setStatus(p.id, 'pending')}>undo</button>
                  </div>
                ) : (
                  <div className="stack" style={{ gap: 8 }}>
                    {p.candidates.length === 0 && <div className="faint" style={{ fontSize: 12.5 }}>No candidate matches — resolver suggests creating a new node.</div>}
                    {p.candidates.map((c) => (
                      <div className="candidate" key={c.existing_id}>
                        <div style={{ flex: 1 }}>
                          <div className="row" style={{ gap: 8 }}>
                            <EntityTag type={c.label} />
                            <strong style={{ fontSize: 13 }}>{c.name}</strong>
                            <span className="mono faint" style={{ fontSize: 11 }}>{c.existing_id}</span>
                          </div>
                          {c.reason && <div className="faint" style={{ fontSize: 11.5, marginTop: 3 }}>{c.reason}</div>}
                        </div>
                        <div className="confbar" style={{ maxWidth: 90 }}><span style={{ width: `${c.score * 100}%` }} /></div>
                        <span className="score" style={{ fontSize: 12, minWidth: 34, textAlign: 'right' }}>{c.score.toFixed(2)}</span>
                        <button className="btn btn-primary btn-sm" onClick={() => setStatus(p.id, 'merged')}>
                          <Icon name="merge" size={13} />Merge
                        </button>
                      </div>
                    ))}
                    <div>
                      <button className="btn btn-sm" onClick={() => setStatus(p.id, 'kept_new')}>Keep as new node</button>
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
