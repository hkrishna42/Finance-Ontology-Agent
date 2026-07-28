import { getImpact } from '../api'
import type { FactTriple, ImpactBriefing } from '../types'
import { useLoaded } from '../lib/useLoaded'
import { PanelHead, SourceBadge } from '../lib/ui'
import { Icon } from '../lib/icons'
import type { NavTarget } from '../App'

const fmtDate = (s: string) => new Date(s).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })

export function ImpactFeed({ onNavigate }: { onNavigate: (t: NavTarget) => void }) {
  const { data, source, loading } = useLoaded<ImpactBriefing[]>(getImpact)

  return (
    <div>
      <PanelHead
        title="Change Impact Feed"
        sub="When a steward write changes a fact, the propagation engine (hop-limit 2, declarative rules) finds the downstream impact set — the funds affected and the disclosure sections rendered stale. Every hop is cited."
        right={source && <SourceBadge source={source} />}
      />
      {loading ? (
        <div className="loading"><span className="spinner" />Loading briefings…</div>
      ) : (
        <div className="stack">
          {(data ?? []).map((b) => (
            <div key={b.id} className="card card-pad">
              <div className="row" style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
                <div className="row" style={{ gap: 10 }}>
                  <div className="brand-mark" style={{ width: 26, height: 26, borderRadius: 7, background: 'linear-gradient(135deg,#e8590c,#f08c00)' }}><Icon name="impact" size={14} /></div>
                  <div>
                    <strong style={{ fontSize: 14 }}>{b.trigger_title}</strong>
                    <div className="faint" style={{ fontSize: 12 }}>{fmtDate(b.created_at)} · rule <span className="mono">{b.rule}</span></div>
                  </div>
                </div>
                <button className="btn btn-ghost btn-sm" onClick={() => onNavigate({ panel: 'docs', focus: { doc_id: b.trigger_doc_id } })}>Trigger doc <Icon name="arrow" size={13} /></button>
              </div>

              <p style={{ fontSize: 13.5, lineHeight: 1.6, margin: '12px 0' }}>{b.summary}</p>

              <div className="grid grid-2">
                <div>
                  <SectionLabel>Fact changes</SectionLabel>
                  <div style={{ marginTop: 6 }}>
                    {b.added.map((t, i) => <Triple key={`a${i}`} t={t} kind="added" />)}
                    {b.changed.map((t, i) => <Triple key={`c${i}`} t={t} kind="changed" />)}
                    {b.removed.map((t, i) => <Triple key={`r${i}`} t={t} kind="removed" />)}
                    {b.added.length + b.changed.length + b.removed.length === 0 && <span className="faint" style={{ fontSize: 12 }}>—</span>}
                  </div>
                </div>
                <div>
                  <SectionLabel>Affected funds &amp; sections</SectionLabel>
                  <div style={{ marginTop: 6 }} className="stack">
                    <div className="wrap-gap">
                      {b.affected_funds.map((f) => (
                        <span key={f.fund} className="chip" title={f.reason}>
                          <Icon name="risk" size={12} />{f.fund.replace('Demo ', '')} <span className="faint">· {f.hops} hop{f.hops > 1 ? 's' : ''}</span>
                        </span>
                      ))}
                    </div>
                    {b.stale_sections.length > 0 && (
                      <div className="stack" style={{ gap: 6 }}>
                        {b.stale_sections.map((s) => (
                          <div key={s.form_ref + s.item} className="row" style={{ gap: 8, fontSize: 12.5 }}>
                            <span className="stale-badge">stale</span>
                            <span><strong>{s.title}</strong> <span className="faint mono">{s.form_ref}</span></span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return <div className="faint" style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.04em', fontWeight: 600 }}>{children}</div>
}

function Triple({ t, kind }: { t: FactTriple; kind: 'added' | 'changed' | 'removed' }) {
  const color = kind === 'added' ? 'var(--good)' : kind === 'removed' ? 'var(--bad)' : 'var(--warn)'
  const sign = kind === 'added' ? '+' : kind === 'removed' ? '−' : '±'
  return (
    <div className="triple">
      <span style={{ color, fontWeight: 700, width: 12 }}>{sign}</span>
      <span className="ent">{t.subject}</span>
      <span className="rel-tag">{t.predicate}</span>
      <span className="ent">{t.object}</span>
      {t.detail && <span className="faint" style={{ fontSize: 11.5 }}>— {t.detail}</span>}
    </div>
  )
}
