"""The resolution pipeline — mention → (CIK, LEI) on the SEC/GLEIF spine.

Order of operations (each step only runs if the earlier, cheaper, more-certain ones miss):

    1. exact **ticker** hit            → deterministic, confidence 1.0
    2. exact normalized **alias** hit  → deterministic, confidence 1.0
    3. **embedding** candidates        → cosine ≥ `sim_threshold` (default 0.90) over spine titles
    4. **LLM adjudication**            → Role.RESOLUTION picks the matching CIK (or "no match")
    5. **GLEIF** enrichment            → attach the LEI to whatever CIK we landed on

Anything unresolved becomes a *provisional* `Resolution` (and is queued in SQLite when a connection
is supplied). The embedder, LLM provider, and GLEIF client are all injectable so the whole pipeline
runs offline and deterministically in tests.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any

from ..config import get_settings
from ..providers.base import Role
from ..providers.embeddings import get_embedder
from ..providers.factory import get_llm_provider
from . import store as queue_store
from .gleif import Gleif
from .normalize import normalize, normalize_ticker
from .spine import CompanyRecord, Spine

# JSON schema for the adjudication step. The FakeProvider's minimal-valid instance is
# {"match": false, "cik": "", "confidence": 0} — a safe "no match" default that routes ambiguous
# mentions to the provisional queue unless a cassette/fixture says otherwise.
ADJUDICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["match", "cik", "confidence"],
    "properties": {
        "match": {"type": "boolean", "description": "true iff one candidate is the same issuer"},
        "cik": {"type": "string", "description": "chosen 10-digit CIK, or '' when match is false"},
        "confidence": {"type": "number", "description": "0.0-1.0"},
    },
}

RESOLUTION_SYSTEM = (
    "You are an entity-resolution adjudicator for SEC issuer names. Given a mention and a short "
    "list of candidate issuers (each with a CIK, ticker, and legal name), decide whether exactly "
    "one candidate is the SAME real-world issuer as the mention. Only match on genuine identity, "
    "never on mere sector or name similarity. Return match=false and cik='' if none clearly match. "
    "When you match, copy the chosen candidate's CIK verbatim."
)


@dataclass(frozen=True)
class Candidate:
    cik: str
    title: str
    ticker: str
    score: float


@dataclass
class Resolution:
    mention: str
    normalized: str
    ticker: str | None
    status: str  # 'resolved' | 'provisional'
    method: str  # 'ticker' | 'alias' | 'embedding_llm' | 'provisional'
    cik: str | None
    lei: str | None
    title: str | None
    confidence: float
    candidates: list[Candidate] = field(default_factory=list)
    queue_id: int | None = None

    @property
    def resolved(self) -> bool:
        return self.status == "resolved"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class Resolver:
    """Resolve issuer mentions to the spine. All external seams are injectable for offline tests."""

    def __init__(
        self,
        spine: Spine | None = None,
        *,
        settings: Any = None,
        embedder: Any = None,
        provider: Any = None,
        gleif: Gleif | None = None,
        sim_threshold: float = 0.90,
        llm_min_conf: float = 0.5,
        max_candidates: int = 5,
    ) -> None:
        self.settings = settings or get_settings()
        self.spine = spine if spine is not None else Spine.load()
        self._embedder = embedder
        self._provider = provider
        self.gleif = gleif
        self.sim_threshold = sim_threshold
        self.llm_min_conf = llm_min_conf
        self.max_candidates = max_candidates
        # Precompute the candidate title matrix lazily (only when the embedding step is reached).
        self._cand_records: list[CompanyRecord] | None = None
        self._cand_vectors: list[list[float]] | None = None

    # -- lazy singletons ------------------------------------------------------------------

    @property
    def embedder(self) -> Any:
        if self._embedder is None:
            self._embedder = get_embedder(self.settings)
        return self._embedder

    @property
    def provider(self) -> Any:
        if self._provider is None:
            self._provider = get_llm_provider(self.settings)
        return self._provider

    def _ensure_candidate_index(self) -> None:
        if self._cand_vectors is not None:
            return
        records = [c for c in self.spine.companies if c.norm]
        texts = [c.norm for c in records]
        vectors = self.embedder.embed(texts) if texts else []
        self._cand_records = records
        self._cand_vectors = vectors

    # -- adjudication call (shared with the fixture generator so keys stay stable) ---------

    def adjudication_call(
        self, mention: str, candidates: list[Candidate]
    ) -> dict[str, Any]:
        payload = {
            "mention": mention,
            "candidates": [
                {"cik": c.cik, "ticker": c.ticker, "legal_name": c.title} for c in candidates
            ],
        }
        user = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return {
            "role": Role.RESOLUTION,
            "schema": ADJUDICATION_SCHEMA,
            "system": RESOLUTION_SYSTEM,
            "messages": [{"role": "user", "content": user}],
        }

    # -- steps ----------------------------------------------------------------------------

    def _embedding_candidates(self, norm: str) -> list[Candidate]:
        self._ensure_candidate_index()
        assert self._cand_records is not None and self._cand_vectors is not None
        if not norm or not self._cand_vectors:
            return []
        qvec = self.embedder.embed([norm])[0]
        scored = [
            Candidate(cik=rec.cik, title=rec.title, ticker=rec.ticker, score=_cosine(qvec, vec))
            for rec, vec in zip(self._cand_records, self._cand_vectors, strict=True)
        ]
        scored = [c for c in scored if c.score >= self.sim_threshold]
        scored.sort(key=lambda c: c.score, reverse=True)
        return scored[: self.max_candidates]

    def _adjudicate(self, mention: str, candidates: list[Candidate]) -> tuple[str | None, float]:
        call = self.adjudication_call(mention, candidates)
        result = self.provider.complete_structured(**call)
        data = result.data or {}
        if not data.get("match"):
            return None, float(data.get("confidence", 0.0) or 0.0)
        cik = str(data.get("cik") or "").strip()
        valid = {c.cik for c in candidates}
        if cik not in valid:  # never trust a CIK the model didn't pick from the candidate set
            return None, 0.0
        return cik, float(data.get("confidence", 0.0) or 0.0)

    def _enrich_lei(self, title: str | None, cik: str | None) -> str | None:
        if self.gleif is None or not title:
            return None
        return self.gleif.lei_for(title, cik=cik)

    # -- public API -----------------------------------------------------------------------

    def resolve(
        self,
        name: str,
        *,
        ticker: str | None = None,
        enrich_lei: bool = True,
        conn: sqlite3.Connection | None = None,
    ) -> Resolution:
        norm = normalize(name)
        tkr = normalize_ticker(ticker)

        def _finish(rec: CompanyRecord, method: str, confidence: float) -> Resolution:
            lei = self._enrich_lei(rec.title, rec.cik) if enrich_lei else None
            return Resolution(
                mention=name,
                normalized=norm,
                ticker=tkr or (rec.ticker or None),
                status="resolved",
                method=method,
                cik=rec.cik,
                lei=lei,
                title=rec.title,
                confidence=confidence,
            )

        # 1. exact ticker
        if tkr:
            rec = self.spine.resolve_ticker(tkr)
            if rec is not None:
                return _finish(rec, "ticker", 1.0)

        # 2. exact normalized alias
        rec = self.spine.resolve_norm(norm)
        if rec is not None:
            return _finish(rec, "alias", 1.0)

        # 3 + 4. embedding candidates → LLM adjudication
        candidates = self._embedding_candidates(norm)
        if candidates:
            cik, conf = self._adjudicate(name, candidates)
            if cik is not None and conf >= self.llm_min_conf:
                chosen = next(c for c in candidates if c.cik == cik)
                rec = CompanyRecord(
                    cik=chosen.cik,
                    ticker=chosen.ticker,
                    title=chosen.title,
                    norm=normalize(chosen.title),
                )
                return _finish(rec, "embedding_llm", conf)

        # 5. unresolved → provisional (queue if a connection is supplied)
        res = Resolution(
            mention=name,
            normalized=norm,
            ticker=tkr,
            status="provisional",
            method="provisional",
            cik=None,
            lei=None,
            title=None,
            confidence=0.0,
            candidates=candidates,
        )
        if conn is not None:
            res.queue_id = queue_store.enqueue(
                conn,
                mention=name,
                normalized=norm,
                ticker=tkr,
                confidence=0.0,
                candidates=[asdict(c) for c in candidates],
            )
        return res

    def resolve_fund(
        self, *, series_id: str | None = None, ticker: str | None = None, name: str | None = None
    ) -> Resolution:
        """Resolve a fund by series id or share-class ticker onto the mutual-fund spine."""
        rec = self.spine.resolve_series(series_id) or self.spine.resolve_fund_ticker(ticker)
        mention = name or series_id or ticker or ""
        if rec is None:
            return Resolution(
                mention=mention,
                normalized=normalize(mention),
                ticker=normalize_ticker(ticker),
                status="provisional",
                method="provisional",
                cik=None,
                lei=None,
                title=None,
                confidence=0.0,
            )
        return Resolution(
            mention=mention,
            normalized=normalize(mention),
            ticker=normalize_ticker(ticker),
            status="resolved",
            method="series" if series_id else "fund_ticker",
            cik=rec.cik,
            lei=None,
            title=rec.series_id,
            confidence=1.0,
        )
