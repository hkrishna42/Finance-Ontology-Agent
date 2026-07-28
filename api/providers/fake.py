"""Deterministic, offline providers for CI, tests, and `stub` mode.

`FakeProvider` implements `LLMProvider` with no network / no key:
  * `complete_structured` returns a **schema-valid** object for the supplied JSON Schema
    (the minimal valid instance built from `required` + types; for the extraction schema that
    is `{"entities": [], "relations": []}`), unless a fixture is present.
  * `complete` returns a canned deterministic string (`"[fake:{role}]"`), unless a fixture is present.

`CassetteProvider` wraps a real `LLMProvider`: it keys each call by the sha256 of
`(role, system, messages, schema)` and replays `<dir>/<key>.json` when present. In `record` mode
it calls the inner provider on a miss and writes the response; in replay mode a miss raises.

Fixture / cassette key
----------------------
`sha256(json({role, system, messages, schema}))` — stable across runs (sorted keys). Text
completions use `schema=None`, so a text and a structured call never collide on the same key.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .base import Completion, Message, Role, StructuredResult, Usage

DEFAULT_FIXTURE_DIR = "fixtures/fake"


def call_key(
    role: Role, system: str, messages: list[Message], schema: dict | None
) -> str:
    """Stable sha256 over the semantically-identifying inputs of a completion call."""
    payload = json.dumps(
        {
            "role": role.value,
            "system": system,
            "messages": list(messages),
            "schema": schema,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def minimal_valid_instance(schema: dict) -> Any:
    """Build the smallest instance that validates against a (subset of) JSON Schema.

    Handles object/array/string/number/integer/boolean/null, `enum`, and `const`. Only
    `required` object properties are populated (safe under `additionalProperties: false`), and
    arrays honour `minItems` (default 0 → empty list).
    """
    if not isinstance(schema, dict):
        return None
    if schema.get("enum"):
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]

    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), t[0] if t else "null")

    if t == "object":
        obj: dict[str, Any] = {}
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            obj[name] = minimal_valid_instance(props.get(name, {}))
        return obj
    if t == "array":
        min_items = int(schema.get("minItems", 0))
        item_schema = schema.get("items", {})
        return [minimal_valid_instance(item_schema) for _ in range(min_items)]
    if t == "string":
        return ""
    if t in ("number", "integer"):
        return 0
    if t == "boolean":
        return False
    return None


def _serialize_structured(r: StructuredResult) -> dict:
    return {
        "data": r.data,
        "raw": r.raw,
        "model": r.model,
        "usage": vars(r.usage),
        "stop_reason": r.stop_reason,
    }


def _deserialize_structured(d: dict) -> StructuredResult:
    return StructuredResult(
        data=d["data"],
        raw=d.get("raw", json.dumps(d["data"])),
        model=d.get("model", "cassette"),
        usage=Usage(**d.get("usage", {})),
        stop_reason=d.get("stop_reason"),
    )


def _serialize_completion(c: Completion) -> dict:
    return {
        "text": c.text,
        "model": c.model,
        "usage": vars(c.usage),
        "stop_reason": c.stop_reason,
    }


def _deserialize_completion(d: dict) -> Completion:
    return Completion(
        text=d["text"],
        model=d.get("model", "cassette"),
        usage=Usage(**d.get("usage", {})),
        stop_reason=d.get("stop_reason"),
    )


class FakeProvider:
    """Deterministic `LLMProvider` for offline CI / stub mode."""

    def __init__(self, fixture_dir: str | Path | None = DEFAULT_FIXTURE_DIR) -> None:
        self.fixture_dir = Path(fixture_dir) if fixture_dir else None

    def complete(
        self,
        *,
        role: Role,
        system: str,
        messages: list[Message],
        max_tokens: int = 1024,
    ) -> Completion:
        key = call_key(role, system, messages, None)
        if self.fixture_dir is not None:
            fpath = self.fixture_dir / f"{key}.json"
            if fpath.exists():
                return _deserialize_completion(json.loads(fpath.read_text()))
        return Completion(
            text=f"[fake:{role.value}]", model="fake", usage=Usage(), stop_reason="end_turn"
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
        key = call_key(role, system, messages, schema)
        if self.fixture_dir is not None:
            fpath = self.fixture_dir / f"{key}.json"
            if fpath.exists():
                return _deserialize_structured(json.loads(fpath.read_text()))
        data = minimal_valid_instance(schema)
        return StructuredResult(
            data=data,
            raw=json.dumps(data, separators=(",", ":")),
            model="fake",
            usage=Usage(),
            stop_reason="end_turn",
        )


class CassetteMiss(RuntimeError):
    """Raised when a cassette is required (replay mode) but no recording exists for the key."""


class CassetteProvider:
    """Record/replay wrapper around a real `LLMProvider` for deterministic offline evals."""

    def __init__(self, inner: Any, cassette_dir: str | Path, record: bool = False) -> None:
        self.inner = inner
        self.cassette_dir = Path(cassette_dir)
        self.record = record

    def _path(self, key: str) -> Path:
        return self.cassette_dir / f"{key}.json"

    def complete(
        self,
        *,
        role: Role,
        system: str,
        messages: list[Message],
        max_tokens: int = 1024,
    ) -> Completion:
        key = call_key(role, system, messages, None)
        fpath = self._path(key)
        if fpath.exists():
            return _deserialize_completion(json.loads(fpath.read_text()))
        if not self.record:
            raise CassetteMiss(
                f"cassette miss for complete key={key} in {self.cassette_dir} "
                f"(role={role.value}); run with record=True to create it"
            )
        result = self.inner.complete(
            role=role, system=system, messages=messages, max_tokens=max_tokens
        )
        self.cassette_dir.mkdir(parents=True, exist_ok=True)
        fpath.write_text(json.dumps(_serialize_completion(result), indent=2))
        return result

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
        key = call_key(role, system, messages, schema)
        fpath = self._path(key)
        if fpath.exists():
            return _deserialize_structured(json.loads(fpath.read_text()))
        if not self.record:
            raise CassetteMiss(
                f"cassette miss for complete_structured key={key} in {self.cassette_dir} "
                f"(role={role.value}); run with record=True to create it"
            )
        result = self.inner.complete_structured(
            role=role,
            schema=schema,
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            cache_system=cache_system,
        )
        self.cassette_dir.mkdir(parents=True, exist_ok=True)
        fpath.write_text(json.dumps(_serialize_structured(result), indent=2))
        return result
