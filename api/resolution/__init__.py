"""Entity resolution — collapse issuer aliases onto a single CIK/LEI spine.

Pipeline (see `resolver.Resolver`):
    normalize → exact ticker/alias hit → embedding-similarity candidates (cosine ≥ 0.90) →
    LLM adjudication (Role.RESOLUTION) → GLEIF LEI enrichment.
Anything still unresolved becomes a *provisional* record queued in SQLite for a human steward to
merge (see `store.ProvisionalQueue` and `routes`).
"""
