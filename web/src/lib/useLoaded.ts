import { useCallback, useEffect, useState } from 'react'
import type { Loaded, Source } from '../api'

interface State<T> { data: T | null; source: Source | null; loading: boolean; error: string | null }

/** Runs an async loader (that returns { data, source }) on mount and exposes reload(). */
export function useLoaded<T>(loader: () => Promise<Loaded<T>>, deps: unknown[] = []) {
  const [state, setState] = useState<State<T>>({ data: null, source: null, loading: true, error: null })

  const run = useCallback(() => {
    let alive = true
    setState((s) => ({ ...s, loading: true, error: null }))
    loader()
      .then((r) => { if (alive) setState({ data: r.data, source: r.source, loading: false, error: null }) })
      .catch((e) => { if (alive) setState({ data: null, source: null, loading: false, error: String(e) }) })
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  useEffect(run, [run])
  return { ...state, reload: run }
}
