import { useState } from 'react'
import { runQuery } from '../api'
import type { Loaded } from '../api'
import type { Citation, QueryMode, QueryResponse } from '../types'
import type { NavTarget } from '../App'
import { Cypher, PanelHead, PathViz, Segmented, SourceBadge, Switch } from '../lib/ui'
import { Icon } from '../lib/icons'

const SUGGESTIONS = [
  'Which funds are most exposed to a TSMC manufacturing disruption?',
  'Do any board members sit on the boards of two companies we hold?',
  "Is the funds' supply-chain / foundry risk disclosed in their prospectus?",
  "What is driving NVIDIA's customer-concentration risk right now?",
]

export function ChatPanel({ onNavigate }: { onNavigate: (t: NavTarget) => void }) {
  const [question, setQuestion] = useState('')
  const [mode, setMode] = useState<QueryMode>('side_by_side')
  const [wall, setWall] = useState(true)
  const [result, setResult] = useState<Loaded<QueryResponse> | null>(null)
  const [busy, setBusy] = useState(false)

  const ask = async (q?: string, m: QueryMode = mode, w: boolean = wall) => {
    const text = (q ?? question).trim()
    if (!text) return
    setQuestion(text)
    setBusy(true)
    const res = await runQuery(text, m, w)
    setResult(res)
    setBusy(false)
  }

  // Re-run automatically when the toggles change and we already have a result
  // (pass the new value explicitly — component state has not settled yet).
  const onMode = (m: QueryMode) => { setMode(m); if (result) void ask(result.data.question, m, wall) }
  const onWall = (w: boolean) => { setWall(w); if (result) void ask(result.data.question, mode, w) }

  const openCitation = (c: Citation) =>
    onNavigate({ panel: 'docs', focus: { doc_id: c.doc_id, chunk_id: c.chunk_id } })

  const r = result?.data

  return (
    <div>
      <PanelHead
        title="Analyst Chat"
        sub="Ask a question in natural language. The graph answer is grounded in cited spans and a reviewable Cypher plan; compare it against a plain vector-RAG baseline, and toggle the internal-information wall."
        right={result && <SourceBadge source={result.source} />}
      />

      <div className="card card-pad">
        <div className="chat-input-row">
          <textarea
            className="chat-input"
            rows={2}
            placeholder="e.g. Which funds are most exposed to a TSMC disruption?"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) void ask() }}
          />
          <button className="btn btn-primary" disabled={busy || !question.trim()} onClick={() => void ask()}>
            {busy ? <><span className="spinner" />Thinking</> : <><Icon name="arrow" />Ask</>}
          </button>
        </div>

        <div className="chat-controls">
          <div className="control-group">
            <span className="control-label">Mode</span>
            <Segmented
              value={mode}
              onChange={onMode}
              options={[{ value: 'graph', label: 'Graph answer' }, { value: 'side_by_side', label: 'Side-by-side' }]}
            />
          </div>
          <div className="control-group">
            <Icon name="lock" size={14} />
            <Switch checked={wall} onChange={onWall} label={wall ? 'Internal wall ON' : 'Internal wall OFF'} />
          </div>
        </div>

        {!result && (
          <div className="suggestions">
            {SUGGESTIONS.map((s) => (
              <button key={s} className="suggestion" onClick={() => void ask(s)}>{s}</button>
            ))}
          </div>
        )}
      </div>

      {r && (
        <div className="stack" style={{ marginTop: 16 }}>
          {r.withheld_count > 0 && (
            <div className="pill warn" style={{ alignSelf: 'flex-start' }}>
              <Icon name="lock" size={13} /> {r.withheld_count} source{r.withheld_count > 1 ? 's' : ''} withheld — internal-information wall is ON
            </div>
          )}

          {mode === 'side_by_side' ? (
            <div className="sbs">
              <div className="sbs-col vector">
                <div className="sbs-col-head"><Icon name="search" size={14} /> Vector RAG (baseline)</div>
                <div className="sbs-col-body">
                  <p style={{ marginBottom: 10 }}>{r.vector_answer ?? 'No baseline answer.'}</p>
                  <div className="faint" style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.04em', fontWeight: 600, marginBottom: 6 }}>Retrieved fragments</div>
                  {(r.vector_fragments ?? []).map((f) => (
                    <div className="frag" key={f.chunk_id}>
                      <div className="frag-meta">
                        <span>{f.title ?? f.doc_id}</span>
                        <span className="score">{f.score.toFixed(2)}</span>
                      </div>
                      <div>{f.text}</div>
                    </div>
                  ))}
                  <div className="faint" style={{ fontSize: 12, marginTop: 8 }}>Flat fragments — no aggregation across holdings, no multi-hop path, no fund attribution.</div>
                </div>
              </div>

              <div className="sbs-col graph">
                <div className="sbs-col-head"><Icon name="graph" size={14} /> Graph answer</div>
                <div className="sbs-col-body">
                  <AnswerBody r={r} onOpenCitation={openCitation} onNavigate={onNavigate} />
                </div>
              </div>
            </div>
          ) : (
            <div className="card card-pad">
              <AnswerBody r={r} onOpenCitation={openCitation} onNavigate={onNavigate} />
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function AnswerBody({ r, onOpenCitation, onNavigate }: {
  r: QueryResponse
  onOpenCitation: (c: Citation) => void
  onNavigate: (t: NavTarget) => void
}) {
  return (
    <div>
      <div className="answer-box">{r.answer}</div>

      {r.graph_paths.length > 0 && (
        <>
          <div className="divider" />
          <div className="row" style={{ justifyContent: 'space-between' }}>
            <div className="faint" style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.04em', fontWeight: 600 }}>Reasoning path</div>
            <button className="btn btn-ghost btn-sm" onClick={() => onNavigate({ panel: 'graph' })}>Open in graph explorer <Icon name="arrow" size={13} /></button>
          </div>
          <div className="path-viz" style={{ marginTop: 8 }}>
            {r.graph_paths.map((p, i) => <PathViz key={i} path={p} highlight />)}
          </div>
        </>
      )}

      {r.citations.length > 0 && (
        <>
          <div className="divider" />
          <div className="faint" style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.04em', fontWeight: 600, marginBottom: 8 }}>Citations ({r.citations.length})</div>
          <div className="cite-list">
            {r.citations.map((c, i) => (
              <div className="cite-item" key={c.id} onClick={() => onOpenCitation(c)}>
                <div className="row" style={{ justifyContent: 'space-between', marginBottom: 4 }}>
                  <span style={{ fontWeight: 600, fontSize: 12.5 }}>
                    <span className="citation">{i + 1}</span> {c.title ?? c.doc_id}
                    {c.page && <span className="faint"> · p.{c.page}</span>}
                  </span>
                  <span className="row" style={{ gap: 6 }}>
                    {c.sensitivity === 'internal' && <span className="pill bad" style={{ fontSize: 10, padding: '1px 6px' }}>internal</span>}
                    <Icon name="external" size={13} />
                  </span>
                </div>
                <div className="cite-span">“{c.span}”</div>
              </div>
            ))}
          </div>
        </>
      )}

      {r.plan && (
        <>
          <div className="divider" />
          <details>
            <summary style={{ cursor: 'pointer', fontSize: 12.5, fontWeight: 600, color: 'var(--text-muted)' }}>Query plan &amp; Cypher</summary>
            <div style={{ marginTop: 10 }}>
              <div className="faint" style={{ fontSize: 12, marginBottom: 6 }}>{r.plan.strategy}</div>
              <ol style={{ margin: '0 0 12px', paddingLeft: 18, fontSize: 12.5, color: 'var(--text-muted)', lineHeight: 1.7 }}>
                {r.plan.steps.map((s, i) => <li key={i}>{s}</li>)}
              </ol>
              {r.cypher && <Cypher query={r.cypher} />}
            </div>
          </details>
        </>
      )}
    </div>
  )
}
