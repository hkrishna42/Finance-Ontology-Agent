"""Heading-aware chunker.

Splits a parsed document into chunks (~target_tokens, hard cap hard_max) on paragraph
boundaries, starting a new chunk at headings and keeping table-like blocks atomic. Each chunk
carries char offsets (start/end) so provenance can point back into the source text.

Token counting is approximate (chars/4) — good enough for chunk sizing; the real token cost is
observed from provider usage at call time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING = re.compile(
    r"^\s*(item\s+\d+[a-z]?\b|part\s+[ivx]+\b|[A-Z][A-Z0-9 ,&/()\-]{6,}|#{1,6}\s)",
    re.IGNORECASE,
)


@dataclass
class Chunk:
    index: int
    text: str
    start: int
    end: int
    heading: str | None = None


def approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _is_heading(block: str) -> bool:
    first = block.strip().splitlines()[0] if block.strip() else ""
    return bool(_HEADING.match(first)) and len(first) < 120


def _is_table(block: str) -> bool:
    lines = block.strip().splitlines()
    if len(lines) < 2:
        return False
    piped = sum(1 for ln in lines if ln.count("|") >= 2 or ln.count("\t") >= 2)
    return piped >= max(2, len(lines) // 2)


def _blocks_with_offsets(text: str) -> list[tuple[str, int, int]]:
    """Split into blocks on blank lines, preserving char offsets into the original text."""
    out: list[tuple[str, int, int]] = []
    pos = 0
    for part in re.split(r"(\n\s*\n)", text):
        if not part or part.strip() == "":
            pos += len(part)
            continue
        start = pos
        end = pos + len(part)
        out.append((part, start, end))
        pos = end
    return out


def chunk_document(
    text: str, target_tokens: int = 1500, hard_max: int = 1800
) -> list[Chunk]:
    chunks: list[Chunk] = []
    cur: list[tuple[str, int, int]] = []
    cur_heading: str | None = None

    def flush() -> None:
        nonlocal cur, cur_heading
        if not cur:
            return
        start = cur[0][1]
        end = cur[-1][2]
        body = text[start:end].strip("\n")
        chunks.append(Chunk(len(chunks), body, start, start + len(body), cur_heading))
        cur = []

    for block, start, end in _blocks_with_offsets(text):
        is_heading = _is_heading(block)
        is_table = _is_table(block)
        cur_tokens = approx_tokens(text[cur[0][1] : cur[-1][2]]) if cur else 0
        block_tokens = approx_tokens(block)

        # At a heading: flush any accumulated section, then adopt the heading for what follows
        # (capture it even for the very first block, when the buffer is still empty).
        if is_heading:
            if cur_tokens > 0:
                flush()
            cur_heading = block.strip().splitlines()[0][:120]

        # A table that would overflow the budget goes in its own atomic chunk.
        if is_table and cur_tokens + block_tokens > hard_max and cur:
            flush()

        cur.append((block, start, end))

        if approx_tokens(text[cur[0][1] : cur[-1][2]]) >= target_tokens and not is_table:
            flush()

    flush()
    return chunks
