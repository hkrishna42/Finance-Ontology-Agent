import { getEval } from '../api'
import type { EvalCard } from '../types'
import { useLoaded } from '../lib/useLoaded'
import { PanelHead, SourceBadge } from '../lib/ui'
import { Icon } from '../lib/icons'

export function EvalPanel() {
  const { data, source, loading } = useLoaded<EvalCard[]>(getEval)

  return (
    <div>
      <PanelHead
        title="Evaluation"
        sub="Offline quality gates — extraction precision/recall, grounding faithfulness, vector-vs-graph on the hero question set, and entitlement-wall leakage. Wired to the eval harness in M8; the cards below show the target metrics."
        right={source && <SourceBadge source={source} />}
      />
      {loading ? (
        <div className="loading"><span className="spinner" />Loading…</div>
      ) : (
        <div className="grid grid-2">
          {(data ?? []).map((c) => (
            <div key={c.id} className="card card-pad">
              <div className="row" style={{ justifyContent: 'space-between' }}>
                <h3 style={{ fontSize: 14 }}>{c.title}</h3>
                <span className={`pill ${c.status === 'wired' ? 'good' : ''}`} style={{ fontSize: 10.5 }}>
                  {c.status === 'wired' ? <><Icon name="check" size={12} />wired</> : 'M8 placeholder'}
                </span>
              </div>
              <p className="faint" style={{ fontSize: 12.5, margin: '6px 0 14px' }}>{c.description}</p>
              <div className="stack" style={{ gap: 10 }}>
                {c.metrics.map((m) => (
                  <div key={m.label}>
                    <div className="row" style={{ justifyContent: 'space-between', fontSize: 12.5 }}>
                      <span className="muted">{m.label}</span>
                      <span className="mono" style={{ fontWeight: 600 }}>
                        {m.value === null ? <span className="faint">pending</span> : `${m.value}${m.unit ?? ''}`}
                        {m.target !== undefined && <span className="faint" style={{ fontWeight: 400 }}> / target {m.target}{m.unit ?? ''}</span>}
                      </span>
                    </div>
                    <div className="confbar" style={{ marginTop: 5 }}>
                      <span style={{ width: m.value === null ? '0%' : `${Math.min(100, typeof m.target === 'number' && m.target > 0 ? (m.value / m.target) * 100 : m.value)}%`, background: m.value === null ? 'var(--border-strong)' : 'var(--accent)' }} />
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
