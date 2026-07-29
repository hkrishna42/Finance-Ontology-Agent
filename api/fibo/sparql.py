"""SPARQL 1.1 over the reasoned FIBO TBox (Agent D uses this alongside GraphRAG).

Read-only queries over the in-memory reasoned graph. Federated `SERVICE` calls are rejected so a query
can never reach the network — this stays an offline, self-contained endpoint.
"""

from __future__ import annotations

from typing import Any

from . import tbox


class SparqlError(ValueError):
    """A rejected or invalid SPARQL query."""


def query(sparql: str) -> dict[str, Any]:
    """Run a read-only SPARQL SELECT/ASK over the reasoned TBox → `{columns, rows}`."""
    text = (sparql or "").strip()
    if not text:
        raise SparqlError("empty query")
    low = text.lower()
    if "service" in low:
        raise SparqlError("federated SERVICE queries are not allowed")
    if not (low.startswith(("select", "ask", "prefix", "construct", "describe"))):
        raise SparqlError("only read-only SELECT/ASK/CONSTRUCT/DESCRIBE queries are allowed")

    g = tbox.load_tbox()
    try:
        res = g.query(text, initNs={"owl": "http://www.w3.org/2002/07/owl#"})
    except Exception as exc:  # noqa: BLE001 - surface a clean 400, never a 500
        raise SparqlError(str(exc)[:300]) from exc

    if res.type == "ASK":
        answer = bool(res.askAnswer)
        return {"columns": ["ask"], "rows": [[answer]], "count": 1, "boolean": answer}

    columns = [str(v) for v in (res.vars or [])]
    rows = [
        [(str(cell) if cell is not None else None) for cell in row]
        for row in res
    ]
    return {"columns": columns, "rows": rows, "count": len(rows)}
