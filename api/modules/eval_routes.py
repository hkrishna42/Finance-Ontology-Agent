"""M8 Evaluation surface (APIRouter, prefix /eval). Wired into api.main via include_router.

Serves the offline quality-gate catalog as LIVE `EvalCard[]` (types.ts) so the Evaluation panel
reads it from the API instead of falling back to the committed web fixture (web/src/fixtures/eval.json).

The eval harness (M8) is not wired yet, so the precision/recall/leakage numbers are honest `null`
placeholders carried under `status: "placeholder"`; when a run lands they become real values with
`status: "wired"` and the card/metric shape is unchanged. The one non-null value is the grounding
similarity threshold actually in effect in the backend (`grounding.DEFAULT_THRESHOLD`), so the panel
shows this is live backend state rather than a static fixture copy.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..extract.grounding import DEFAULT_THRESHOLD

router = APIRouter(prefix="/eval", tags=["eval"])


def eval_cards() -> list[dict[str, Any]]:
    """The live eval-gate catalog as `EvalCard[]` (types.ts EvalCard / EvalMetric).

    Values are `null` (pending) until the M8 harness reports a run; `grounding.threshold` is the
    real similarity threshold in effect, surfaced so the card set is demonstrably live backend data.
    """
    return [
        {
            "id": "extraction_pr",
            "title": "Extraction precision / recall",
            "description": (
                "Entity + relation extraction vs. a hand-labelled golden set. Wired to the M8 eval "
                "harness; values below are pending the first run."
            ),
            "status": "placeholder",
            "metrics": [
                {"label": "Entity precision", "value": None, "unit": "%", "target": 90},
                {"label": "Entity recall", "value": None, "unit": "%", "target": 80},
                {"label": "Relation precision", "value": None, "unit": "%", "target": 85},
                {"label": "Relation recall", "value": None, "unit": "%", "target": 70},
            ],
        },
        {
            "id": "grounding",
            "title": "Grounding gate - span faithfulness",
            "description": (
                "Share of extracted facts whose verbatim span is found in the source chunk "
                "(invented spans are dropped). The gate runs live; the pass rate is pending M8."
            ),
            "status": "placeholder",
            "metrics": [
                {"label": "Spans verified", "value": None, "unit": "%", "target": 100},
                {"label": "Facts dropped (ungrounded)", "value": None, "unit": "%"},
                # Live backend state: the grounding similarity threshold actually in effect.
                {"label": "Grounding threshold (in effect)", "value": DEFAULT_THRESHOLD, "target": 1},
            ],
        },
        {
            "id": "vector_vs_graph",
            "title": "Vector RAG vs. Graph answer",
            "description": (
                "Head-to-head on the hero question set - multi-hop correctness, aggregation, and "
                "citation coverage. Wired in M8."
            ),
            "status": "placeholder",
            "metrics": [
                {"label": "Graph multi-hop correct", "value": None, "unit": "/ 12"},
                {"label": "Vector multi-hop correct", "value": None, "unit": "/ 12"},
                {"label": "Citation coverage (graph)", "value": None, "unit": "%", "target": 100},
            ],
        },
        {
            "id": "entitlement",
            "title": "Entitlement wall - leakage",
            "description": (
                "Confirms zero internal-sensitivity chunks leak into answers when the wall is on. "
                "Wired in M8."
            ),
            "status": "placeholder",
            "metrics": [
                {"label": "Internal-source leaks", "value": None, "unit": "count", "target": 0},
                {"label": "Withheld correctly surfaced", "value": None, "unit": "%", "target": 100},
            ],
        },
    ]


@router.get("")
def get_eval() -> list[dict[str, Any]]:
    """GET /eval -> types.ts `EvalCard[]` (live). A bare array; the panel renders it directly."""
    return eval_cards()
