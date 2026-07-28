"""Role -> model routing (the ONE place a model id appears).

Balanced profile (per the approved plan): Sonnet 5 for the heavy demo agents, Haiku 4.5 for the
light agents. Capability-aware — the 5-family takes `effort` (+ adaptive thinking) and rejects
`temperature`; Haiku takes `temperature=0` and rejects `effort`. Switching profile to
"quality-max" promotes the heavy agents to Opus 5 with no other code change.
"""

from __future__ import annotations

from dataclasses import dataclass

from .base import Role

SONNET5 = "claude-sonnet-5"
HAIKU45 = "claude-haiku-4-5"
OPUS5 = "claude-opus-5"


@dataclass(frozen=True)
class ModelSpec:
    model: str
    family: str  # "5" (Opus/Sonnet 5) | "haiku"
    effort: str | None = None  # 5-family only: low|medium|high|xhigh|max
    temperature: float | None = None  # Haiku only (5-family rejects sampling params)

    @property
    def supports_effort(self) -> bool:
        return self.family == "5"

    @property
    def supports_sampling(self) -> bool:
        return self.family == "haiku"


def _haiku() -> ModelSpec:
    return ModelSpec(HAIKU45, "haiku", temperature=0.0)


# Balanced profile.
_BALANCED: dict[Role, ModelSpec] = {
    Role.INTAKE: _haiku(),
    Role.EXTRACTION: ModelSpec(SONNET5, "5", effort="medium"),
    Role.RESOLUTION: ModelSpec(SONNET5, "5", effort="medium"),
    Role.STEWARD: _haiku(),
    Role.ROUTER: _haiku(),
    Role.ANALYST: ModelSpec(SONNET5, "5", effort="high"),
    Role.SYNTHESIZER: ModelSpec(SONNET5, "5", effort="medium"),
    Role.IMPACT: ModelSpec(SONNET5, "5", effort="high"),
}

# Quality-max overlay: promote the heavy (5-family) agents to Opus 5; keep Haiku agents.
_QUALITY_MAX: dict[Role, ModelSpec] = {
    role: (ModelSpec(OPUS5, "5", effort=spec.effort) if spec.family == "5" else spec)
    for role, spec in _BALANCED.items()
}

_PROFILES: dict[str, dict[Role, ModelSpec]] = {
    "balanced": _BALANCED,
    "quality-max": _QUALITY_MAX,
}


def resolve(role: Role, profile: str = "balanced") -> ModelSpec:
    try:
        return _PROFILES[profile][role]
    except KeyError as exc:  # pragma: no cover - defensive
        raise KeyError(f"no routing for role={role} profile={profile}") from exc


def all_models(profile: str = "balanced") -> set[str]:
    """Distinct model ids used by a profile (for the models.lock manifest)."""
    return {spec.model for spec in _PROFILES[profile].values()}
