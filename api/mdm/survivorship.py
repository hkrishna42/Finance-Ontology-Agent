"""Attribute-level survivorship engine — reconcile conflicting source records into one golden record.

Reads the declarative `survivorship_policy.yaml` (like `change_impact.load_policy` reads
`impact_policy.yaml`) and applies a per-attribute rule to the cluster of source records, producing a
canonical golden record + an auditable decision log. `run_match_and_merge` publishes it: the Gold dim
+ per-attribute lineage in the lakehouse, and (optionally) the canonical node in Neo4j — reusing the
deterministic MERGE write pattern, never the LLM steward.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

import yaml

from ..fibo import grounding
from ..lakehouse import store as lakehouse
from ..ontology.schema import entity_spec
from . import matching
from .models import GoldenRecord, SurvivorshipDecision

_POLICY_PATH = Path(__file__).parent / "survivorship_policy.yaml"


def load_policy(path: Path = _POLICY_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


# --- value helpers --------------------------------------------------------------------------


def _nonempty(v: Any) -> bool:
    return v not in (None, "", "Not Stated", "N/A", "na")


def _num(v: Any) -> float | None:
    m = re.search(r"-?\d[\d,]*\.?\d*", str(v or ""))
    return float(m.group().replace(",", "")) if m else None


# --- survivorship rules: (attr, records, spec, trust) -> (source, value, rationale, alternates) ----


def _trust_wins(attr, records, spec, trust):
    cands = [r for r in records if _nonempty(r["attributes"].get(attr))]
    if not cands:
        return None, None, f"No source provided {attr}.", []
    w = max(cands, key=lambda r: trust.get(r["system_id"], 0))
    return (w["system_id"], w["attributes"][attr],
            f"Highest-trust source ({w['system_id']}, trust {trust.get(w['system_id'])}) wins.", [])


def _system_of_record(attr, records, spec, trust):
    src = spec.get("source")
    for r in records:
        if r["system_id"] == src and _nonempty(r["attributes"].get(attr)):
            return src, r["attributes"][attr], f"The curated system of record ({src}) governs {attr}.", []
    return _trust_wins(attr, records, spec, trust)


def _crosswalk(attr, records, spec, trust):
    src = spec.get("source")
    canonical, canon_src = None, None
    for r in records:
        if r["system_id"] == src and _nonempty(r["attributes"].get(attr)):
            canonical, canon_src = r["attributes"][attr], src
            break
    if canonical is None:
        cands = [r for r in records if _nonempty(r["attributes"].get(attr))]
        if cands:
            w = max(cands, key=lambda r: trust.get(r["system_id"], 0))
            canonical, canon_src = w["attributes"][attr], w["system_id"]
    alts = sorted({
        str(r["attributes"].get(attr)) for r in records
        if _nonempty(r["attributes"].get(attr)) and str(r["attributes"].get(attr)) != str(canonical)
    })
    rat = f"Canonical identifier {canonical}; alternates retained and crosswalked: {', '.join(alts) or 'none'}."
    return canon_src, canonical, rat, alts


def _most_complete(attr, records, spec, trust):
    cands = [r for r in records if _nonempty(r["attributes"].get(attr))]
    if not cands:
        return None, None, f"No source provided {attr}.", []

    def score(r):
        v = str(r["attributes"][attr])
        bonus = 100 if (attr == "address" and re.search(r"[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}", v)) else 0
        return (len(v) + bonus, trust.get(r["system_id"], 0))

    w = max(cands, key=score)
    extra = " (includes the full postcode)" if attr == "address" else ""
    return (w["system_id"], w["attributes"][attr],
            f"Most complete value from {w['system_id']}{extra}.", [])


def _numeric_tolerance(attr, records, spec, trust):
    cands = [(r, _num(r["attributes"].get(attr))) for r in records]
    cands = [(r, n) for r, n in cands if n is not None]
    if not cands:
        return _trust_wins(attr, records, spec, trust)
    nums = [n for _, n in cands]
    spread = (max(nums) - min(nums)) / max(abs(min(nums)), 1.0)
    tol = float(spec.get("tolerance_pct", 1.0)) / 100.0
    src = spec.get("source")
    winner = next((r for r, _ in cands if r["system_id"] == src),
                  max(cands, key=lambda rn: trust.get(rn[0]["system_id"], 0))[0])
    if spread <= tol:
        rat = (f"Values agree within tolerance ({spread * 100:.2f}% ≤ {spec.get('tolerance_pct')}%); "
               f"the curated {winner['system_id']} value survives.")
    else:
        rat = (f"Values differ by {spread * 100:.1f}% (> {spec.get('tolerance_pct')}%); "
               f"highest-trust {winner['system_id']} wins.")
    return winner["system_id"], winner["attributes"][attr], rat, []


def _ontology_classification(attr, records, spec, trust):
    variants = sorted({
        str(r["attributes"].get(attr)) for r in records if _nonempty(r["attributes"].get(attr))
    })
    curies = {grounding.PROPERTY_TYPE_TO_FIBO.get(str(v).strip().lower()) for v in variants}
    curies.discard(None)
    fibo = next(iter(curies)) if len(curies) == 1 else None
    canonical = spec.get("canonical") or (variants[0] if variants else None)
    rat = (f"{', '.join(variants)} map to one canonical property class"
           + (f" ({fibo})." if fibo else "."))
    return "ontology rule engine", canonical, rat, []


_RULES = {
    "trust_wins": _trust_wins,
    "system_of_record": _system_of_record,
    "canonical_identifier_crosswalk": _crosswalk,
    "most_complete": _most_complete,
    "numeric_tolerance_then_trust": _numeric_tolerance,
    "ontology_classification_mapping": _ontology_classification,
}


# --- golden record --------------------------------------------------------------------------


def build_golden(records: list[dict[str, Any]], entity_type: str, master_key: str,
                 policy: dict[str, Any], trust: dict[str, int]) -> GoldenRecord:
    et = policy["entity_types"][entity_type]
    default = policy.get("default_rule", "trust_wins")
    canonical: dict[str, Any] = {}
    retained: dict[str, list[str]] = {}
    decisions: list[SurvivorshipDecision] = []
    for attr, spec in et["attributes"].items():
        fn = _RULES.get(spec.get("rule", default), _trust_wins)
        source, value, rationale, alts = fn(attr, records, spec, trust)
        canonical[attr] = value
        if alts:
            retained[attr] = alts
        decisions.append(SurvivorshipDecision(
            attribute=attr, rule=spec.get("label", spec.get("rule", default)),
            winning_source=source or "—", winning_value=value, rationale=rationale,
        ))
    pk = str(canonical.get(et.get("id_field")) or master_key)
    g = grounding.ground(entity_type, attributes=canonical)
    return GoldenRecord(
        entity_type=entity_type, dim_table=et["dim_table"], pk=pk,
        canonical_attributes=canonical, retained_alternate_ids=retained,
        fibo_class=g.class_iri or None, fibo_curie=g.curie or None, decisions=decisions,
    )


def _lineage_rows(golden: GoldenRecord, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_sys = {r["system_id"]: r["record_id"] for r in records}
    rows = []
    for d in golden.decisions:
        bid = by_sys.get(d.winning_source)
        if bid:
            rows.append({"bronze_record_id": bid, "attribute": d.attribute, "contributed": 1})
    return rows


def _write_canonical_node(neo4j_store: Any, golden: GoldenRecord, name_field: str) -> None:
    """Best-effort: MERGE the canonical golden node into Neo4j (deterministic; never the LLM steward)."""
    spec = entity_spec(golden.entity_type)
    key = spec.key if spec else "name"
    key_val = golden.canonical_attributes.get(name_field) or golden.pk
    props = {
        **{k: v for k, v in golden.canonical_attributes.items() if v is not None},
        "fibo_class": golden.fibo_curie,
        "lakehouse_table": golden.dim_table,
        "lakehouse_pk": golden.pk,
        "mdm_golden": True,
    }
    try:
        neo4j_store.run(
            f"MERGE (n:{golden.entity_type} {{{key}: $kv}}) SET n += $props",
            kv=str(key_val), props=props,
        )
    except Exception:  # noqa: BLE001 - publish must not fail on a graph hiccup
        pass


def run_match_and_merge(
    conn: sqlite3.Connection, entity_type: str, master_key: str, *,
    policy: dict[str, Any] | None = None, neo4j_store: Any = None, now: str | None = None,
) -> dict[str, Any]:
    """Match the cluster, reconcile via survivorship, and PUBLISH the golden record (Gold + graph)."""
    policy = policy or load_policy()
    et = policy["entity_types"].get(entity_type)
    if et is None:
        raise KeyError(f"no survivorship policy for entity_type {entity_type!r}")
    records = lakehouse.bronze_for_master(conn, entity_type, master_key)
    trust = lakehouse.trust_by_system(conn)

    m = matching.match(
        records, name_field=et["name_field"], id_field=et.get("id_field"),
        strong_ids=tuple(et.get("strong_ids", [])), threshold=float(et.get("threshold", 0.88)),
    )
    golden = build_golden(records, entity_type, master_key, policy, trust)

    lakehouse.upsert_gold_dim(
        conn, dim_table=golden.dim_table, pk=golden.pk, entity_type=entity_type,
        attributes=golden.canonical_attributes, fibo_class=golden.fibo_curie,
        golden_record_id=f"GR-{golden.pk}", published_at=now,
    )
    lakehouse.replace_lineage(conn, golden.dim_table, golden.pk, _lineage_rows(golden, records))
    if neo4j_store is not None:
        _write_canonical_node(neo4j_store, golden, et["name_field"])

    return {"match": m.as_dict(), "golden_record": golden.as_dict(), "published": True}


def match_only(conn: sqlite3.Connection, entity_type: str, master_key: str,
               policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """Step 3 of the wizard — matching + scoring without publishing."""
    policy = policy or load_policy()
    et = policy["entity_types"].get(entity_type)
    if et is None:
        raise KeyError(f"no survivorship policy for entity_type {entity_type!r}")
    records = lakehouse.bronze_for_master(conn, entity_type, master_key)
    m = matching.match(
        records, name_field=et["name_field"], id_field=et.get("id_field"),
        strong_ids=tuple(et.get("strong_ids", [])), threshold=float(et.get("threshold", 0.88)),
    )
    return m.as_dict()
