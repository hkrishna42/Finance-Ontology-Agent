import { useCallback, useEffect, useRef, useState } from 'react'
import { enrichFirm, getFirms, selectFirm } from '../api'
import type { Source } from '../api'
import type { Firm, SSEEvent } from '../types'
import { SourceBadge } from '../lib/ui'
import { Icon } from '../lib/icons'

// Header firm selector. Lists registered firms (name + fund count + demo badge), highlights the
// active one, and on selection POSTs /firms/{id}/select then asks App to refresh the panels
// (the server defaults every panel to the active firm). When /firms is unreachable the data layer
// serves the committed demo-firm fixture — we still render it, but selection is disabled.

export function FirmSelector({ onSelect, onAddFirm, reloadKey, scopeAll, onSelectAll }: {
  onSelect?: (firm: Firm) => void
  onAddFirm?: () => void
  reloadKey?: number
  scopeAll?: boolean            // "All data" (unscoped) is the current scope, not a registry firm
  onSelectAll?: () => void      // switch to the unscoped "All data" scope
}) {
  const [firms, setFirms] = useState<Firm[] | null>(null)
  const [source, setSource] = useState<Source | null>(null)
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  // Per-firm manual enrichment: which firm is streaming, plus a transient inline done/error note.
  const [enrichingId, setEnrichingId] = useState<string | null>(null)
  const [enrichNote, setEnrichNote] = useState<{ id: string; text: string; tone: 'good' | 'bad' } | null>(null)
  const noteTimer = useRef<number | null>(null)

  const load = useCallback(async () => {
    const res = await getFirms()
    setFirms(res.data)
    setSource(res.source)
  }, [])

  // Re-fetch on mount and whenever App bumps reloadKey (firm switch or a newly-onboarded firm).
  useEffect(() => { load() }, [load, reloadKey])

  // Close the menu on Escape for keyboard users.
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  // Drop the pending inline-note timer if the selector unmounts.
  useEffect(() => () => { if (noteTimer.current != null) clearTimeout(noteTimer.current) }, [])

  if (!firms || firms.length === 0) return null // nothing registered → hide gracefully

  const offline = source !== 'live'
  const active = firms.find((f) => f.is_active) ?? firms[0]

  const choose = async (f: Firm) => {
    if (offline || busy) return
    if (f.is_active) {
      // Already the registry's active firm: a no-op normally, but if we're in the unscoped "All data"
      // scope, re-selecting it switches the UI back to this firm.
      setOpen(false)
      if (scopeAll) onSelect?.(f)
      return
    }
    setBusy(f.firm_id)
    const updated = await selectFirm(f.firm_id)
    setBusy(null)
    setOpen(false)
    // Optimistically flip the active flag so the trigger + list update immediately.
    setFirms((prev) => (prev ? prev.map((x) => ({ ...x, is_active: x.firm_id === f.firm_id })) : prev))
    onSelect?.(updated ?? { ...f, is_active: true })
  }

  // Manually (re-)enrich one firm's top holdings. Streams the same pipeline the onboard flow can run
  // automatically; keeps the menu open and shows a spinner on the row, then a transient inline note.
  const runEnrich = async (f: Firm) => {
    if (offline || enrichingId) return
    setEnrichingId(f.firm_id)
    setEnrichNote(null)
    if (noteTimer.current != null) { clearTimeout(noteTimer.current); noteTimer.current = null }

    // Collect through an object so the callback's writes survive TS control-flow analysis (a `let`
    // assigned only inside the closure would stay narrowed to its initial `null`).
    const out: { terminal: Record<string, unknown> | null; fatal: string | null } = { terminal: null, fatal: null }
    try {
      await enrichFirm(f.firm_id, (ev: SSEEvent) => {
        const d = ev.data as Record<string, unknown>
        if (ev.event === 'job.completed') out.terminal = d
        else if (ev.event === 'error' && d.fatal !== false) out.fatal = String(d.message ?? 'enrichment failed')
      })
      if (out.fatal) {
        setEnrichNote({ id: f.firm_id, text: out.fatal, tone: 'bad' })
      } else {
        const t = out.terminal
        const en = t && typeof t.enriched === 'number' ? t.enriched : 0
        const rf = t && typeof t.risk_factors_added === 'number' ? t.risk_factors_added : 0
        setEnrichNote({
          id: f.firm_id,
          text: `Enriched ${en} ${en === 1 ? 'holding' : 'holdings'} · ${rf} risk ${rf === 1 ? 'factor' : 'factors'} added`,
          tone: 'good',
        })
        load() // refresh the registry so any changed fund counts / status flow through
      }
    } catch (e) {
      setEnrichNote({ id: f.firm_id, text: e instanceof Error ? e.message : 'enrichment failed', tone: 'bad' })
    } finally {
      setEnrichingId(null)
      noteTimer.current = window.setTimeout(() => setEnrichNote(null), 6000)
    }
  }

  return (
    <div className="firm-selector" style={{ position: 'relative' }}>
      <button
        type="button"
        className="btn btn-sm"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        title={offline ? 'Firm registry offline — showing the demo firm (selection disabled)' : 'Switch the active firm'}
        style={{ maxWidth: 248 }}
      >
        {scopeAll ? (
          <>
            <span style={{ width: 18, height: 18, flex: 'none', borderRadius: 5, display: 'grid', placeItems: 'center', color: 'var(--accent)', border: '1px solid var(--accent-border)' }}>
              <Icon name="graph" size={12} />
            </span>
            <span className="nowrap" style={{ overflow: 'hidden', textOverflow: 'ellipsis', fontWeight: 600 }}>All data</span>
          </>
        ) : (
          <>
            <FirmAvatar firm={active} size={18} />
            <span className="nowrap" style={{ overflow: 'hidden', textOverflow: 'ellipsis', fontWeight: 600 }}>{active.name}</span>
            {active.status === 'demo' && <DemoBadge />}
          </>
        )}
        <span style={{ display: 'inline-flex', color: 'var(--text-faint)', transition: 'transform .15s', transform: open ? 'rotate(-90deg)' : 'rotate(90deg)' }}>
          <Icon name="arrow" size={13} />
        </span>
      </button>

      {open && (
        <>
          {/* Click-away scrim (transparent, below the menu). */}
          <div onClick={() => setOpen(false)} style={{ position: 'fixed', inset: 0, zIndex: 49 }} />
          <div
            role="menu"
            aria-label="Firms"
            style={{
              position: 'absolute', top: 'calc(100% + 6px)', right: 0, zIndex: 50, minWidth: 268,
              background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--r-md)',
              boxShadow: 'var(--shadow-lg)', padding: 6,
            }}
          >
            <div className="row" style={{ justifyContent: 'space-between', padding: '4px 8px 6px' }}>
              <span className="faint" style={{ fontSize: 10.5, textTransform: 'uppercase', letterSpacing: '0.06em', fontWeight: 700 }}>Firms</span>
              {source && <SourceBadge source={source} />}
            </div>

            {firms.map((f) => {
              const isActive = f.is_active && !scopeAll // in "All data" scope no single firm is active
              const canEnrich = f.source !== 'demo' // only real onboarded firms can be (re-)enriched
              const isEnriching = enrichingId === f.firm_id
              const note = enrichNote?.id === f.firm_id ? enrichNote : null
              return (
                <div key={f.firm_id}>
                  <div
                    className="row"
                    style={{ gap: 2, borderRadius: 'var(--r-sm)', ...(isActive ? { background: 'var(--accent-weak)' } : {}) }}
                  >
                    <button
                      role="menuitem"
                      type="button"
                      className="btn btn-ghost"
                      onClick={() => choose(f)}
                      disabled={(offline || busy != null) && !isActive}
                      title={offline ? 'Selection disabled while the registry is offline' : isActive ? 'Active firm' : `Switch to ${f.name}`}
                      style={{
                        flex: 1, minWidth: 0, justifyContent: 'flex-start', gap: 10, padding: '8px 10px',
                        color: 'var(--text)', background: 'transparent',
                      }}
                    >
                      <FirmAvatar firm={f} size={22} />
                      <span style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 1 }}>
                        <span className="row" style={{ gap: 6 }}>
                          <span className="nowrap" style={{ fontWeight: 600, fontSize: 13, overflow: 'hidden', textOverflow: 'ellipsis' }}>{f.name}</span>
                          {f.status === 'demo' && <DemoBadge />}
                        </span>
                        <span className="faint" style={{ fontSize: 11.5, fontWeight: 400 }}>
                          {isEnriching ? 'enriching…' : `${f.fund_count} ${f.fund_count === 1 ? 'fund' : 'funds'}`}
                        </span>
                      </span>
                      {busy === f.firm_id
                        ? <span className="spinner" style={{ margin: 0, width: 15, height: 15 }} />
                        : isActive
                          ? <span style={{ display: 'inline-flex', color: 'var(--accent)' }}><Icon name="check" size={15} /></span>
                          : null}
                    </button>
                    {canEnrich && (
                      <button
                        role="menuitem"
                        type="button"
                        className="icon-btn"
                        onClick={() => runEnrich(f)}
                        disabled={offline || enrichingId != null}
                        title={offline ? 'Enrichment needs a live backend' : `Enrich ${f.name} — pull top holdings’ 10-K risk factors`}
                        aria-label={`Enrich ${f.name}`}
                        style={{ flex: 'none', marginRight: 4 }}
                      >
                        {isEnriching
                          ? <span className="spinner" style={{ margin: 0, width: 14, height: 14 }} />
                          : <Icon name="impact" size={15} />}
                      </button>
                    )}
                  </div>
                  {note && (
                    <div
                      className="row"
                      style={{ gap: 6, padding: '1px 10px 5px 42px', fontSize: 11, color: note.tone === 'good' ? 'var(--good)' : 'var(--bad)' }}
                    >
                      <span style={{ display: 'inline-flex', flex: 'none' }}><Icon name={note.tone === 'good' ? 'check' : 'alert'} size={12} /></span>
                      <span className="nowrap" style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' }}>{note.text}</span>
                    </div>
                  )}
                </div>
              )
            })}

            {/* Unscoped "All data" scope — the whole graph + all documents, incl. ad-hoc uploads that
                attach to no firm. Sends the ALL_DATA_SCOPE sentinel through the firm-scoped getters. */}
            <button
              role="menuitem"
              type="button"
              className="btn btn-ghost"
              onClick={() => { setOpen(false); onSelectAll?.() }}
              title="Show the whole graph and all documents, ignoring the active firm (includes uploaded documents)"
              style={{
                width: '100%', justifyContent: 'flex-start', gap: 10, padding: '8px 10px', color: 'var(--text)',
                borderRadius: 'var(--r-sm)', ...(scopeAll ? { background: 'var(--accent-weak)' } : {}),
              }}
            >
              <span style={{ width: 22, height: 22, flex: 'none', borderRadius: 6, display: 'grid', placeItems: 'center', border: '1px solid var(--border-strong)', color: 'var(--accent)' }}>
                <Icon name="graph" size={14} />
              </span>
              <span style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 1 }}>
                <span className="nowrap" style={{ fontWeight: 600, fontSize: 13 }}>All data</span>
                <span className="faint" style={{ fontSize: 11.5, fontWeight: 400 }}>unscoped · includes uploads</span>
              </span>
              {scopeAll && <span style={{ display: 'inline-flex', color: 'var(--accent)' }}><Icon name="check" size={15} /></span>}
            </button>

            <div style={{ height: 1, background: 'var(--border)', margin: '6px 4px' }} />

            {/* Onboard a new firm from EDGAR + GLEIF (opens the AddFirmModal via App). */}
            <button
              role="menuitem"
              type="button"
              className="btn btn-ghost"
              onClick={() => { setOpen(false); onAddFirm?.() }}
              title="Onboard a new firm from SEC EDGAR + GLEIF"
              style={{
                width: '100%', justifyContent: 'flex-start', gap: 10, padding: '8px 10px', color: 'var(--text)',
              }}
            >
              <span style={{ width: 22, height: 22, flex: 'none', borderRadius: 6, display: 'grid', placeItems: 'center', border: '1px dashed var(--border-strong)', color: 'var(--accent)', fontSize: 16, fontWeight: 600, lineHeight: 1 }}>+</span>
              <span style={{ flex: 1, fontWeight: 600, fontSize: 13 }}>Add firm…</span>
            </button>

            {offline && (
              <div className="faint" style={{ fontSize: 11, padding: '6px 8px 2px', lineHeight: 1.4 }}>
                Registry offline — showing the committed demo firm. Selection re-enables once <span className="mono">/firms</span> is live.
              </div>
            )}
          </div>
        </>
      )}
    </div>
  )
}

/** Small square "avatar" with the firm's initial — gradient for the demo firm, accent otherwise. */
function FirmAvatar({ firm, size }: { firm: Firm; size: number }) {
  const demo = firm.status === 'demo'
  const initial = firm.name.trim().charAt(0).toUpperCase() || 'F'
  return (
    <span
      aria-hidden="true"
      style={{
        width: size, height: size, flex: 'none', borderRadius: Math.round(size / 3.5),
        display: 'grid', placeItems: 'center', color: '#fff', fontWeight: 700, fontSize: Math.round(size * 0.52),
        background: demo ? 'linear-gradient(135deg, #3b5bdb, #7048e8)' : 'var(--accent)',
        boxShadow: 'inset 0 0 0 1px rgba(255,255,255,0.15)',
      }}
    >
      {initial}
    </span>
  )
}

function DemoBadge() {
  return (
    <span
      style={{
        flex: 'none', fontSize: 9.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em',
        color: 'var(--accent)', background: 'var(--accent-weak)', border: '1px solid var(--accent-border)',
        padding: '0 5px', borderRadius: 20, lineHeight: 1.7,
      }}
    >
      demo
    </span>
  )
}
