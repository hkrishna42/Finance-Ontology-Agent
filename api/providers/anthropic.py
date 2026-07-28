"""`AnthropicProvider` — the real `LLMProvider` (Claude via the Anthropic SDK).

Role → model is resolved through `registry.resolve(role, settings.routing_profile)`; agent code
never names a model. Capability-aware per the routing registry:

  * 5-family (Sonnet/Opus 5, `supports_effort`): `thinking={"type":"adaptive"}` +
    `output_config={"format": json_schema, "effort": spec.effort}`; **no** temperature.
  * Haiku (`supports_sampling`): `temperature=0` + `output_config={"format": json_schema}`;
    no effort/thinking.

The schema-card system prompt is prompt-cached when `cache_system=True` (an `ephemeral`
`cache_control` block). `bedrock` target is a thin branch (VPC deploy): `AnthropicBedrockMantle`
+ `anthropic.`-prefixed model ids. Not network-tested in M0 — importable, typed, mock-tested.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from . import registry
from .base import Completion, Message, Role, StructuredResult, Usage

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings


class ProviderError(RuntimeError):
    """Provider-side failure (refusal, empty content, bad JSON)."""


def _make_client(settings: Settings) -> Any:
    import anthropic

    if settings.anthropic_target == "bedrock":
        # VPC deploy: Bedrock-hosted Claude. Model ids are prefixed with "anthropic." below.
        return anthropic.AnthropicBedrockMantle(aws_region=settings.aws_region)
    # Reads ANTHROPIC_API_KEY from the environment.
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


class AnthropicProvider:
    """`LLMProvider` backed by the Anthropic Messages API."""

    def __init__(
        self,
        settings: Settings,
        client: Any = None,
        log_conn: Any = None,
    ) -> None:
        self.settings = settings
        self.client = client if client is not None else _make_client(settings)
        # Optional sqlite connection for the trace drawer (stores.sqlite.log_llm_call).
        self._log_conn = log_conn

    # -- helpers --------------------------------------------------------------------------

    def _model_id(self, model: str) -> str:
        if self.settings.anthropic_target == "bedrock":
            return f"anthropic.{model}"
        return model

    def _system_blocks(self, system: str, cache_system: bool) -> list[dict]:
        block: dict[str, Any] = {"type": "text", "text": system}
        if cache_system:
            block["cache_control"] = {"type": "ephemeral"}
        return [block]

    @staticmethod
    def _messages(messages: list[Message]) -> list[dict]:
        return [{"role": m["role"], "content": m["content"]} for m in messages]

    @staticmethod
    def _first_text(resp: Any) -> str:
        for block in getattr(resp, "content", []) or []:
            if getattr(block, "type", None) == "text":
                return block.text
        raise ProviderError("no text block in Anthropic response")

    @staticmethod
    def _usage(resp: Any) -> Usage:
        u = getattr(resp, "usage", None)
        if u is None:
            return Usage()
        return Usage(
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )

    def _log(self, role: Role, model: str, usage: Usage, stop_reason: str | None) -> None:
        if self._log_conn is None:
            return
        try:  # observability must never break the request path
            from ..stores import sqlite as sqlite_store

            sqlite_store.log_llm_call(
                self._log_conn,
                role=role.value,
                model=model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_input_tokens,
                cache_creation_tokens=usage.cache_creation_input_tokens,
                stop_reason=stop_reason,
            )
        except Exception:  # pragma: no cover - logging is best-effort
            pass

    # -- LLMProvider ----------------------------------------------------------------------

    def complete(
        self,
        *,
        role: Role,
        system: str,
        messages: list[Message],
        max_tokens: int = 1024,
    ) -> Completion:
        spec = registry.resolve(role, self.settings.routing_profile)
        model = self._model_id(spec.model)
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": self._system_blocks(system, cache_system=False),
            "messages": self._messages(messages),
        }
        if spec.supports_effort:
            kwargs["thinking"] = {"type": "adaptive"}
        elif spec.supports_sampling:
            kwargs["temperature"] = spec.temperature if spec.temperature is not None else 0.0

        resp = self.client.messages.create(**kwargs)
        if getattr(resp, "stop_reason", None) == "refusal":
            raise ProviderError(f"model refused (role={role.value}, model={model})")
        text = self._first_text(resp)
        usage = self._usage(resp)
        self._log(role, model, usage, getattr(resp, "stop_reason", None))
        return Completion(
            text=text, model=model, usage=usage, stop_reason=getattr(resp, "stop_reason", None)
        )

    def complete_structured(
        self,
        *,
        role: Role,
        schema: dict,
        system: str,
        messages: list[Message],
        max_tokens: int = 2048,
        cache_system: bool = False,
    ) -> StructuredResult:
        spec = registry.resolve(role, self.settings.routing_profile)
        model = self._model_id(spec.model)
        fmt = {"type": "json_schema", "schema": schema}
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": self._system_blocks(system, cache_system),
            "messages": self._messages(messages),
        }
        if spec.supports_effort:
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"format": fmt, "effort": spec.effort}
        elif spec.supports_sampling:
            kwargs["temperature"] = spec.temperature if spec.temperature is not None else 0.0
            kwargs["output_config"] = {"format": fmt}
        else:  # pragma: no cover - defensive
            kwargs["output_config"] = {"format": fmt}

        resp = self.client.messages.create(**kwargs)
        if getattr(resp, "stop_reason", None) == "refusal":
            raise ProviderError(f"model refused (role={role.value}, model={model})")
        text = self._first_text(resp)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"model returned non-JSON structured output: {exc}") from exc
        usage = self._usage(resp)
        self._log(role, model, usage, getattr(resp, "stop_reason", None))
        return StructuredResult(
            data=data,
            raw=text,
            model=model,
            usage=usage,
            stop_reason=getattr(resp, "stop_reason", None),
        )
