// Graph Explorer data helper: lazy-load a node's next tranche of neighbours from the backend, for
// firms whose full subgraph exceeds the initial /graph payload cap. Returns the same {nodes, edges}
// shape as /graph (nodes carry the top-level fibo_class grounding; edges are weight-ranked server-side).

import type { GraphEdge, GraphNode } from '../types'

export interface NeighborsResult { nodes: GraphNode[]; edges: GraphEdge[] }

/** GET /graph/neighbors?node_id=&limit=&min_confidence=&entitlements=public — a node's neighbourhood.
 *  `nodeId` is the node's `id` (Neo4j elementId). Returns null on any network/HTTP/shape failure so
 *  callers can quietly keep the client-side view they already have. */
export async function fetchNeighbors(
  nodeId: string,
  opts?: { limit?: number; minConfidence?: number },
): Promise<NeighborsResult | null> {
  const qs = new URLSearchParams({ node_id: nodeId, entitlements: 'public' })
  qs.set('limit', String(opts?.limit ?? 200))
  if (opts?.minConfidence != null) qs.set('min_confidence', String(opts.minConfidence))
  try {
    const r = await fetch(`/graph/neighbors?${qs.toString()}`)
    if (!r.ok) return null
    const d = (await r.json()) as { nodes?: unknown; edges?: unknown } | null
    if (!d || !Array.isArray(d.nodes) || !Array.isArray(d.edges)) return null
    return { nodes: d.nodes as GraphNode[], edges: d.edges as GraphEdge[] }
  } catch {
    return null
  }
}
