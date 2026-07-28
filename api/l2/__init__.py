"""L2 firm-structure layer — deterministic (non-LLM) structured parsers.

Currently: the N-PORT-P holdings parser (`api.l2.nport`), which turns a fund's monthly portfolio
filing into a `Fund` node + weighted `HOLDS` edges. Structural facts are never invented by a model
(see the ontology module docstring): `HOLDS` is `extractable=False`.
"""
