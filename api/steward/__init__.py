"""Steward — the write-time gatekeeper between extracted triples and the graph.

Responsibilities (see `steward.Steward`):
  * validate every triple's subject/object labels against `RELATION_SPECS` domain/range,
  * dedupe + merge provenance lists (corroboration count) for repeat facts,
  * flag functional-relation conflicts (`conflict: true`) instead of silently overwriting,
  * stamp bitemporal tags (valid-from / recorded-at) and carry `PROVENANCE_FIELDS` on every write,
  * call the steward LLM (Role.STEWARD) for entailment ONLY on low-confidence (<0.6) triples,
  * keep rejected triples queryable (as `:RejectedFact`), and
  * emit a `facts_changed` list (returned + optional callback) for the change-impact worker.

It never builds the ImpactGraph itself — that belongs to the apps worker.
"""
