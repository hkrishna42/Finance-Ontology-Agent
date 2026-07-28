import { useCallback, useEffect, useState } from 'react'
import { getFirms, selectFirm } from '../api'
import type { Source } from '../api'
import type { Firm } from '../types'
import { SourceBadge } from '../lib/ui'
import { Icon } from '../lib/icons'

// Header firm selector. Lists registered firms (name + fund count + demo badge), highlights the
// active one, and on selection POSTs /firms/{id}/select then asks App to refresh the panels
// (the server defaults every panel to the active firm). When /firms is unreachable the data layer
// serves the committed demo-firm fixture — we still render it, but selection is disabled.

export function FirmSelector({ onSelect, onAddFirm, reloadKey }: {
  onSelect?: (firm: Firm) => void
  onAddFirm?: () => void
  reloadKey?: number
}) {
  const [firms, setFirms] = useState<Firm[] | null>(null)
  const [source, setSource] = useState<Source | null>(null)
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)

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

  if (!firms || firms.length === 0) return null // nothing registered → hide gracefully

  const offline = source !== 'live'
  const active = firms.find((f) => f.is_active) ?? firms[0]

  const choose = async (f: Firm) => {
    if (offline || busy) return
    if (f.is_active) { setOpen(false); return }
    setBusy(f.firm_id)
    const updated = await selectFirm(f.firm_id)
    setBusy(null)
    setOpen(false)
    // Optimistically flip the active flag so the trigger + list update immediately.
    setFirms((prev) => (prev ? prev.map((x) => ({ ...x, is_active: x.firm_id === f.firm_id })) : prev))
    onSelect?.(updated ?? { ...f, is_active: true })
  }

  return (
    <div style={{ position: 'relative' }}>
      <button
        type="button"
        className="btn btn-sm"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        title={offline ? 'Firm registry offline — showing the demo firm (selection disabled)' : 'Switch the active firm'}
        style={{ maxWidth: 248 }}
      >
        <FirmAvatar firm={active} size={18} />
        <span className="nowrap" style={{ overflow: 'hidden', textOverflow: 'ellipsis', fontWeight: 600 }}>{active.name}</span>
        {active.status === 'demo' && <DemoBadge />}
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
              const isActive = f.is_active
              return (
                <button
                  key={f.firm_id}
                  role="menuitem"
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => choose(f)}
                  disabled={(offline || busy != null) && !isActive}
                  title={offline ? 'Selection disabled while the registry is offline' : isActive ? 'Active firm' : `Switch to ${f.name}`}
                  style={{
                    width: '100%', justifyContent: 'flex-start', gap: 10, padding: '8px 10px', color: 'var(--text)',
                    ...(isActive ? { background: 'var(--accent-weak)' } : {}),
                  }}
                >
                  <FirmAvatar firm={f} size={22} />
                  <span style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 1 }}>
                    <span className="row" style={{ gap: 6 }}>
                      <span className="nowrap" style={{ fontWeight: 600, fontSize: 13, overflow: 'hidden', textOverflow: 'ellipsis' }}>{f.name}</span>
                      {f.status === 'demo' && <DemoBadge />}
                    </span>
                    <span className="faint" style={{ fontSize: 11.5, fontWeight: 400 }}>
                      {f.fund_count} {f.fund_count === 1 ? 'fund' : 'funds'}
                    </span>
                  </span>
                  {busy === f.firm_id
                    ? <span className="spinner" style={{ margin: 0, width: 15, height: 15 }} />
                    : isActive
                      ? <span style={{ display: 'inline-flex', color: 'var(--accent)' }}><Icon name="check" size={15} /></span>
                      : null}
                </button>
              )
            })}

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
