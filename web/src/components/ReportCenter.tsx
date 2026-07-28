import { useEffect, useState } from 'react'
import { getReports } from '../api'
import type { ReportPack } from '../types'
import { useLoaded } from '../lib/useLoaded'
import { Cypher, PanelHead, SourceBadge } from '../lib/ui'
import { Icon } from '../lib/icons'
import type { NavTarget } from '../App'

const fmtDate = (s: string) => new Date(s).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })

export function ReportCenter({ onNavigate }: { onNavigate: (t: NavTarget) => void }) {
  const { data, source, loading } = useLoaded<ReportPack[]>(getReports)
  const [openId, setOpenId] = useState<string | null>(null)
  useEffect(() => { if (data && data.length && !openId) setOpenId(data[0].report_id) }, [data, openId])

  const reports = data ?? []
  const open = reports.find((r) => r.report_id === openId)

  return (
    <div>
      <PanelHead
        title="Report Center"
        sub="Generated report packs with an immutable provenance appendix — every claim links to the source span or the Cypher that produced it, and the pack carries a content hash for audit."
        right={source && <SourceBadge source={source} />}
      />
      {loading ? (
        <div className="loading"><span className="spinner" />Loading reports…</div>
      ) : (
        <div className="doc-layout">
          <div className="doc-list">
            {reports.map((r) => (
              <div key={r.report_id} className={`doc-list-item ${openId === r.report_id ? 'active' : ''}`} onClick={() => setOpenId(r.report_id)}>
                <div className="dli-title">{r.title}</div>
                <div className="row" style={{ gap: 6, marginTop: 5 }}>
                  <span className="tag">{r.period}</span>
                  <StatusPill status={r.status} />
                </div>
              </div>
            ))}
          </div>

          <div className="card">
            {open && (
              <>
                <div className="card-head" style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
                  <div>
                    <h3>{open.title}</h3>
                    <div className="card-head-sub">{open.period} · generated {fmtDate(open.created_at)}</div>
                  </div>
                  <StatusPill status={open.status} />
                </div>
                <div className="card-pad stack">
                  {open.status === 'out_of_date' && (
                    <div className="pill warn" style={{ alignSelf: 'flex-start' }}><Icon name="alert" size={13} />A cited fact changed since generation — see the Change Impact feed</div>
                  )}

                  {open.sections.map((s, i) => (
                    <div key={i}>
                      <div className="row" style={{ gap: 8 }}>
                        <h4 style={{ fontSize: 13.5 }}>{s.heading}</h4>
                        {s.stale && <span className="stale-badge">stale</span>}
                      </div>
                      <p style={{ fontSize: 13.5, lineHeight: 1.65, marginTop: 4, color: 'var(--text)' }}>{s.body}</p>
                    </div>
                  ))}

                  <div className="divider" />
                  <div className="faint" style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.04em', fontWeight: 600 }}>Provenance appendix</div>
                  <div className="stack" style={{ gap: 8 }}>
                    {open.provenance.map((p, i) => (
                      <div key={i} className="res-item" style={{ padding: 12 }}>
                        <div className="row" style={{ justifyContent: 'space-between', gap: 8, flexWrap: 'wrap' }}>
                          <strong style={{ fontSize: 12.5 }}>{p.claim}</strong>
                          <button className="btn btn-ghost btn-sm" onClick={() => onNavigate({ panel: 'docs', focus: { doc_id: p.doc_id, chunk_id: p.chunk_id } })}>
                            {p.doc_id} <Icon name="arrow" size={12} />
                          </button>
                        </div>
                        {p.span && <div className="cite-span" style={{ fontSize: 12, marginTop: 5 }}>“{p.span}”</div>}
                        {p.cypher && <div style={{ marginTop: 6 }}><Cypher query={p.cypher} /></div>}
                      </div>
                    ))}
                  </div>

                  <div className="divider" />
                  <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
                    <span className="tag"><Icon name="lock" size={12} />sha256</span>
                    <span className="hash">{open.sha256}</span>
                  </div>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function StatusPill({ status }: { status: ReportPack['status'] }) {
  const map = { final: 'good', draft: '', out_of_date: 'warn' } as const
  const label = { final: 'final', draft: 'draft', out_of_date: 'out of date' }[status]
  return <span className={`pill ${map[status]}`} style={{ fontSize: 10.5, padding: '1px 7px' }}>{label}</span>
}
