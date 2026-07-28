"""The steward review pass + graph writer.

`Steward.review(triples, existing=...)` classifies each candidate triple into one action:

    reject      — bad domain/range, unknown relation, or (low-conf) not entailed by its span
    corroborate — same (subject, rel, object) already present → bump corroboration + merge provenance
    conflict    — a *functional* relation already points the subject at a different object
    insert      — a new, valid fact

The LLM entailment check (Role.STEWARD) runs ONLY when a triple's confidence < `low_conf` — high-
confidence structured facts are trusted. Rejected triples are retained (returned and, on write,
persisted as `:RejectedFact` nodes) so they stay queryable. `GraphWriter` applies a `StewardResult`
to Neo4j, stamping `PROVENANCE_FIELDS` + bitemporal tags and appending to a provenance list.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..ontology.schema import (
    PROVENANCE_FIELDS,
    entity_labels,
    entity_spec,
    relation_spec,
    relation_types,
)
from ..providers.base import Role
from ..providers.factory import get_llm_provider

# Relations treated as single-valued: a second, different object for the same subject is a conflict
# (kept + flagged), not an overwrite.
FUNCTIONAL_RELATIONS: frozenset[str] = frozenset({"SUBSIDIARY_OF", "MANAGED_BY"})
LOW_CONF_THRESHOLD = 0.6

ENTAILMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["entailed", "confidence"],
    "properties": {
        "entailed": {"type": "boolean", "description": "does the span state/entail the triple?"},
        "confidence": {"type": "number", "description": "0.0-1.0"},
    },
}

STEWARD_SYSTEM = (
    "You are a knowledge-graph steward enforcing grounding. Given a candidate triple "
    "(subject, relation, object) and the VERBATIM source span it was extracted from, decide whether "
    "the span actually states or entails the triple. Reject hallucinations, over-reaches, and "
    "spans that only mention the entities without asserting the relation. Return entailed=false when "
    "unsure."
)

# Actions
INSERT = "insert"
CORROBORATE = "corroborate"
CONFLICT = "conflict"
REJECT = "reject"

_ACCEPTED = frozenset({INSERT, CORROBORATE, CONFLICT})


# --- data model ----------------------------------------------------------------------------


@dataclass
class CandidateTriple:
    subject_label: str
    subject_key: str
    rel_type: str
    object_label: str
    object_key: str
    qualifiers: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0

    def fact_id(self) -> str:
        raw = "|".join(
            [
                self.subject_label,
                self.subject_key,
                self.rel_type,
                self.object_label,
                self.object_key,
                str(self.provenance.get("span", "")),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class ExistingEdge:
    object_key: str
    corroboration: int = 1
    sources: list[str] = field(default_factory=list)


@dataclass
class StewardDecision:
    triple: CandidateTriple
    action: str
    reason: str | None = None
    conflict: bool = False
    corroboration: int = 1
    entailment_checked: bool = False

    @property
    def accepted(self) -> bool:
        return self.action in _ACCEPTED


@dataclass
class StewardResult:
    decisions: list[StewardDecision]

    @property
    def accepted(self) -> list[StewardDecision]:
        return [d for d in self.decisions if d.accepted]

    @property
    def rejected(self) -> list[StewardDecision]:
        return [d for d in self.decisions if d.action == REJECT]

    @property
    def conflicts(self) -> list[StewardDecision]:
        return [d for d in self.decisions if d.conflict]

    @property
    def facts_changed(self) -> list[dict[str, Any]]:
        """The change set the impact worker consumes (accepted facts only)."""
        out: list[dict[str, Any]] = []
        for d in self.accepted:
            t = d.triple
            out.append(
                {
                    "op": d.action,
                    "subject_label": t.subject_label,
                    "subject_key": t.subject_key,
                    "rel_type": t.rel_type,
                    "object_label": t.object_label,
                    "object_key": t.object_key,
                    "conflict": d.conflict,
                    "corroboration": d.corroboration,
                    "sensitivity": t.provenance.get("sensitivity", "public"),
                }
            )
        return out


ExistingMap = Mapping[tuple[str, str, str], list[ExistingEdge]]
FactsChangedCallback = Callable[[list[dict[str, Any]]], None]


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


# --- steward -------------------------------------------------------------------------------


class Steward:
    """Validate + reconcile candidate triples before they touch the graph."""

    def __init__(
        self,
        *,
        provider: Any = None,
        settings: Any = None,
        functional: frozenset[str] = FUNCTIONAL_RELATIONS,
        low_conf: float = LOW_CONF_THRESHOLD,
    ) -> None:
        self._provider = provider
        self.settings = settings
        self.functional = functional
        self.low_conf = low_conf

    @property
    def provider(self) -> Any:
        if self._provider is None:
            from ..config import get_settings

            self._provider = get_llm_provider(self.settings or get_settings())
        return self._provider

    # -- domain/range ---------------------------------------------------------------------

    def _validate_domain_range(self, t: CandidateTriple) -> str | None:
        """Return a rejection reason if the triple violates the ontology, else None."""
        if t.rel_type not in relation_types():
            return "unknown_relation"
        if t.subject_label not in entity_labels():
            return "unknown_subject_label"
        if t.object_label not in entity_labels():
            return "unknown_object_label"
        spec = relation_spec(t.rel_type)
        if t.subject_label not in spec.domain:
            return f"bad_domain:{t.subject_label} not in {list(spec.domain)}"
        if t.object_label not in spec.range:
            return f"bad_range:{t.object_label} not in {list(spec.range)}"
        return None

    # -- entailment (LLM, low-confidence only) --------------------------------------------

    def entailment_call(self, t: CandidateTriple) -> dict[str, Any]:
        payload = {
            "subject": {"label": t.subject_label, "name": t.subject_key},
            "relation": t.rel_type,
            "object": {"label": t.object_label, "name": t.object_key},
            "span": t.provenance.get("span", ""),
        }
        user = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return {
            "role": Role.STEWARD,
            "schema": ENTAILMENT_SCHEMA,
            "system": STEWARD_SYSTEM,
            "messages": [{"role": "user", "content": user}],
        }

    def _check_entailment(self, t: CandidateTriple) -> bool:
        result = self.provider.complete_structured(**self.entailment_call(t))
        return bool((result.data or {}).get("entailed"))

    # -- review ---------------------------------------------------------------------------

    def review(
        self,
        triples: list[CandidateTriple],
        *,
        existing: ExistingMap | None = None,
        on_facts_changed: FactsChangedCallback | None = None,
    ) -> StewardResult:
        existing = existing or {}
        # Mutable working copy so corroboration accumulates across a batch, too.
        seen: dict[tuple[str, str, str], dict[str, ExistingEdge]] = {}
        for key, edges in existing.items():
            seen[key] = {e.object_key: ExistingEdge(e.object_key, e.corroboration, list(e.sources))
                         for e in edges}

        decisions: list[StewardDecision] = []
        for t in triples:
            reason = self._validate_domain_range(t)
            if reason is not None:
                decisions.append(StewardDecision(t, REJECT, reason=reason))
                continue

            entailment_checked = False
            if t.confidence < self.low_conf:
                entailment_checked = True
                if not self._check_entailment(t):
                    decisions.append(
                        StewardDecision(
                            t, REJECT, reason="not_entailed", entailment_checked=True
                        )
                    )
                    continue

            key = (t.subject_label, t.subject_key, t.rel_type)
            bucket = seen.setdefault(key, {})
            src = str(t.provenance.get("doc_id") or t.provenance.get("chunk_id") or "")

            if t.object_key in bucket:  # dedupe → corroborate
                edge = bucket[t.object_key]
                if src and src not in edge.sources:
                    edge.sources.append(src)
                edge.corroboration += 1
                decisions.append(
                    StewardDecision(
                        t,
                        CORROBORATE,
                        corroboration=edge.corroboration,
                        entailment_checked=entailment_checked,
                    )
                )
            elif t.rel_type in self.functional and bucket:  # functional conflict
                bucket[t.object_key] = ExistingEdge(
                    t.object_key, 1, [src] if src else []
                )
                decisions.append(
                    StewardDecision(
                        t,
                        CONFLICT,
                        reason="functional_conflict",
                        conflict=True,
                        corroboration=1,
                        entailment_checked=entailment_checked,
                    )
                )
            else:  # new fact
                bucket[t.object_key] = ExistingEdge(
                    t.object_key, 1, [src] if src else []
                )
                decisions.append(
                    StewardDecision(
                        t, INSERT, corroboration=1, entailment_checked=entailment_checked
                    )
                )

        result = StewardResult(decisions=decisions)
        if on_facts_changed is not None:
            on_facts_changed(result.facts_changed)
        return result


# --- graph writer --------------------------------------------------------------------------


def _key_prop(label: str) -> str:
    return entity_spec(label).key


class GraphWriter:
    """Persist a `StewardResult` to Neo4j: accepted edges + rejected facts.

    Accepted edges carry the `PROVENANCE_FIELDS` present on the triple, a corroboration count, a
    growing `doc_ids` provenance list, the `conflict` flag, and bitemporal tags (`valid_from` from
    the fact's `as_of`, `recorded_at` = write time). Rejected triples become `:RejectedFact` nodes.
    """

    def __init__(self, store: Any) -> None:
        self.store = store

    def _edge_props(self, d: StewardDecision, now: str) -> dict[str, Any]:
        t = d.triple
        props: dict[str, Any] = {k: t.provenance[k] for k in PROVENANCE_FIELDS if k in t.provenance}
        props.update(t.qualifiers)
        props["corroboration"] = d.corroboration
        props["conflict"] = d.conflict
        props["valid_from"] = t.provenance.get("as_of")
        props["recorded_at"] = now
        return props

    def write(self, result: StewardResult, *, now: str | None = None) -> dict[str, int]:
        now = now or _now_iso()
        edges = 0
        rejected = 0
        for d in result.accepted:
            t = d.triple
            skp, okp = _key_prop(t.subject_label), _key_prop(t.object_label)
            src = str(t.provenance.get("doc_id") or t.provenance.get("chunk_id") or "")
            query = (
                f"MERGE (s:{t.subject_label} {{{skp}: $subject_key}}) "
                f"MERGE (o:{t.object_label} {{{okp}: $object_key}}) "
                f"MERGE (s)-[r:{t.rel_type}]->(o) "
                "SET r += $props "
                "SET r.doc_ids = CASE WHEN $src = '' THEN coalesce(r.doc_ids, []) "
                "  WHEN $src IN coalesce(r.doc_ids, []) THEN r.doc_ids "
                "  ELSE coalesce(r.doc_ids, []) + $src END"
            )
            self.store.run(
                query,
                subject_key=t.subject_key,
                object_key=t.object_key,
                props=self._edge_props(d, now),
                src=src,
            )
            edges += 1
        for d in result.rejected:
            t = d.triple
            self.store.run(
                "MERGE (rf:RejectedFact {id: $id}) "
                "SET rf.subject_label = $sl, rf.subject_key = $sk, rf.rel_type = $rt, "
                "    rf.object_label = $ol, rf.object_key = $ok, rf.reason = $reason, "
                "    rf.span = $span, rf.doc_id = $doc_id, rf.confidence = $conf, "
                "    rf.rejected_at = $now",
                id=t.fact_id(),
                sl=t.subject_label,
                sk=t.subject_key,
                rt=t.rel_type,
                ol=t.object_label,
                ok=t.object_key,
                reason=d.reason,
                span=t.provenance.get("span", ""),
                doc_id=t.provenance.get("doc_id", ""),
                conf=t.confidence,
                now=now,
            )
            rejected += 1
        return {"edges": edges, "rejected": rejected}


def existing_from_store(store: Any, triples: list[CandidateTriple]) -> dict[
    tuple[str, str, str], list[ExistingEdge]
]:
    """Query the live graph for the current objects of each (subject, rel) in `triples`.

    Lets the steward dedupe/detect-conflict against what's already in Neo4j.
    """
    out: dict[tuple[str, str, str], list[ExistingEdge]] = {}
    for t in triples:
        key = (t.subject_label, t.subject_key, t.rel_type)
        if key in out:
            continue
        skp, okp = _key_prop(t.subject_label), _key_prop(t.object_label)
        rows = store.run(
            f"MATCH (s:{t.subject_label} {{{skp}: $sk}})-[r:{t.rel_type}]->(o:{t.object_label}) "
            f"RETURN o.{okp} AS ok, coalesce(r.corroboration, 1) AS corr, "
            "coalesce(r.doc_ids, []) AS sources",
            sk=t.subject_key,
        )
        out[key] = [
            ExistingEdge(object_key=r["ok"], corroboration=r["corr"], sources=list(r["sources"]))
            for r in rows
            if r["ok"] is not None
        ]
    return out


def decisions_as_dicts(result: StewardResult) -> list[dict[str, Any]]:
    """Serialize decisions (for API responses / logging)."""
    out = []
    for d in result.decisions:
        row = asdict(d)
        row["accepted"] = d.accepted
        out.append(row)
    return out
