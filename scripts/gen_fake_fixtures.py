#!/usr/bin/env python
"""Generate deterministic FakeProvider fixtures under fixtures/fake/.

FakeProvider (stub mode) replays `fixtures/fake/<call_key>.json` when a call matches; otherwise it
returns the schema's minimal-valid instance. These fixtures let the OFFLINE stub pipeline produce a
*meaningful* result for the two graph-worker LLM steps that otherwise default to a conservative
"no":

  * resolution adjudication (Role.RESOLUTION) — confirm a fuzzy NVIDIA mention → CIK 0001045810
  * steward entailment      (Role.STEWARD)    — approve a low-confidence but well-grounded triple

Keys are derived from the exact (role, system, messages, schema) via `api.providers.fake.call_key`,
using the SAME prompt builders the resolver/steward call at runtime, so they never drift. Re-run
after changing those prompts:  uv run python scripts/gen_fake_fixtures.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.providers.fake import call_key  # noqa: E402
from api.resolution.resolver import Candidate, Resolver  # noqa: E402
from api.resolution.spine import Spine  # noqa: E402
from api.steward.steward import CandidateTriple, Steward  # noqa: E402

FIXTURE_DIR = ROOT / "fixtures" / "fake"

# The canonical adjudication scenario (must match tests/test_resolution_pipeline.py).
ADJ_MENTION = "Nvidia Corp (the chip maker)"
ADJ_CANDIDATE = Candidate(cik="0001045810", title="NVIDIA CORP", ticker="NVDA", score=1.0)

# The canonical entailment scenario (must match tests/test_steward_review.py).
ENTAIL_TRIPLE = CandidateTriple(
    subject_label="Company",
    subject_key="Contoso Foundry",
    rel_type="SUPPLIES_TO",
    object_label="Company",
    object_key="Fabrikam Systems",
    qualifiers={"criticality": "critical"},
    provenance={
        "span": "Contoso Foundry is the sole supplier of critical wafers to Fabrikam Systems.",
        "doc_id": "doc_entail",
        "as_of": "2026-05-31",
        "sensitivity": "public",
    },
    confidence=0.4,
)


def _write(call: dict, data: dict) -> Path:
    key = call_key(call["role"], call["system"], call["messages"], call["schema"])
    payload = {
        "data": data,
        "raw": json.dumps(data, separators=(",", ":")),
        "model": "fake",
        "usage": {},
        "stop_reason": "end_turn",
    }
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / f"{key}.json"
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def main() -> int:
    resolver = Resolver(spine=Spine.load(), provider=object(), embedder=object())
    adj_call = resolver.adjudication_call(ADJ_MENTION, [ADJ_CANDIDATE])
    p1 = _write(adj_call, {"match": True, "cik": "0001045810", "confidence": 0.97})

    steward = Steward(provider=object())
    entail_call = steward.entailment_call(ENTAIL_TRIPLE)
    p2 = _write(entail_call, {"entailed": True, "confidence": 0.86})

    print(f"wrote {p1.relative_to(ROOT)}")
    print(f"wrote {p2.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
