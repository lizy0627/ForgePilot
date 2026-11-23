"""Validate the selected analyst's primary and fallback before opening a worktree."""
from types import SimpleNamespace

import httpx
import pytest

from forgepilot.agents.analysis import validate_skill_routes
from forgepilot.agents.registry import AgentRegistry
from forgepilot.artifacts.models import DataPolicy
from forgepilot.errors import ForgePilotError
from forgepilot.models.accounting import ModelBudget
from forgepilot.models.profiles import ModelConfiguration, ModelProfile, ModelRoute
from test_model_gateway import completion, gateway_for


@pytest.mark.parametrize(("primary", "fallback", "scope", "cloud_allowed", "stage", "accepted"), [
    ("cloud", "local", "local_only", True, "root_cause_analysis", False),
    ("local", "cloud", "local_only", True, "root_cause_analysis", False),
    ("local", "cloud", "allowed_by_policy", False, "root_cause_analysis", False),
    ("local", "cloud", "allowed_by_policy", True, "root_cause_analysis", True),
    ("local", "cloud", "local_only", False, "impact_test_analysis", True),
])
def test_selection_checks_only_target_roles_and_all_their_routes(qt_draft, primary, fallback, scope,
                                                                cloud_allowed, stage, accepted):
    profiles = tuple(ModelProfile(profile_id=name, base_url=url, model=name, family=name,
                                  api_key_ref="env:FORGEPILOT_API_KEY") for name, url in (
        ("local", "http://127.0.0.1:1/v1"), ("cloud", "https://offline.test/v1")))
    router = ModelConfiguration(profiles=profiles, routes=(
        ModelRoute(route_id="analysis", primary=primary, fallback=fallback, role="root_cause_analyst"),
        ModelRoute(route_id="analysis", primary="local", role="impact_test_analyst"))).router()
    workflow = SimpleNamespace(agents=AgentRegistry(), gateway=SimpleNamespace(router=router))
    content = qt_draft.reviewed_content().model_copy(update={"stages": (stage,), "usage_scope": scope})
    skills = (SimpleNamespace(content=content),)
    policy = DataPolicy(cloud_model_allowed=cloud_allowed)
    if accepted:
        validate_skill_routes(workflow, skills, policy)
    else:
        with pytest.raises(ForgePilotError, match="skill_route_denied"):
            validate_skill_routes(workflow, skills, policy)


@pytest.mark.parametrize("workflow", [SimpleNamespace(single_agent=True), SimpleNamespace()])
def test_missing_analyst_rejects_skill_before_repository_access(qt_draft, workflow):
    with pytest.raises(ForgePilotError, match="skill_stage_unavailable"):
        validate_skill_routes(workflow, (SimpleNamespace(content=qt_draft.reviewed_content()),), DataPolicy())


@pytest.mark.asyncio
async def test_first_dispatch_excludes_budget_denials_and_does_not_repeat_on_fallback():
    events = []

    def handler(request):
        events.append("provider")
        if events.count("provider") == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=completion())

    async with gateway_for(handler) as gateway:
        client = gateway.for_route("analysis", budget=ModelBudget(max_model_calls=2))
        await client.chat([], on_dispatch=lambda: events.append("injection"))
        with pytest.raises(ForgePilotError, match="llm_budget"):
            await client.chat([], on_dispatch=lambda: events.append("injection"))
        assert events == ["injection", "provider", "provider"]


@pytest.mark.asyncio
async def test_receipt_failure_does_not_start_or_account_a_provider_call():
    def fail_receipt():
        raise OSError("Cannot persist injection receipt")

    async with gateway_for(lambda _: pytest.fail("Provider received unregistered guidance")) as gateway:
        with pytest.raises(OSError, match="Cannot persist"):
            await gateway.for_route("analysis").chat([], on_dispatch=fail_receipt)
        assert not gateway.accounting.snapshot()
