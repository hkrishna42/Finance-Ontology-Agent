"""Ontology v0 — the single source of truth for the Firm Ontology Platform.

One declarative spec (ENTITY_SPECS + RELATION_SPECS) drives THREE derived artifacts so
nothing can drift:

    extraction_json_schema()  -> the JSON Schema the Extraction agent must emit per chunk
                                 (Anthropic `output_config.format`); only *extractable* types.
    neo4j_ddl(dim)            -> Cypher constraints + the :Chunk vector index (dim 384).
    schema_card()             -> a compact textual ontology description injected into prompts.

Layers (see the build plan): L1 market/issuer knowledge, L2 firm structure (funds/holdings),
L3 obligations/outputs (forms/disclosures/reports), infra (documents/chunks/reports).

Structural vs extractable
-------------------------
Relations and entities carry `extractable`. LLM extraction only ever produces the extractable
subset; the rest (HOLDS from N-PORT, MANAGED_BY, SUBJECT_TO, DERIVED_FROM, MENTIONS, Fund,
RegulatoryForm, GeneratedReport, Document, Chunk) are written by deterministic code. This is the
first line of the reliability story: the model is not even *allowed* to invent structural facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# --- Embedding / vector config (kept here so DDL and the embedder agree) --------------------

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
VECTOR_DIM = 384
VECTOR_SIMILARITY = "cosine"


# --- Controlled vocabularies ---------------------------------------------------------------


class Layer(str, Enum):
    L1 = "L1"  # market & issuer knowledge
    L2 = "L2"  # firm structure
    L3 = "L3"  # obligations & outputs
    INFRA = "infra"


class RiskCategory(str, Enum):
    supply_chain = "supply_chain"
    customer_concentration = "customer_concentration"
    regulatory = "regulatory"
    geopolitical = "geopolitical"
    technology = "technology"
    litigation = "litigation"
    other = "other"


class EventType(str, Enum):
    acquisition = "acquisition"
    partnership = "partnership"
    litigation = "litigation"
    product_launch = "product_launch"
    guidance = "guidance"
    other = "other"


class Criticality(str, Enum):
    critical = "critical"
    named = "named"
    mentioned = "mentioned"


class Sensitivity(str, Enum):
    public = "public"
    internal = "internal"


# --- Declarative ontology spec -------------------------------------------------------------


@dataclass(frozen=True)
class EntitySpec:
    label: str
    key: str  # property used as the MERGE key / uniqueness constraint
    props: tuple[str, ...]
    layer: Layer
    description: str
    extractable: bool  # produced by the Extraction agent from text?
    fibo_class: str | None = None  # default FIBO grounding (curie); see api/fibo/grounding.py


@dataclass(frozen=True)
class RelationSpec:
    type: str
    domain: tuple[str, ...]  # allowed source labels
    range: tuple[str, ...]  # allowed target labels
    qualifiers: tuple[str, ...]
    description: str
    extractable: bool


ENTITY_SPECS: tuple[EntitySpec, ...] = (
    # -- L1: extractable from filings ------------------------------------------------------
    EntitySpec("Company", "name", ("aliases", "cik", "ticker", "lei", "country"), Layer.L1,
               "A corporation / issuer.", True, fibo_class="cmns-org:LegalEntity"),
    EntitySpec("Person", "name", ("aliases",), Layer.L1,
               "A named individual (director, officer, alum).", True),
    EntitySpec("Product", "name", ("category",), Layer.L1,
               "A product or product line.", True),
    EntitySpec("BusinessSegment", "name", (), Layer.L1,
               "A reportable business segment.", True),
    EntitySpec("Region", "name", ("iso_code",), Layer.L1,
               "A geography of operation.", True),
    EntitySpec("RiskFactor", "title", ("category",), Layer.L1,
               "A discrete risk factor (category ∈ RiskCategory).", True),
    EntitySpec("MetricObservation", "key", ("metric", "value", "unit", "period", "basis"),
               Layer.L1, "A reported metric value for a period (key = metric|period|basis).",
               True),
    EntitySpec("Event", "key", ("type", "date"), Layer.L1,
               "A discrete corporate event (key = type|date|subject; type ∈ EventType).", True),
    # -- L3: DisclosureSection is extracted from the prospectus risk section ---------------
    EntitySpec("DisclosureSection", "key", ("form_ref", "item", "title"), Layer.L3,
               "A section of a regulatory disclosure (key = form_ref|item).", True),
    # -- L2 / L3 / infra: structural, written by deterministic code -------------------------
    EntitySpec("Fund", "name", ("series_id", "cik", "lei"), Layer.L2,
               "A registered fund (the series).", False),
    # -- L2: real-estate master data (structural; written by the MDM publish + seed, not the LLM) --
    EntitySpec("RealProperty", "name",
               ("address", "city", "country", "property_type", "rentable_area"), Layer.L2,
               "A parcel of real property (building / land).", False,
               fibo_class="fibo-fnd-plc-rp:RealProperty"),
    EntitySpec("Portfolio", "name", ("strategy", "base_currency"), Layer.L2,
               "A property portfolio a fund holds.", False,
               fibo_class="fibo-sec-sec-pls:CollectiveInvestmentVehicle"),
    EntitySpec("Lease", "key", ("tenant", "start_date", "end_date", "annual_rent"), Layer.L2,
               "A lease over a property (key = property|tenant|start).", False,
               fibo_class="fibo-fnd-agr-ctr:Contract"),
    EntitySpec("Loan", "key", ("principal", "rate", "maturity", "servicer", "currency"), Layer.L2,
               "A commercial real-estate loan (key = borrower|property|origination).", False,
               fibo_class="fibo-loan-ln-ln:Loan"),
    EntitySpec("Valuation", "key", ("value", "currency", "as_of", "method"), Layer.L2,
               "A property valuation (key = property|as_of|source).", False,
               fibo_class="fibo-fnd-plc-rp:RealPropertyAppraisal"),
    EntitySpec("RegulatoryForm", "form", ("frequency",), Layer.L3,
               "A regulatory form the firm/issuer is subject to.", False),
    EntitySpec("GeneratedReport", "report_id", ("title", "period", "sha256", "created_at"),
               Layer.L3, "A generated report pack (audit-trail node).", False),
    EntitySpec("Document", "doc_id",
               ("doc_type", "filing_date", "accession_no", "sensitivity", "title", "url"),
               Layer.INFRA, "An ingested source document.", False),
    EntitySpec("Chunk", "chunk_id", ("doc_id", "text", "page", "start", "end", "embedding"),
               Layer.INFRA, "A chunk of a document (carries the embedding vector).", False),
)


RELATION_SPECS: tuple[RelationSpec, ...] = (
    # -- Extractable (produced by the Extraction agent) ------------------------------------
    RelationSpec("SUPPLIES_TO", ("Company",), ("Company",), ("criticality", "component"),
                 "Source supplies a component to target (criticality ∈ Criticality).", True),
    RelationSpec("CUSTOMER_OF", ("Company",), ("Company",), ("est_revenue_share",),
                 "Source is a customer of target.", True),
    RelationSpec("COMPETES_WITH", ("Company",), ("Company",), ("market",),
                 "Source competes with target.", True),
    RelationSpec("SUBSIDIARY_OF", ("Company",), ("Company",), (),
                 "Source is a subsidiary of target.", True),
    RelationSpec("PARTNER_OF", ("Company",), ("Company",), ("nature",),
                 "Source partners with target.", True),
    RelationSpec("ACQUIRED", ("Company",), ("Company",), ("status", "value"),
                 "Source acquired target (status ∈ announced|closed).", True),
    RelationSpec("INVESTS_IN", ("Company",), ("Company",), (),
                 "Source holds an equity/strategic investment in target.", True),
    RelationSpec("OFFICER_OF", ("Person",), ("Company",), ("title", "since"),
                 "Person is an officer of the company.", True),
    RelationSpec("DIRECTOR_OF", ("Person",), ("Company",), ("committee", "since"),
                 "Person is a director of the company.", True),
    RelationSpec("FORMERLY_AT", ("Person",), ("Company",), ("role", "years"),
                 "Person was formerly at the company (alumni/interlock paths).", True),
    RelationSpec("HAS_SEGMENT", ("Company",), ("BusinessSegment",), (),
                 "Company reports the business segment.", True),
    RelationSpec("SELLS", ("Company",), ("Product",), (),
                 "Company sells the product.", True),
    RelationSpec("OPERATES_IN", ("Company",), ("Region",), ("activity",),
                 "Company operates in the region.", True),
    RelationSpec("EXPOSED_TO", ("Company",), ("RiskFactor",), ("severity_language",),
                 "Company is exposed to the risk factor (verbatim hedge language).", True),
    RelationSpec("REPORTED", ("Company",), ("MetricObservation",), (),
                 "Company reported the metric observation.", True),
    RelationSpec("COVERS", ("DisclosureSection",), ("RiskFactor",), (),
                 "A prospectus disclosure section covers the risk factor.", True),
    # -- Structural (written by deterministic modules) -------------------------------------
    RelationSpec("HOLDS", ("Fund",), ("Company",),
                 ("weight_pct", "value_usd", "shares", "as_of", "source"),
                 "Fund holds the issuer (from N-PORT; method=structured_parse).", False),
    RelationSpec("MANAGED_BY", ("Fund",), ("Company",), (),
                 "Fund is managed by the firm.", False),
    RelationSpec("SUBJECT_TO", ("Fund", "Company"), ("RegulatoryForm",), (),
                 "Fund/company is subject to the regulatory form.", False),
    RelationSpec("DERIVED_FROM", ("GeneratedReport",), ("Document", "Chunk"),
                 ("query", "as_of"),
                 "A generated report derives from facts/queries (audit trail).", False),
    # -- Real-estate structural edges (MDM publish + seed) ---------------------------------
    RelationSpec("HOLDS_PORTFOLIO", ("Fund", "Company"), ("Portfolio",), (),
                 "A fund/manager holds a property portfolio.", False),
    RelationSpec("CONTAINS_PROPERTY", ("Portfolio", "Fund"), ("RealProperty",), ("weight_pct",),
                 "A portfolio contains a property.", False),
    RelationSpec("LEASES_SPACE_AT", ("Company",), ("RealProperty",), ("sq_ft",),
                 "A tenant leases space at a property.", False),
    RelationSpec("COLLATERALIZED_BY", ("Loan",), ("RealProperty",), (),
                 "A loan is collateralized by a property.", False),
    RelationSpec("HAS_VALUATION", ("RealProperty",), ("Valuation",), (),
                 "A property has a valuation.", False),
    RelationSpec("SERVICED_BY", ("Loan",), ("Company",), (),
                 "A loan is serviced by a company.", False),
    RelationSpec("MENTIONS", ("Chunk",),
                 tuple(e.label for e in ENTITY_SPECS if e.extractable),
                 (), "A chunk mentions an entity (provenance).", False),
)

# Provenance carried on every *semantic* (non-infra) edge write.
PROVENANCE_FIELDS: tuple[str, ...] = (
    "doc_id", "chunk_id", "page", "span", "confidence",
    "as_of", "reported_at", "extractor_model", "sensitivity",
)


# --- Lookups -------------------------------------------------------------------------------


def entity_labels() -> list[str]:
    return [e.label for e in ENTITY_SPECS]


def relation_types() -> list[str]:
    return [r.type for r in RELATION_SPECS]


def extractable_entity_labels() -> list[str]:
    return [e.label for e in ENTITY_SPECS if e.extractable]


def extractable_relation_types() -> list[str]:
    return [r.type for r in RELATION_SPECS if r.extractable]


def entity_spec(label: str) -> EntitySpec:
    for e in ENTITY_SPECS:
        if e.label == label:
            return e
    raise KeyError(f"unknown entity label: {label}")


def relation_spec(rtype: str) -> RelationSpec:
    for r in RELATION_SPECS:
        if r.type == rtype:
            return r
    raise KeyError(f"unknown relation type: {rtype}")


# --- Derived artifact 1: extraction JSON Schema --------------------------------------------


def extraction_json_schema() -> dict:
    """JSON Schema for one chunk's extraction output (Anthropic output_config.format).

    Note: Anthropic's json_schema ignores numeric range constraints, so `confidence ∈ [0,1]`
    is documented here but enforced downstream in the grounding gate / steward.
    """
    entity = {
        "type": "object",
        "additionalProperties": False,
        "required": ["label", "name", "span", "confidence"],
        "properties": {
            "label": {"type": "string", "enum": extractable_entity_labels()},
            "name": {"type": "string", "description": "Canonical mention text."},
            "aliases": {"type": "array", "items": {"type": "string"}},
            "category": {"type": "string",
                         "description": "For RiskFactor: a RiskCategory; for Product: free text."},
            "attributes": {
                "type": "array",
                "description": "Optional label-specific properties as {key, value} pairs "
                "(e.g. key='cik'; key='ticker'; key='period').",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["key", "value"],
                    "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
                },
            },
            "span": {"type": "string",
                     "description": "VERBATIM source sentence supporting this entity."},
            "confidence": {"type": "number", "description": "0.0–1.0."},
        },
    }
    relation = {
        "type": "object",
        "additionalProperties": False,
        "required": ["type", "subject", "object", "span", "confidence"],
        "properties": {
            "type": {"type": "string", "enum": extractable_relation_types()},
            "subject": {"type": "string", "description": "Source entity name."},
            "object": {"type": "string", "description": "Target entity name."},
            "qualifiers": {
                "type": "array",
                "description": "Optional edge qualifiers as {key, value} pairs "
                "(e.g. key='criticality', value='critical'; key='component', value='foundry').",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["key", "value"],
                    "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
                },
            },
            "span": {"type": "string",
                     "description": "VERBATIM source sentence supporting this relation."},
            "confidence": {"type": "number", "description": "0.0–1.0."},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["entities", "relations"],
        "properties": {
            "entities": {"type": "array", "items": entity},
            "relations": {"type": "array", "items": relation},
        },
    }


# --- Derived artifact 2: Neo4j DDL ---------------------------------------------------------


def neo4j_ddl(dim: int = VECTOR_DIM) -> list[str]:
    """Uniqueness constraints per entity key + the :Chunk vector index."""
    stmts: list[str] = []
    for e in ENTITY_SPECS:
        cname = f"unique_{e.label.lower()}_{e.key}"
        stmts.append(
            f"CREATE CONSTRAINT {cname} IF NOT EXISTS "
            f"FOR (n:{e.label}) REQUIRE n.{e.key} IS UNIQUE"
        )
    stmts.append(
        "CREATE VECTOR INDEX chunk_embedding IF NOT EXISTS "
        "FOR (c:Chunk) ON (c.embedding) "
        f"OPTIONS {{indexConfig: {{`vector.dimensions`: {dim}, "
        f"`vector.similarity_function`: '{VECTOR_SIMILARITY}'}}}}"
    )
    stmts.append(
        "CREATE FULLTEXT INDEX chunk_text IF NOT EXISTS "
        "FOR (c:Chunk) ON EACH [c.text]"
    )
    # Non-unique index backing canonical-identity dedup: Company mentions snap onto an existing node
    # whose normalized name matches (see api/ingest/pipeline.py::_canonical_company_names).
    stmts.append(
        "CREATE INDEX company_norm IF NOT EXISTS FOR (c:Company) ON (c.norm)"
    )
    return stmts


# --- Derived artifact 3: prompt schema-card ------------------------------------------------


def schema_card() -> str:
    """Compact ontology description injected (and prompt-cached) into extraction prompts."""
    lines = ["ENTITY TYPES (label — key — description):"]
    for e in ENTITY_SPECS:
        if not e.extractable:
            continue
        lines.append(f"  {e.label} — key:{e.key} — {e.description}")
    lines.append("")
    lines.append("RELATION TYPES (TYPE: Domain -> Range [qualifiers] — description):")
    for r in RELATION_SPECS:
        if not r.extractable:
            continue
        dom = "|".join(r.domain)
        rng = "|".join(r.range)
        q = f" [{', '.join(r.qualifiers)}]" if r.qualifiers else ""
        lines.append(f"  {r.type}: {dom} -> {rng}{q} — {r.description}")
    lines.append("")
    lines.append("CONTROLLED VOCABULARY:")
    lines.append("  RiskFactor.category ∈ " + ", ".join(c.value for c in RiskCategory))
    lines.append("  Event.type ∈ " + ", ".join(t.value for t in EventType))
    lines.append("  SUPPLIES_TO.criticality ∈ " + ", ".join(c.value for c in Criticality))
    lines.append("")
    lines.append(
        "RULES: Only emit the entity/relation types above. Every entity and relation MUST "
        "include a `span`: the VERBATIM sentence from the chunk that supports it (used by a "
        "grounding gate — invented spans are dropped). Set `confidence` in [0,1]. Do not infer "
        "facts not stated in the chunk."
    )
    return "\n".join(lines)


# --- Pydantic envelope for parsing / validating agent output -------------------------------

# Imported lazily so the declarative spec above stays import-light for codegen consumers.


@dataclass
class OntologyInfo:
    """Small summary object used by /ontology endpoints and tests."""

    entity_count: int = field(default_factory=lambda: len(ENTITY_SPECS))
    relation_count: int = field(default_factory=lambda: len(RELATION_SPECS))
    extractable_entities: int = field(
        default_factory=lambda: len(extractable_entity_labels())
    )
    extractable_relations: int = field(
        default_factory=lambda: len(extractable_relation_types())
    )
    vector_dim: int = VECTOR_DIM
    embed_model: str = EMBED_MODEL
