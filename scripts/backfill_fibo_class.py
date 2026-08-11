#!/usr/bin/env python
"""Backfill `fibo_class` on existing Neo4j nodes (idempotent).

Every groundable node label (Company, Fund, RealProperty, ...) gets its deterministic FIBO class
(``api.fibo.grounding.LABEL_FIBO_CURIE`` — the ontology default) stamped where it is missing.
``WHERE n.fibo_class IS NULL`` means it never overwrites a refined class already stamped by the
extraction pipeline or MDM (e.g. ``CorporateDebtIssuer``). New writes stamp ``fibo_class`` inline
(``api/l2/nport.py``, ``api/onboarding/pipeline.py``, ``api/ingest/pipeline.py``), so this is a
one-time catch-up for nodes written before that (the seed graph + earlier onboards).

Run:  NEO4J_URI=bolt://localhost:7687 uv run python scripts/backfill_fibo_class.py
"""

from __future__ import annotations

from typing import Any

from neo4j import GraphDatabase

from api.config import get_settings
from api.fibo.grounding import LABEL_FIBO_CURIE


def backfill(driver: Any) -> dict[str, int]:
    """Stamp the label-default FIBO class on every groundable node missing one. Returns per-label counts."""
    counts: dict[str, int] = {}
    with driver.session() as sess:
        for label, curie in LABEL_FIBO_CURIE.items():
            rec = sess.run(
                f"MATCH (n:`{label}`) WHERE n.fibo_class IS NULL "  # noqa: S608 - label is a trusted ontology constant
                "SET n.fibo_class = $curie "
                "RETURN count(n) AS n",
                curie=curie,
            ).single()
            n = int(rec["n"]) if rec else 0
            if n:
                counts[label] = n
    return counts


def main() -> None:
    s = get_settings()
    driver = GraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password))
    try:
        counts = backfill(driver)
    finally:
        driver.close()
    for label, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {label:16} +{n}")
    print(f"stamped fibo_class on {sum(counts.values())} node(s)")


if __name__ == "__main__":
    main()
