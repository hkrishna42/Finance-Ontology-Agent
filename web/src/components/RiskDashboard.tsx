import { useMemo, useState } from 'react'
import { Bar, BarChart, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { getRisk } from '../api'
import type { ExplainRef, RiskData } from '../types'
import { useLoaded } from '../lib/useLoaded'
import { Cypher, Drawer, PanelHead, PathViz, pct, SourceBadge, usd } from '../lib/ui'
import { Icon } from '../lib/icons'

const SEV = ['transparent', 'rgba(232,89,12,0.22)', 'rgba(232,89,12,0.55)', 'rgba(201,42,42,0.82)']
const prettyCat = (c: string) => c.replace(/_/g, ' ')

export function RiskDashboard() {
  const { data, source, loading } = useLoaded<RiskData>(getRisk)
  const [fund, setFund] = useState(0)
  const [explain, setExplain] = useState<{ title: string; ref: ExplainRef } | null>(null)

  const funds = useMemo(() => (data ? [...new Set(data.concentration.map((c) => c.fund))] : []), [data])
  const activeFund = funds[fund]
  const holdings = useMemo(
    () => (data ? data.concentration.filter((c) => c.fund === activeFund).sort((a, b) => b.weight_pct - a.weight_pct) : []),
    [data, activeFund],
  )

  if (loading) return <div className="loading"><span className="spinner" />Computing risk…</div>
  if (!data) return null

  return (
    <div>
      <PanelHead
        title="Risk Dashboard"
        sub="Concentration, Herfindahl-Hirschman Index (HHI), a risk-factor exposure heatmap, and single-source supply flags. Every number has an 'explain' drawer with the exact read-only Cypher and the edges it traverses."
        right={source && <SourceBadge source={source} />}
      />

      {/* KPI row */}
      <div className="grid grid-4" style={{ marginBottom: 18 }}>
        {data.hhi.map((h) => (
          <div className="stat" key={h.fund}>
            <div className="stat-label">HHI · {h.fund.replace('Demo ', '')}</div>
            <div className="stat-value">{h.hhi} <small>top {pct(h.top_weight_pct)}</small></div>
            <div className="stat-foot">{h.interpretation.split('.')[0]}.</div>
          </div>
        ))}
        <div className="stat">
          <div className="stat-label">Single-source flags</div>
          <div className="stat-value" style={{ color: 'var(--bad)' }}>{data.single_source.length}</div>
          <div className="stat-foot">critical suppliers held issuers depend on</div>
        </div>
        <div className="stat">
          <div className="stat-label">Top shared supplier</div>
          <div className="stat-value" style={{ fontSize: 18 }}>{data.single_source[0]?.supplier ?? '—'}</div>
          <div className="stat-foot">{data.single_source[0] ? `${pct(data.single_source[0].aggregate_weight_pct)} of assets (worst fund)` : ''}</div>
        </div>
      </div>

      <div className="grid grid-2">
        {/* Concentration chart + table */}
        <div className="card">
          <div className="card-head" style={{ justifyContent: 'space-between' }}>
            <div><h3>Concentration by issuer</h3><div className="card-head-sub">disclosed holdings, N-PORT as of 2026-05-31</div></div>
            <div className="segmented">
              {funds.map((f, i) => (
                <button key={f} className={i === fund ? 'active' : ''} onClick={() => setFund(i)}>{f.replace('Demo ', '').replace(' Fund', '')}</button>
              ))}
            </div>
          </div>
          <div className="card-pad">
            <ResponsiveContainer width="100%" height={Math.max(200, holdings.length * 30)}>
              <BarChart data={holdings} layout="vertical" margin={{ left: 8, right: 24, top: 4, bottom: 4 }}>
                <XAxis type="number" domain={[0, 'dataMax']} unit="%" tick={{ fontSize: 11 }} />
                <YAxis type="category" dataKey="ticker" width={52} tick={{ fontSize: 11 }} />
                <Tooltip
                  cursor={{ fill: 'var(--accent-weak)' }}
                  contentStyle={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 8, fontSize: 12, color: 'var(--text)' }}
                  formatter={(v, _n, p) => {
                    const pl = (p as { payload?: { value_usd: number; issuer: string } }).payload
                    return [`${v}%  ·  ${pl ? usd(pl.value_usd) : ''}`, pl?.issuer ?? '']
                  }}
                />
                <Bar dataKey="weight_pct" radius={[0, 4, 4, 0]}>
                  {holdings.map((h, i) => <Cell key={i} fill={h.weight_pct >= 10 ? 'var(--bad)' : h.weight_pct >= 7 ? 'var(--warn)' : 'var(--accent)'} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>

        {/* HHI explain */}
        <div className="card">
          <div className="card-head"><h3>Herfindahl-Hirschman Index</h3><span className="card-head-sub">Σ(weightᵢ²) over holdings</span></div>
          <div className="card-pad stack">
            {data.hhi.map((h) => (
              <div key={h.fund} className="res-item" style={{ padding: 12 }}>
                <div className="row" style={{ justifyContent: 'space-between' }}>
                  <strong style={{ fontSize: 13 }}>{h.fund}</strong>
                  <span className="stat-value" style={{ fontSize: 20 }}>{h.hhi}</span>
                </div>
                <p className="faint" style={{ fontSize: 12.5, margin: '6px 0 10px' }}>{h.interpretation}</p>
                {h.explain && (
                  <button className="btn btn-sm" onClick={() => setExplain({ title: `HHI — ${h.fund}`, ref: h.explain! })}>
                    <Icon name="search" size={13} />Explain this number
                  </button>
                )}
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Heatmap */}
      <div className="card" style={{ marginTop: 16 }}>
        <div className="card-head"><h3>Risk-factor exposure heatmap</h3><span className="card-head-sub">held issuers × RiskCategory · darker = stronger disclosed exposure</span></div>
        <div className="card-pad">
          <div className="table-wrap">
            <div className="heatmap" style={{ gridTemplateColumns: `180px repeat(${data.heatmap.categories.length}, minmax(74px, 1fr))`, minWidth: 640 }}>
              <div />
              {data.heatmap.categories.map((c) => <div key={c} className="hm-label-x">{prettyCat(c)}</div>)}
              {data.heatmap.companies.map((co) => (
                <HeatRow key={co} co={co} data={data} />
              ))}
            </div>
          </div>
          <div className="row" style={{ gap: 14, marginTop: 12 }}>
            {['none', 'low', 'elevated', 'high'].map((l, i) => (
              <span key={l} className="row" style={{ gap: 6, fontSize: 11.5 }}>
                <span style={{ width: 14, height: 14, borderRadius: 3, background: i === 0 ? 'var(--surface-2)' : SEV[i], border: '1px solid var(--border)' }} />
                <span className="faint">{l}</span>
              </span>
            ))}
          </div>
        </div>
      </div>

      {/* Single-source flags */}
      <div className="card" style={{ marginTop: 16 }}>
        <div className="card-head"><h3>Single-source supply flags</h3><span className="card-head-sub">one supplier that many held issuers critically depend on</span></div>
        <div className="card-pad stack">
          {data.single_source.map((s) => (
            <div key={s.supplier} className="res-item">
              <div className="row" style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
                <div className="row" style={{ gap: 8 }}>
                  <Icon name="alert" size={16} />
                  <strong style={{ fontSize: 14 }}>{s.supplier}</strong>
                  <span className="pill bad" style={{ fontSize: 10, padding: '1px 6px' }}>{s.criticality}</span>
                </div>
                <span className="pill warn"><Icon name="risk" size={12} />{pct(s.aggregate_weight_pct)} of assets exposed</span>
              </div>
              <div className="wrap-gap" style={{ margin: '10px 0' }}>
                {s.dependents.map((d) => <span key={d} className="chip">{d}</span>)}
              </div>
              <div className="row" style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
                <span className="faint" style={{ fontSize: 12 }}>Exposed funds: {s.exposed_funds.map((f) => f.replace('Demo ', '')).join(', ')}</span>
                {s.explain && (
                  <button className="btn btn-sm" onClick={() => setExplain({ title: `Single-source — ${s.supplier}`, ref: s.explain! })}>
                    <Icon name="search" size={13} />Explain this number
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>

      <Drawer open={!!explain} title={explain?.title ?? ''} sub="Reviewable read-only Cypher and the edges it traverses" onClose={() => setExplain(null)}>
        {explain && (
          <div className="stack">
            {explain.ref.note && <div className="pill accent" style={{ alignSelf: 'flex-start' }}><Icon name="info" size={13} />{explain.ref.note}</div>}
            <div>
              <div className="faint" style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.04em', fontWeight: 600, marginBottom: 6 }}>Cypher</div>
              <Cypher query={explain.ref.cypher} />
            </div>
            <div>
              <div className="faint" style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.04em', fontWeight: 600, marginBottom: 8 }}>Edges traversed</div>
              <div className="path-viz">
                {explain.ref.edges.map((e, i) => <PathViz key={i} path={{ steps: [e] }} highlight />)}
              </div>
            </div>
          </div>
        )}
      </Drawer>
    </div>
  )
}

function HeatRow({ co, data }: { co: string; data: RiskData }) {
  return (
    <>
      <div className="hm-label-y">{co}</div>
      {data.heatmap.categories.map((cat) => {
        const cell = data.heatmap.cells.find((x) => x.company === co && x.category === cat)
        const sev = cell?.severity ?? 0
        return (
          <div
            key={cat}
            className="hm-cell"
            style={{ background: sev === 0 ? 'var(--surface-2)' : SEV[sev], color: sev >= 2 ? '#fff' : 'var(--text-muted)' }}
            title={cell?.severity_language ? `${co} · ${prettyCat(cat)}: “${cell.severity_language}”` : `${co} · ${prettyCat(cat)}: no disclosed exposure`}
          >
            {sev > 0 ? sev : ''}
          </div>
        )
      })}
    </>
  )
}
