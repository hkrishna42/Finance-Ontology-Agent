"""FIBO grounding + OWL-RL reasoning + SPARQL over a vendored FIBO OWL slice (offline)."""

from .grounding import FiboGrounding, ground
from .reasoner import ReasoningResult, validate

__all__ = ["FiboGrounding", "ground", "ReasoningResult", "validate"]
