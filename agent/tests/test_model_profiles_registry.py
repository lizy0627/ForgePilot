"""Model and Agent configuration is validated without provider requests."""
import json

import pytest
from pydantic import ValidationError

from forgepilot.agents import AgentRegistry, BUILTIN_AGENT_DEFINITIONS
from forgepilot.errors import ForgePilotError
from forgepilot.models.profiles import ModelConfiguration, ModelProfile, ModelRoute, SecretResolver


def profile(name="primary", family="family-a", **changes):
    values = dict(profile_id=name, provider="openai-compatible", base_url="https://model.test/v1",
                  api_key_ref="env:FORGEPILOT_API_KEY", model="configured-model", family=family,
                  timeout_seconds=30, max_output_tokens=4096, temperature=0.2)
    values.update(changes)
    return ModelProfile(**values)


def registry_json(*definitions):
    return json.dumps({"schema_version": "1.0", "agents": [
        value.model_dump(mode="json") if hasattr(value, "model_dump") else value for value in definitions
    ]})


def test_profile_roundtrip_and_role_route_override(tmp_path):
    config = ModelConfiguration(profiles=[profile(), profile("fallback", "family-b")], routes=[
        ModelRoute(route_id="analysis", primary="primary"),
        ModelRoute(route_id="analysis", role="impact_test_analyst", primary="fallback"),
    ])
    path = tmp_path / "models.json"
    path.write_text(config.model_dump_json(), encoding="utf-8")
    loaded = ModelConfiguration.load(path)
    assert loaded == config
    assert loaded.router().resolve("analysis", "root_cause_analyst").primary.profile_id == "primary"
    assert loaded.router().resolve("analysis", "impact_test_analyst").primary.profile_id == "fallback"
    with pytest.raises(ForgePilotError, match="No model route"):
        loaded.router().resolve("missing")


@pytest.mark.parametrize("changes", [
    {"api_key_ref": "actual-api-secret"}, {"api_key_ref": "file:C:/secret"},
    {"api_key_ref": "dpapi:../../secret"}, {"api_key": "raw-secret"},
    {"base_url": "https://user:raw-secret@model.test"},
    {"base_url": "https://model.test/v1?api_key=raw-secret"},
    {"base_url": "file:///secrets"}, {"base_url": "https://model.test/#raw-secret"},
    {"provider": "python-plugin"}, {"temperature": float("nan")},
    {"temperature": 2.1}, {"timeout_seconds": 0}, {"timeout_seconds": 601},
    {"timeout_seconds": True}, {"max_output_tokens": 0}, {"max_output_tokens": 65_537},
    {"model": "bad\nmodel"}, {"family": ""}, {"supports_tool_calling": "true"},
])
def test_profile_rejects_credentials_and_invalid_settings(changes):
    with pytest.raises(ValidationError) as caught:
        profile(**changes)
    assert "raw-secret" not in str(caught.value)


def test_config_loader_safe_errors_duplicate_ids_and_unknown_profiles():
    raw_secret = "sensitive-context-do-not-echo"
    for raw in (raw_secret, '{"schema_version":"1.0","schema_version":"2.0"}',
                json.dumps({"profiles": [], "routes": [], "api_key": raw_secret})):
        with pytest.raises(ForgePilotError, match="llm_config") as caught:
            ModelConfiguration.from_json(raw)
        assert raw_secret not in str(caught.value)
    with pytest.raises(ValidationError, match="unique"):
        ModelConfiguration(profiles=[profile(), profile()], routes=[ModelRoute(route_id="chat", primary="primary")])
    with pytest.raises(ValidationError, match="unknown"):
        ModelConfiguration(profiles=[profile()], routes=[ModelRoute(route_id="chat", primary="missing")])
    with pytest.raises(ValidationError, match="unique"):
        ModelConfiguration(profiles=[profile()], routes=[ModelRoute(route_id="chat", primary="primary")] * 2)


def test_family_constraint_includes_fallback_and_role_overrides():
    profiles = [profile("implement", "family-a"), profile("review", "family-b"), profile("same", "family-a")]
    implementation = ModelRoute(route_id="implementation", primary="implement")
    with pytest.raises(ValidationError, match="families must differ"):
        ModelConfiguration(profiles=profiles, routes=[implementation,
            ModelRoute(route_id="review", primary="review", fallback="same")])
    with pytest.raises(ValidationError, match="families must differ"):
        ModelConfiguration(profiles=profiles, routes=[implementation,
            ModelRoute(route_id="review", role="reviewer", primary="same")])
    config = ModelConfiguration(profiles=profiles, routes=[implementation,
        ModelRoute(route_id="review", primary="review")])
    assert config.router().resolve("review").primary.family != config.router().resolve("implementation").primary.family


def test_secret_resolver_uses_runtime_environment_without_serialization(monkeypatch):
    secret = "offline-runtime-only-secret"
    monkeypatch.setenv("FORGEPILOT_API_KEY", secret)
    assert SecretResolver()(profile().api_key_ref) == secret
    assert secret not in profile().model_dump_json()
    assert secret not in repr(profile())
    with pytest.raises(ForgePilotError, match="credential reference"):
        SecretResolver(environment={})(profile().api_key_ref)


def test_secret_resolver_dpapi_reference_and_safe_failures(monkeypatch):
    import forgepilot.models.profiles as module
    looked_up = []
    monkeypatch.setattr(module, "unprotect_secrets", lambda blob: {"api_key": "offline-dpapi-key"})
    resolver = SecretResolver(dpapi_lookup=lambda name: looked_up.append(name) or b"encrypted")
    assert resolver("dpapi:model-api-key") == "offline-dpapi-key"
    assert looked_up == ["model-api-key"]
    def fail(_):
        raise RuntimeError("secret-blob-and-key")
    with pytest.raises(ForgePilotError) as caught:
        SecretResolver(dpapi_lookup=fail)("dpapi:model-api-key")
    assert "secret-blob-and-key" not in str(caught.value)


def test_registry_loads_versioned_manifest_and_validates_prompt_role(tmp_path):
    definition = BUILTIN_AGENT_DEFINITIONS[3]
    path = tmp_path / "agents.json"
    path.write_text(registry_json(definition), encoding="utf-8")
    assert AgentRegistry.load(path).get("implementer.v1") == definition
    invalid = definition.model_dump(mode="json")
    for prompt in ("unversioned", "implementer-prompt.v2", "reviewer-prompt.v1"):
        invalid["prompt_version"] = prompt
        with pytest.raises(ForgePilotError, match="agent_config"):
            AgentRegistry.from_json(registry_json(invalid))
    invalid["prompt_version"] = "implementer-prompt.v2"
    registry = AgentRegistry.from_json(registry_json(invalid),
        prompt_versions={"implementer-prompt.v2": "implementer"})
    assert registry.get("implementer.v1").prompt_version == "implementer-prompt.v2"


@pytest.mark.parametrize("changes", [
    {"agent_id": "implementer"}, {"output_artifacts": ["TaskSpec"]},
    {"input_artifacts": ["SecretContext"]}, {"capabilities": ["mcp.read"]},
    {"limits": {"max_model_calls": 0}}, {"python_module": "arbitrary.code"},
])
def test_registry_rejects_invalid_contracts_without_echo(changes):
    invalid = BUILTIN_AGENT_DEFINITIONS[3].model_dump(mode="json")
    invalid.update(changes)
    with pytest.raises(ForgePilotError, match="agent_config") as caught:
        AgentRegistry.from_json(registry_json(invalid))
    assert "arbitrary.code" not in str(caught.value)


def test_registry_validates_route_and_input_output_budget_boundaries():
    definition = BUILTIN_AGENT_DEFINITIONS[3]
    registry = AgentRegistry([definition])
    registry.validate_artifacts("implementer.v1", ["TaskSpec", "ExecutionPlan"])
    registry.validate_artifacts("implementer.v1", ["ChangeSet"], output=True)
    with pytest.raises(ForgePilotError, match="agent_artifact"):
        registry.validate_artifacts("implementer.v1", ["ReviewReport"], output=True)
    limits = definition.limits.model_dump()
    limits["max_model_calls"] -= 1
    assert registry.validate_limits("implementer.v1", limits).max_model_calls == limits["max_model_calls"]
    limits["max_model_calls"] += 2
    with pytest.raises(ForgePilotError, match="agent_budget"):
        registry.validate_limits("implementer.v1", limits)
    config = ModelConfiguration(profiles=[profile(supports_tool_calling=False)],
        routes=[ModelRoute(route_id="implementation", primary="primary")])
    with pytest.raises(ForgePilotError, match="Tool Calling"):
        AgentRegistry([definition], router=config.router())
