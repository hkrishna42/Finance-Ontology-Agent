"""M6 Change Impact — deterministic fact-diff + policy-driven downstream propagation.

Pipeline:
  1. SUPERSEDES versioning: a new Document supersedes an older one (Document-[:SUPERSEDES]->Document).
  2. Fact-diff: two triple sets keyed on (subject, predicate, object/value, period) →
     {added, removed, changed} with old/new spans.
  3. Propagation engine: READS `api/ontology/impact_policy.yaml` and applies each rule's traversal
     against the graph (via a Resolver) to find the downstream impact set. Every hop is cited.
  4. Impact narrator (Role.IMPACT; stub fixture) → a cited briefing + an `impact` SSEEvent.

All numbers/edges come from the graph; the LLM only phrases the briefing. The engine is generic
over the policy: it dispatches on each rule's traverse relation-sequence, reading hop counts,
change filters and target flags from the YAML — so a policy edit changes behaviour with no code
change, and an *unsupported* traversal raises (surfacing a needed contract/resolver change).
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from ..contracts.events import EventType, SSEEvent, make_event
from ..providers.base import Role

POLICY_PATH = Path(__file__).resolve().parents[1] / "ontology" / "impact_policy.yaml"
FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "impact"


# --- Policy model (read-only view of impact_policy.yaml) -----------------------------------


@dataclass(frozen=True)
class Rule:
    name: str
    trigger: dict
    traverse: list[dict]
    effect: str
    target_flag: dict | None = None

    @property
    def relation_signature(self) -> tuple[str, ...]:
        return tuple(step["relation"] for step in self.traverse)


@dataclass(frozen=True)
class Policy:
    version: int
    hop_limit: int
    rules: list[Rule]


def load_policy(path: Path = POLICY_PATH) -> Policy:
    data = yaml.safe_load(path.read_text())
    rules = [
        Rule(
            name=r["name"],
            trigger=r["trigger"],
            traverse=r.get("traverse", []),
            effect=r.get("effect", ""),
            target_flag=r.get("target_flag"),
        )
        for r in data["rules"]
    ]
    return Policy(version=data["version"], hop_limit=data["hop_limit"], rules=rules)


# --- Resolvers (the graph reads the propagation needs) -------------------------------------


class Resolver(Protocol):
    def funds_holding(self, companies: Iterable[str]) -> list[dict]: ...
    def supplier_dependents(self, company: str, hops: int) -> list[str]: ...
    def disclosures_covering(self, risk_title: str) -> list[dict]: ...
    def reports_deriving(self, item: dict) -> list[dict]: ...


class Neo4jResolver:
    """Resolver backed by a live Neo4jStore (or any object with `.run(query, **params)`).

    An optional `firm` scopes `funds_holding` to the funds that firm manages, so a change's downstream
    fund impact never bleeds across firms (a single issuer can be held by more than one firm's funds).
    Left `None` the resolver behaves exactly as before (all funds holding the changed companies).
    """

    def __init__(self, store: Any, firm: str | None = None) -> None:
        self.store = store
        self.firm = firm

    def funds_holding(self, companies: Iterable[str]) -> list[dict]:
        names = list(dict.fromkeys(companies))
        if not names:
            return []
        if self.firm:
            return self.store.run(
                "MATCH (f:Fund)-[:MANAGED_BY]->(:Company {name:$firm})\n"
                "MATCH (f)-[h:HOLDS]->(co:Company) WHERE co.name IN $names\n"
                "RETURN f.name AS fund, f.series_id AS series_id, co.name AS company,\n"
                "       h.weight_pct AS weight_pct ORDER BY fund, weight_pct DESC",
                names=names, firm=self.firm,
            )
        return self.store.run(
            "MATCH (f:Fund)-[h:HOLDS]->(co:Company) WHERE co.name IN $names\n"
            "RETURN f.name AS fund, f.series_id AS series_id, co.name AS company,\n"
            "       h.weight_pct AS weight_pct ORDER BY fund, weight_pct DESC",
            names=names,
        )

    def supplier_dependents(self, company: str, hops: int) -> list[str]:
        # {company} ∪ {dep : (dep)-[:SUPPLIES_TO*1..hops]->(company)} (seed: dependent->supplier)
        rows = self.store.run(
            f"MATCH (dep:Company)-[:SUPPLIES_TO*1..{int(hops)}]->(anchor:Company {{name:$company}})\n"
            "RETURN collect(DISTINCT dep.name) AS deps",
            company=company,
        )
        deps = rows[0]["deps"] if rows else []
        return list(dict.fromkeys([company, *deps]))

    def disclosures_covering(self, risk_title: str) -> list[dict]:
        return self.store.run(
            "MATCH (ds:DisclosureSection)-[:COVERS]->(rf:RiskFactor {title:$title})\n"
            "RETURN ds.key AS section_key, ds.form_ref AS form_ref, ds.item AS item,\n"
            "       ds.title AS title, rf.title AS risk_title ORDER BY section_key",
            title=risk_title,
        )

    def reports_deriving(self, item: dict) -> list[dict]:
        doc_id = item.get("doc_id")
        chunk_id = item.get("chunk_id")
        return self.store.run(
            "MATCH (r:GeneratedReport)-[df:DERIVED_FROM]->(src)\n"
            "WHERE src.doc_id = $doc_id OR src.chunk_id = $chunk_id\n"
            "RETURN DISTINCT r.report_id AS report_id, r.title AS title ORDER BY report_id",
            doc_id=doc_id, chunk_id=chunk_id,
        )


class DictResolver:
    """In-memory resolver (offline unit tests). `supplies` are (dependent, supplier) pairs."""

    def __init__(
        self,
        holds: list[tuple[str, str, float]],  # (fund, company, weight_pct)
        supplies: list[tuple[str, str]],  # (dependent, supplier)
        covers: list[tuple[str, str, str]],  # (section_key, form_ref, risk_title)
        series: dict[str, str] | None = None,  # fund -> series_id
        reports: list[dict] | None = None,
        section_meta: dict[str, dict] | None = None,  # section_key -> {item, title}
    ) -> None:
        self.holds = holds
        self.supplies = supplies
        self.covers = covers
        self.series = series or {}
        self.reports = reports or []
        self.section_meta = section_meta or {}

    def funds_holding(self, companies: Iterable[str]) -> list[dict]:
        wanted = set(companies)
        out = [
            {"fund": f, "series_id": self.series.get(f), "company": c, "weight_pct": w}
            for (f, c, w) in self.holds
            if c in wanted
        ]
        out.sort(key=lambda r: (r["fund"], -r["weight_pct"]))
        return out

    def supplier_dependents(self, company: str, hops: int) -> list[str]:
        # inbound BFS: find dep with (dep)-> ... ->(company) within `hops`
        inbound: dict[str, list[str]] = {}
        for dep, sup in self.supplies:
            inbound.setdefault(sup, []).append(dep)
        seen = {company}
        dq: deque[tuple[str, int]] = deque([(company, 0)])
        while dq:
            node, d = dq.popleft()
            if d >= hops:
                continue
            for dep in inbound.get(node, []):
                if dep not in seen:
                    seen.add(dep)
                    dq.append((dep, d + 1))
        return list(dict.fromkeys([company, *sorted(seen - {company})]))

    def disclosures_covering(self, risk_title: str) -> list[dict]:
        out = [
            {"section_key": k, "form_ref": fr, "risk_title": rt,
             "item": self.section_meta.get(k, {}).get("item"),
             "title": self.section_meta.get(k, {}).get("title")}
            for (k, fr, rt) in self.covers
            if rt == risk_title
        ]
        out.sort(key=lambda r: r["section_key"])
        return out

    def reports_deriving(self, item: dict) -> list[dict]:
        return list(self.reports)


# --- Fact-diff -----------------------------------------------------------------------------


def _identity(fact: dict) -> tuple:
    if fact["kind"] == "relation":
        period = fact.get("period") or (fact.get("qualifiers") or {}).get("period")
        return ("relation", fact["type"], fact["subject"], fact["object"], period)
    return ("entity", fact["label"], fact["key"])


def _payload(fact: dict) -> dict:
    """The value bag compared for 'changed' (identity already matched)."""
    return dict(fact.get("qualifiers") or {}) if fact["kind"] == "relation" else dict(fact.get("props") or {})


def _diff_item(fact: dict, change: str, old: dict | None, new: dict | None) -> dict:
    item = {
        "kind": fact["kind"],
        "change": change,
        "subject": fact.get("subject") if fact["kind"] == "relation" else fact.get("key"),
        "doc_id": (new or fact).get("doc_id"),
        "chunk_id": (new or fact).get("chunk_id"),
        "old_span": (old or {}).get("span"),
        "new_span": (new or {}).get("span"),
        "old_value": _payload(old) if old else None,
        "new_value": _payload(new) if new else None,
    }
    if fact["kind"] == "relation":
        item["type"] = fact["type"]
        item["object"] = fact["object"]
    else:
        item["label"] = fact["label"]
    return item


def diff_facts(v1_facts: list[dict], v2_facts: list[dict]) -> dict:
    """Deterministic triple-set diff → {added, removed, changed, counts}."""
    m1 = {_identity(f): f for f in v1_facts}
    m2 = {_identity(f): f for f in v2_facts}
    added = [_diff_item(f, "added", None, f) for idn, f in m2.items() if idn not in m1]
    removed = [_diff_item(f, "removed", f, None) for idn, f in m1.items() if idn not in m2]
    changed = [
        _diff_item(m2[idn], "changed", m1[idn], m2[idn])
        for idn in (m1.keys() & m2.keys())
        if _payload(m1[idn]) != _payload(m2[idn])
    ]
    _k = lambda i: (i.get("type") or i.get("label"), i["subject"], i.get("object") or "")  # noqa: E731
    added.sort(key=_k)
    removed.sort(key=_k)
    changed.sort(key=_k)
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "counts": {"added": len(added), "removed": len(removed), "changed": len(changed)},
    }


# --- Propagation engine (policy-driven) ----------------------------------------------------


def _trigger_matches(rule: Rule, item: dict) -> bool:
    t = rule.trigger
    if t.get("kind") != item["kind"]:
        return False
    if item["kind"] == "entity" and t.get("label") != item.get("label"):
        return False
    if item["kind"] == "relation" and t.get("type") != item.get("type"):
        return False
    return item["change"] in t.get("change", [])


def _item_citation(item: dict) -> dict:
    return {
        "change": item["change"],
        "predicate": item.get("type") or item.get("label"),
        "subject": item["subject"],
        "object": item.get("object"),
        "span": item.get("new_span") or item.get("old_span"),
        "doc_id": item.get("doc_id"),
        "chunk_id": item.get("chunk_id"),
    }


@dataclass
class _Accum:
    funds: dict[str, dict] = field(default_factory=dict)
    sections: dict[str, dict] = field(default_factory=dict)
    reports: dict[str, dict] = field(default_factory=dict)


def _apply_rule(rule: Rule, item: dict, resolver: Resolver, acc: _Accum, hop_limit: int) -> None:
    sig = rule.relation_signature
    cite = _item_citation(item)

    if sig == ("SUPPLIES_TO", "HOLDS"):
        hops = min(int(rule.traverse[0].get("hops", hop_limit)), hop_limit)
        deps = resolver.supplier_dependents(item["subject"], hops)
        for fh in resolver.funds_holding(deps):
            _add_fund(acc, fh, rule, cite, via=item["subject"])

    elif sig == ("HOLDS",):
        for fh in resolver.funds_holding([item["subject"]]):
            _add_fund(acc, fh, rule, cite, via=item["subject"])

    elif sig == ("COVERS",):
        for ds in resolver.disclosures_covering(item.get("object")):
            _add_section(acc, ds, rule, cite)

    elif sig == ("DERIVED_FROM",):
        for r in resolver.reports_deriving(item):
            _add_report(acc, r, rule, cite)

    else:  # pragma: no cover - defensive: a new policy traversal needs a resolver + handler
        raise NotImplementedError(
            f"impact_policy rule '{rule.name}' has unsupported traversal {sig}; "
            "add a Resolver method + handler (report as a contract change)"
        )


def _add_fund(acc: _Accum, fh: dict, rule: Rule, cite: dict, via: str) -> None:
    entry = acc.funds.setdefault(
        fh["fund"],
        {"fund": fh["fund"], "series_id": fh.get("series_id"), "via": set(),
         "rules": set(), "holdings": {}, "citations": []},
    )
    entry["via"].add(via)
    entry["rules"].add(rule.name)
    entry["holdings"][fh["company"]] = fh.get("weight_pct")
    if cite not in entry["citations"]:
        entry["citations"].append(cite)


def _add_section(acc: _Accum, ds: dict, rule: Rule, cite: dict) -> None:
    entry = acc.sections.setdefault(
        ds["section_key"],
        {"section_key": ds["section_key"], "form_ref": ds.get("form_ref"),
         "item": ds.get("item"), "title": ds.get("title"),
         "covers_risk": ds.get("risk_title"), "flag": rule.target_flag,
         "effect": rule.effect, "rules": set(), "citations": []},
    )
    entry["rules"].add(rule.name)
    if cite not in entry["citations"]:
        entry["citations"].append(cite)


def _add_report(acc: _Accum, r: dict, rule: Rule, cite: dict) -> None:
    entry = acc.reports.setdefault(
        r["report_id"],
        {"report_id": r["report_id"], "title": r.get("title"), "flag": rule.target_flag,
         "effect": rule.effect, "rules": set(), "citations": []},
    )
    entry["rules"].add(rule.name)
    if cite not in entry["citations"]:
        entry["citations"].append(cite)


def propagate(diff: dict, resolver: Resolver, policy: Policy | None = None) -> dict:
    """Apply the policy to a fact-diff → downstream impact set (funds, sections, reports)."""
    policy = policy or load_policy()
    acc = _Accum()
    for item in [*diff["added"], *diff["removed"], *diff["changed"]]:
        for rule in policy.rules:
            if _trigger_matches(rule, item):
                _apply_rule(rule, item, resolver, acc, policy.hop_limit)

    def _fund_out(e: dict) -> dict:
        return {
            "fund": e["fund"], "series_id": e["series_id"],
            "via": sorted(e["via"]), "rules": sorted(e["rules"]),
            "holdings": [{"company": c, "weight_pct": w} for c, w in sorted(e["holdings"].items())],
            "citations": e["citations"],
        }

    affected_funds = [_fund_out(e) for e in acc.funds.values()]
    affected_funds.sort(key=lambda f: f["fund"])
    stale_sections = [
        {**e, "rules": sorted(e["rules"])} for e in acc.sections.values()
    ]
    stale_sections.sort(key=lambda s: s["section_key"])
    out_of_date_reports = [
        {**e, "rules": sorted(e["rules"])} for e in acc.reports.values()
    ]
    out_of_date_reports.sort(key=lambda r: r["report_id"])
    return {
        "affected_funds": affected_funds,
        "stale_sections": stale_sections,
        "out_of_date_reports": out_of_date_reports,
        "summary": {
            "n_affected_funds": len(affected_funds),
            "n_stale_sections": len(stale_sections),
            "n_out_of_date_reports": len(out_of_date_reports),
        },
    }


# --- Fixtures + SUPERSEDES + full run ------------------------------------------------------


def load_fixture(path: str | Path) -> dict:
    p = Path(path)
    if not p.is_absolute() and not p.exists():
        p = FIXTURE_DIR / p
    return json.loads(p.read_text())


def diff_fixtures(v1: dict, v2: dict) -> dict:
    return diff_facts(v1.get("facts", []), v2.get("facts", []))


def record_supersedes(store: Any, new_doc: dict, old_doc_id: str) -> None:
    """Write Document-[:SUPERSEDES]->Document (new version supersedes the prior one)."""
    store.run(
        "MERGE (nd:Document {doc_id:$new_id})\n"
        "SET nd.title=$title, nd.filing_date=$filing_date, nd.doc_type=coalesce($doc_type, nd.doc_type)\n"
        "WITH nd MATCH (od:Document {doc_id:$old_id})\n"
        "MERGE (nd)-[:SUPERSEDES]->(od)",
        new_id=new_doc["doc_id"], title=new_doc.get("title"),
        filing_date=new_doc.get("filing_date"), doc_type=new_doc.get("doc_type"),
        old_id=old_doc_id,
    )


def apply_flags(store: Any, propagation: dict) -> dict:
    """Persist the target flags (stale:true / out_of_date:true) onto the graph nodes."""
    n_sections = n_reports = 0
    for s in propagation["stale_sections"]:
        flag = s.get("flag") or {}
        if flag:
            store.run(
                f"MATCH (ds:DisclosureSection {{key:$key}}) SET ds.`{flag['prop']}` = $val",
                key=s["section_key"], val=flag["value"],
            )
            n_sections += 1
    for r in propagation["out_of_date_reports"]:
        flag = r.get("flag") or {}
        if flag:
            store.run(
                f"MATCH (gr:GeneratedReport {{report_id:$rid}}) SET gr.`{flag['prop']}` = $val",
                rid=r["report_id"], val=flag["value"],
            )
            n_reports += 1
    return {"sections_flagged": n_sections, "reports_flagged": n_reports}


# --- Narration + SSE event -----------------------------------------------------------------


def _template_briefing(diff: dict, propagation: dict) -> str:
    c = diff["counts"]
    if c["added"] == c["removed"] == c["changed"] == 0:
        return "No fact changes detected between the two document versions — no downstream impact."
    funds = [f["fund"] for f in propagation["affected_funds"]]
    parts = [
        f"Detected {c['added']} added, {c['removed']} removed, {c['changed']} changed fact(s)."
    ]
    if funds:
        parts.append(f"Affected funds: {', '.join(funds)}.")
    else:
        parts.append("No funds are affected.")
    n_stale = propagation["summary"]["n_stale_sections"]
    if n_stale:
        secs = ", ".join(s["section_key"] for s in propagation["stale_sections"])
        parts.append(f"{n_stale} disclosure section(s) marked stale: {secs}.")
    n_reports = propagation["summary"]["n_out_of_date_reports"]
    if n_reports:
        parts.append(f"{n_reports} generated report(s) flagged out-of-date.")
    return " ".join(parts)


def impact_briefing(diff: dict, propagation: dict, provider: Any = None,
                    role: Role = Role.IMPACT) -> dict:
    """Cited impact briefing. Numbers are deterministic; the LLM only phrases the prose.

    If the provider call fails (e.g. zero-credit 400 in full mode), the deterministic narrative is
    still returned and `narration_unavailable` is set — never a 500 (bug #2).
    """
    narrative = _template_briefing(diff, propagation)
    llm_note = None
    narration_unavailable = None
    if provider is not None:
        system = (
            "You are a change-impact analyst. Summarise the supplied diff and downstream impact "
            "faithfully and concisely. Do not invent facts; cite only what is provided."
        )
        user = f"Diff counts: {diff['counts']}. Impact: {narrative}"
        try:
            completion = provider.complete(role=role, system=system,
                                           messages=[{"role": "user", "content": user}])
            llm_note = completion.text
            if llm_note and not llm_note.startswith("[fake:"):
                narrative = llm_note
        except Exception as exc:  # noqa: BLE001 - degrade gracefully; numbers already computed
            narration_unavailable = f"LLM narration unavailable: {exc}"
    return {
        "narrative": narrative,
        "llm_note": llm_note,
        "role": role.value,
        "narration_unavailable": narration_unavailable,
        "diff": diff,
        "impact": propagation,
    }


# --- UI collection projection (#4): GET /impact -> types.ts ImpactBriefing[] --------------------


def _fact_triples(items: list[dict]) -> list[dict]:
    """Map diff items -> types.ts FactTriple[] {subject, predicate, object, detail?}."""
    out = []
    for i in items:
        predicate = i.get("type") or i.get("label")
        if i["change"] == "changed":
            fields = sorted((i.get("new_value") or {}).keys())
            detail = f"changed: {', '.join(fields)}" if fields else "changed"
        else:
            nv = i.get("new_value") or {}
            detail = "; ".join(f"{k}={v}" for k, v in sorted(nv.items())) or None
        out.append({
            "subject": i["subject"], "predicate": predicate,
            "object": i.get("object") or "", "detail": detail,
        })
    return out


def impact_briefings(
    v1: dict, v2: dict, resolver: Resolver, policy: Policy | None = None,
) -> list[dict]:
    """Project a v1→v2 fixture pair into types.ts ImpactBriefing[] (a read; no LLM)."""
    diff = diff_fixtures(v1, v2)
    prop = propagate(diff, resolver, policy=policy)
    brief = impact_briefing(diff, prop, provider=None)  # deterministic summary, no LLM

    v1doc = v1.get("document", {})
    v2doc = v2.get("document", {})
    filing = v2doc.get("filing_date") or ""
    created_at = f"{filing}T00:00:00Z" if filing else ""
    rules = sorted(
        {r for f in prop["affected_funds"] for r in f["rules"]}
        | {r for s in prop["stale_sections"] for r in s["rules"]}
    )

    affected = []
    for f in prop["affected_funds"]:
        via = set(f.get("via") or [])
        held = {h["company"]: h["weight_pct"] for h in f["holdings"]}
        anchor = next((c for c in sorted(via) if c in held), None)
        hops = 1 if anchor else 2
        if anchor is not None:
            reason = f"holds {anchor} at {held[anchor]}% (inbound HOLDS, {hops} hop)"
        else:
            reason = (f"holds {', '.join(sorted(held))} in the affected supply chain "
                      f"({hops} hops)")
        affected.append({"fund": f["fund"], "reason": reason, "hops": hops})
    affected.sort(key=lambda a: a["fund"])

    stale = []
    for s in prop["stale_sections"]:
        stale.append({
            "form_ref": s.get("form_ref") or "", "item": s.get("item") or "",
            "title": s.get("title") or "",
            "reason": (f"COVERS the '{s.get('covers_risk')}' risk factor whose exposure changed"),
        })

    briefing = {
        "id": f"impact-{v1doc.get('doc_id', 'v1')}-to-{v2doc.get('doc_id', 'v2')}",
        "trigger_doc_id": v2doc.get("doc_id", ""),
        "trigger_title": v2doc.get("title", ""),
        "created_at": created_at,
        "rule": ", ".join(rules),
        "summary": brief["narrative"],
        "added": _fact_triples(diff["added"]),
        "removed": _fact_triples(diff["removed"]),
        "changed": _fact_triples(diff["changed"]),
        "affected_funds": affected,
        "stale_sections": stale,
    }
    return [briefing]


def make_impact_event(job_id: str, diff: dict, propagation: dict, seq: int = 0) -> SSEEvent:
    """Emit the frozen `impact` SSEEvent shape (contracts.events.EventType.IMPACT)."""
    return make_event(
        EventType.IMPACT,
        job_id,
        seq=seq,
        added=diff["counts"]["added"],
        removed=diff["counts"]["removed"],
        changed=diff["counts"]["changed"],
        affected_funds=[f["fund"] for f in propagation["affected_funds"]],
        stale_sections=[s["section_key"] for s in propagation["stale_sections"]],
        out_of_date_reports=[r["report_id"] for r in propagation["out_of_date_reports"]],
    )


def run_impact(
    resolver: Resolver, v1: dict, v2: dict, provider: Any = None,
    policy: Policy | None = None,
) -> dict:
    """End-to-end: diff two fixture versions, propagate, and narrate a cited briefing."""
    diff = diff_fixtures(v1, v2)
    propagation = propagate(diff, resolver, policy=policy)
    return impact_briefing(diff, propagation, provider=provider)
