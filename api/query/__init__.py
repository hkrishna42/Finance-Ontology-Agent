"""M3 query pipeline: Router → (analytics | text-to-Cypher + vector) → Synthesizer.

Entry point is `graph.QueryGraph.answer(...)`, exposed over HTTP by `routes` (prefix `/query`).
Read-only is enforced twice on any generated Cypher (the `cypher_guard` blocklist AND a
reader-mode Neo4j transaction), and entitlement filtering on `sensitivity` produces an honest
`withheld_count`.
"""
