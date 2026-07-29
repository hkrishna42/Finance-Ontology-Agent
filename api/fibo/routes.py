"""FIBO HTTP surface (APIRouter, prefix /fibo). Wired into api.main via include_router.

  * GET  /fibo/classes                  -> the curated FIBO class list (iri, curie, label, parents)
  * GET  /fibo/ground?label=&category=  -> the FIBO grounding for one entity label
  * POST /fibo/validate {instances}     -> OWL reasoning result (valid + violations)
  * POST /fibo/sparql {query}           -> SPARQL 1.1 results over the reasoned TBox
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from . import grounding, reasoner, sparql, tbox

router = APIRouter(prefix="/fibo", tags=["fibo"])


@router.get("/classes")
def list_classes() -> dict[str, Any]:
    """The curated FIBO classes we ground to, with their real IRIs + subclass parents."""
    return {"classes": tbox.all_classes(), "count": len(tbox.all_classes())}


@router.get("/ground")
def ground_label(
    label: str = Query(...),
    category: str | None = Query(default=None),
) -> dict[str, Any]:
    """Ground one ontology label to a FIBO class (deterministic; the per-instance default)."""
    g = grounding.ground(label, category=category)
    return {
        "label": label,
        "grounded": g.grounded,
        "class_iri": g.class_iri or None,
        "curie": g.curie or None,
        "refined": g.refined,
    }


class ValidateRequest(BaseModel):
    instances: list[dict[str, Any]]  # [{id, class_iri}]


@router.post("/validate")
def validate(req: ValidateRequest) -> dict[str, Any]:
    """Run OWL-RL reasoning over the supplied FIBO-typed instances; report consistency."""
    return reasoner.validate(req.instances).as_dict()


class SparqlRequest(BaseModel):
    query: str


@router.post("/sparql")
def run_sparql(req: SparqlRequest) -> dict[str, Any]:
    """Answer a read-only SPARQL query over the reasoned FIBO TBox."""
    try:
        return sparql.query(req.query)
    except sparql.SparqlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
