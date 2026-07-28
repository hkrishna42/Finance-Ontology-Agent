"""Provider tests: FakeProvider contract, cassette record→replay, and routing registry."""

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator

from api.ontology.schema import extraction_json_schema
from api.providers import registry
from api.providers.base import LLMProvider, Role
from api.providers.fake import CassetteMiss, CassetteProvider, FakeProvider


def test_fake_provider_is_llm_provider():
    p = FakeProvider(fixture_dir=None)
    assert isinstance(p, LLMProvider)


def test_fake_structured_returns_schema_valid_extraction():
    p = FakeProvider(fixture_dir=None)
    schema = extraction_json_schema()
    result = p.complete_structured(
        role=Role.EXTRACTION,
        schema=schema,
        system="sys",
        messages=[{"role": "user", "content": "chunk text"}],
    )
    assert Draft202012Validator(schema).is_valid(result.data)
    assert result.data == {"entities": [], "relations": []}
    assert result.model == "fake"


def test_fake_complete_is_deterministic():
    p = FakeProvider(fixture_dir=None)
    kw = dict(role=Role.ROUTER, system="s", messages=[{"role": "user", "content": "hi"}])
    a = p.complete(**kw)
    b = p.complete(**kw)
    assert a.text == b.text == "[fake:router]"


def test_cassette_record_then_replay_identical(tmp_path):
    inner = FakeProvider(fixture_dir=None)
    schema = extraction_json_schema()
    call = dict(
        role=Role.EXTRACTION,
        schema=schema,
        system="sys",
        messages=[{"role": "user", "content": "hello world"}],
    )

    recorder = CassetteProvider(inner, tmp_path, record=True)
    recorded = recorder.complete_structured(**call)

    # A cassette file should now exist.
    assert list(tmp_path.glob("*.json"))

    player = CassetteProvider(inner, tmp_path, record=False)
    replayed = player.complete_structured(**call)

    assert replayed.data == recorded.data
    assert replayed.raw == recorded.raw


def test_cassette_replay_miss_raises(tmp_path):
    player = CassetteProvider(FakeProvider(fixture_dir=None), tmp_path, record=False)
    with pytest.raises(CassetteMiss):
        player.complete_structured(
            role=Role.EXTRACTION,
            schema=extraction_json_schema(),
            system="sys",
            messages=[{"role": "user", "content": "unseen"}],
        )


def test_registry_routes_light_roles_to_haiku():
    for role in (Role.INTAKE, Role.ROUTER, Role.STEWARD):
        spec = registry.resolve(role, "balanced")
        assert spec.model == registry.HAIKU45
        assert spec.family == "haiku"
        assert spec.supports_sampling
        assert not spec.supports_effort
        assert spec.temperature == 0.0


def test_registry_routes_heavy_roles_to_sonnet5():
    for role in (Role.EXTRACTION, Role.ANALYST):
        spec = registry.resolve(role, "balanced")
        assert spec.model == registry.SONNET5
        assert spec.family == "5"
        assert spec.supports_effort
        assert not spec.supports_sampling
        assert spec.effort is not None


def test_all_models_balanced():
    assert registry.all_models("balanced") == {registry.SONNET5, registry.HAIKU45}
