import { useCallback, useEffect, useState } from 'react'

export type ThemePref = 'light' | 'dark' | 'system'
const KEY = 'fop-theme'

function apply(pref: ThemePref) {
  const root = document.documentElement
  if (pref === 'system') root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', pref)
}

export function useTheme() {
  const [pref, setPref] = useState<ThemePref>(() => (localStorage.getItem(KEY) as ThemePref) || 'system')

  useEffect(() => {
    apply(pref)
    localStorage.setItem(KEY, pref)
  }, [pref])

  // Cycle system -> light -> dark -> system
  const cycle = useCallback(() => {
    setPref((p) => (p === 'system' ? 'light' : p === 'light' ? 'dark' : 'system'))
  }, [])

  return { pref, setPref, cycle }
}
