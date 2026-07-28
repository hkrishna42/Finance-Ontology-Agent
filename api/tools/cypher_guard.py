"""Read-only Cypher guard — the backstop for the M3 query pipeline.

The Analyst agent may generate Cypher against the graph. Before anything is executed we run
`is_read_only(q)`: it must be a single read statement (MATCH/WITH/UNWIND/CALL…RETURN/SHOW) with
no write clause, no stacked statement, no `apoc.*`, and no non-allowlisted procedure call. This is
a Gate-0 down-payment; M3 hardens it further (parameterisation, row caps, entitlement filters).

Design: scrub string literals + comments first (so a write keyword inside a string literal or a
commented-out clause is neither a false reject nor a hiding place), then apply a keyword blocklist
and a procedure allowlist to the scrubbed text.
"""

from __future__ import annotations

import re

# Clauses that mutate the graph or schema. Any occurrence ⇒ not read-only.
WRITE_KEYWORDS: frozenset[str] = frozenset(
    {
        "CREATE",
        "MERGE",
        "DELETE",
        "DETACH",
        "SET",
        "REMOVE",
        "DROP",
        "FOREACH",
        "LOAD",  # LOAD CSV
    }
)

# Only these procedures may be CALLed from a "read-only" query (vector/fulltext search + metadata).
READ_CALL_ALLOWLIST: frozenset[str] = frozenset(
    {
        "db.index.vector.querynodes",
        "db.index.fulltext.querynodes",
        "db.index.fulltext.queryrelationships",
        "db.labels",
        "db.relationshiptypes",
        "db.propertykeys",
        "db.schema.visualization",
        "db.schema.nodetypeproperties",
    }
)

# A read query must begin with one of these keywords.
READ_STARTERS: frozenset[str] = frozenset(
    {"MATCH", "OPTIONAL", "WITH", "UNWIND", "RETURN", "CALL", "SHOW"}
)

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"//[^\n]*")
_SQ_STRING = re.compile(r"'(?:[^'\\]|\\.)*'")
_DQ_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
_BACKTICK = re.compile(r"`[^`]*`")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CALL_PROC = re.compile(r"\bCALL\s+([A-Za-z0-9_.]+)", re.IGNORECASE)


def _scrub(q: str) -> str:
    """Blank out comments and string/backtick literals so only structural tokens remain."""
    q = _BLOCK_COMMENT.sub(" ", q)
    q = _LINE_COMMENT.sub(" ", q)
    q = _SQ_STRING.sub("''", q)
    q = _DQ_STRING.sub('""', q)
    q = _BACKTICK.sub("``", q)
    return q


def is_read_only(query: object) -> bool:
    """Return True only if `query` is a single, safe, read-only Cypher statement."""
    if not isinstance(query, str):
        return False
    stripped = query.strip()
    if not stripped:
        return False

    scrubbed = _scrub(stripped)
    body = scrubbed.strip().rstrip(";").strip()
    if not body:
        return False
    # Stacked statements (injection): no semicolons allowed inside the (single) statement.
    if ";" in body:
        return False

    upper = body.upper()
    # apoc.* is never allowed (write vectors; not present in CE anyway).
    if "APOC." in upper:
        return False

    tokens_upper = {m.group(0).upper() for m in _WORD.finditer(body)}
    if tokens_upper & WRITE_KEYWORDS:
        return False

    # Every CALLed procedure must be on the read allowlist.
    for proc in _CALL_PROC.findall(body):
        if proc.lower() not in READ_CALL_ALLOWLIST:
            return False

    # Must actually read something back.
    if "RETURN" not in tokens_upper and "YIELD" not in tokens_upper:
        return False

    # Must start with a recognised read clause.
    first = _WORD.search(body)
    if first is None or first.group(0).upper() not in READ_STARTERS:
        return False

    return True


def assert_read_only(query: str) -> str:
    """Return `query` if read-only, else raise `ValueError` (for call sites that prefer raising)."""
    if not is_read_only(query):
        raise ValueError("query rejected by read-only Cypher guard")
    return query
