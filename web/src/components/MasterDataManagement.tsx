import { useEffect, useState } from 'react'
import { getMdmEntities, getMdmSources, postMdmMatch, postMdmMerge } from '../api'
import type { GoldenRecord, MdmEntity, MdmMatch, MdmSources, SurvivorshipDecision } from '../types'
import { PanelHead } from '../lib/ui'
import { Icon } from '../lib/icons'

const AGENTS = [
  ['Agent A · Retriever', 'OCR, PDF tables, numeric NER'],
  ['Agent B · Ontologist', 'FIBO RDF grounding & LEI resolution'],
  ['Agent C · Validator', 'Competency questions & quality gates'],
  ['Agent D · GraphRAG', 'Natural language → Cypher & grounding'],
]

export function MasterDataManagement() {
  const [entities, setEntities] = useState<MdmEntity[]>([])
  const [selected, setSelected] = useState<MdmEntity | null>(null)
  const [sources, setSources] = useState<MdmSources | null>(null)
  const [match, setMatch] = useState<MdmMatch | null>(null)
  const [merged, setMerged] = useState<{ golden_record: GoldenRecord; match: MdmMatch } | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)

  useEffect(() => { void getMdmEntities().then((e) => { setEntities(e); setLoading(false) }) }, [])

  const select = async (e: MdmEntity) => {
    setSelected(e); setSources(null); setMatch(null); setMerged(null)
    const [s, m] = await Promise.all([getMdmSources(e.entity_id), postMdmMatch(e.entity_id)])
    setSources(s); setMatch(m)
  }
  const runMerge = async () => {
    if (!selected) return
    setBusy(true)
    const r = await postMdmMerge(selected.entity_id)
    if (r) setMerged({ golden_record: r.golden_record, match: r.match })
    setBusy(false)
  }

  return (
    <div>
      <PanelHead
        title="Master Data Management"
        sub="Multi-source entity resolution → a single, auditable golden record. The FIBO ontology supplies the shared class and matching key; attribute-level survivorship rules reconcile conflicting source systems into one canonical record — the single source of truth."
      />

      <div className="grid grid-4" style={{ marginBottom: 20 }}>
        {AGENTS.map(([name, desc]) => (
          <div key={name} className="card card-pad" style={{ padding: '12px 14px' }}>
            <div className="row" style={{ justifyContent: 'space-between', gap: 8 }}>
              <strong style={{ fontSize: 12.5 }}>{name}</strong>
              <span className="pill good" style={{ fontSize: 10 }}>ready</span>
            </div>
            <div className="faint" style={{ fontSize: 11.5, marginTop: 4 }}>{desc}</div>
          </div>
        ))}
      </div>

      <Section n={1} title="Select master entity to resolve">
        {loading ? <div className="loading"><span className="spinner" />Loading entities…</div> : (
          entities.length === 0
            ? <div className="empty">No master entities in the lakehouse yet.</div>
            : (
              <div className="grid grid-3">
                {entities.map((e) => (
                  <button
                    key={e.entity_id}
                    className={`card card-pad mdm-entity ${selected?.entity_id === e.entity_id ? 'sel' : ''}`}
                    style={{ textAlign: 'left', cursor: 'pointer',
                             borderColor: selected?.entity_id === e.entity_id ? 'var(--accent)' : undefined }}
                    onClick={() => void select(e)}
                  >
                    <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 6 }}>{e.display_name}</div>
                    {e.fibo_curie && <div><FiboBadge curie={e.fibo_curie} /></div>}
                    <div className="faint" style={{ fontSize: 12, marginTop: 6 }}>
                      {e.n_sources} source systems · {e.n_attributes} attributes reconciled
                    </div>
                  </button>
                ))}
              </div>
            )
        )}
      </Section>

      {selected && sources && (
        <Section n={2} title={`Source-system records — ${sources.sources.length} systems ingested`}>
          <div className="table-wrap">
            <div className="grid" style={{ gridTemplateColumns: `repeat(${sources.sources.length}, minmax(210px, 1fr))`, gap: 12 }}>
              {sources.sources.map((s) => (
                <div key={s.system} className="card card-pad" style={{ padding: '12px 14px' }}>
                  <div className="row" style={{ justifyContent: 'space-between', gap: 6 }}>
                    <span className="faint" style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.04em' }}>{s.system_name}</span>
                    <span className={`pill ${(s.trust ?? 0) >= 90 ? 'good' : (s.trust ?? 0) >= 84 ? 'accent' : 'warn'}`} style={{ fontSize: 10 }}>Trust {s.trust}</span>
                  </div>
                  <div style={{ marginTop: 8, display: 'grid', gap: 5 }}>
                    {Object.entries(s.values).map(([k, v]) => (
                      <div key={k} style={{ fontSize: 12.5 }}>
                        <span className="faint">{k}:</span> <span className="mono">{v ?? '—'}</span>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </Section>
      )}

      {selected && match && (
        <Section n={3} title="Ontology-driven entity matching (blocking & scoring)">
          <div className="card card-pad" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr auto', gap: 18, alignItems: 'center' }}>
            <div>
              <SectionLabel>Matching key</SectionLabel>
              <div style={{ fontSize: 13.5, marginTop: 4 }}>{match.blocking_keys.join(' + ')}</div>
            </div>
            <div>
              <SectionLabel>Method</SectionLabel>
              <div className="faint" style={{ fontSize: 12.5, marginTop: 4 }}>{match.method}</div>
            </div>
            <div style={{ textAlign: 'center' }}>
              <SectionLabel>Match confidence</SectionLabel>
              <div style={{ fontSize: 28, fontWeight: 700, color: match.resolved ? 'var(--good)' : 'var(--warn)' }}>
                {match.confidence_pct}%
              </div>
            </div>
          </div>
          {match.resolved && (
            <div className="pill good" style={{ marginTop: 10 }}>
              <Icon name="check" size={13} />Resolved — all {match.matched_record_ids.length} source records confirmed as the same entity
            </div>
          )}
        </Section>
      )}

      {selected && !merged && (
        <div style={{ textAlign: 'center', margin: '20px 0 6px' }}>
          <button className="btn btn-primary" onClick={() => void runMerge()} disabled={busy || !match?.resolved}>
            {busy ? <><span className="spinner" />Running match &amp; merge…</> : <><Icon name="merge" size={14} />Run Match &amp; Merge</>}
          </button>
        </div>
      )}

      {merged && (
        <>
          <Section n={4} title="Attribute-level survivorship rules">
            <div className="table-wrap">
              <table className="tbl" style={{ minWidth: 720 }}>
                <thead>
                  <tr><th>Attribute</th><th>Survivorship rule</th><th>Winning source</th><th>Winning value</th><th>Rationale</th></tr>
                </thead>
                <tbody>
                  {merged.golden_record.per_attribute.map((d: SurvivorshipDecision) => (
                    <tr key={d.attribute}>
                      <td><strong>{d.attribute}</strong></td>
                      <td style={{ color: 'var(--accent)' }}>{d.rule}</td>
                      <td className="mono faint">{d.winning_source}</td>
                      <td><strong>{String(d.winning_value ?? '—')}</strong></td>
                      <td className="faint" style={{ fontSize: 12 }}>{d.rationale}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>

          <Section n={5} title="Published golden record">
            <GoldenCard golden={merged.golden_record} />
          </Section>
        </>
      )}
    </div>
  )
}

function GoldenCard({ golden }: { golden: GoldenRecord }) {
  return (
    <div className="card card-pad">
      <div className="row" style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
        <div className="row" style={{ gap: 10 }}>
          <div className="brand-mark" style={{ width: 26, height: 26, borderRadius: 7, background: 'linear-gradient(135deg,#2f9e44,#37b24d)' }}><Icon name="check" size={14} /></div>
          <div>
            <strong style={{ fontSize: 14 }}>{golden.pk}</strong>
            <div className="faint mono" style={{ fontSize: 12 }}>{golden.lakehouse_provenance}</div>
          </div>
        </div>
        {golden.fibo_curie && <FiboBadge curie={golden.fibo_curie} grounded />}
      </div>
      <div className="grid grid-2" style={{ marginTop: 12 }}>
        <div>
          <SectionLabel>Canonical attributes</SectionLabel>
          <div style={{ marginTop: 6, display: 'grid', gap: 4 }}>
            {Object.entries(golden.canonical_attributes).map(([k, v]) => (
              <div key={k} style={{ fontSize: 13 }}><span className="faint">{k}:</span> <strong>{String(v ?? '—')}</strong></div>
            ))}
          </div>
        </div>
        <div>
          <SectionLabel>Retained alternate identifiers</SectionLabel>
          <div style={{ marginTop: 6, display: 'grid', gap: 4 }}>
            {Object.keys(golden.retained_alternate_ids).length === 0
              ? <span className="faint" style={{ fontSize: 12 }}>—</span>
              : Object.entries(golden.retained_alternate_ids).map(([k, alts]) => (
                <div key={k} style={{ fontSize: 12.5 }}><span className="faint">{k}:</span> <span className="mono">{alts.join(', ')}</span></div>
              ))}
          </div>
        </div>
      </div>
    </div>
  )
}

function FiboBadge({ curie, grounded }: { curie: string; grounded?: boolean }) {
  return (
    <span className="pill" style={{ fontSize: 10.5, background: 'var(--good-bg)', color: 'var(--good)', marginTop: grounded ? 0 : 6, display: 'inline-flex' }} title="FIBO OWL class">
      {grounded ? 'FIBO GROUNDED · ' : ''}<span className="mono">{curie}</span>
    </span>
  )
}

function Section({ n, title, children }: { n: number; title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginTop: 20 }}>
      <div className="row" style={{ gap: 9, marginBottom: 10 }}>
        <span className="pill accent" style={{ width: 22, height: 22, justifyContent: 'center', padding: 0, borderRadius: 999 }}>{n}</span>
        <strong style={{ fontSize: 13, textTransform: 'uppercase', letterSpacing: '0.03em' }}>{title}</strong>
      </div>
      {children}
    </div>
  )
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return <div className="faint" style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.04em', fontWeight: 600 }}>{children}</div>
}
