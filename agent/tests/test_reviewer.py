"""Independent review exercises real gateway boundaries with fake providers."""
import asyncio
from copy import deepcopy
import json

import pytest

from forgepilot.agents.prompts import get_prompt
from forgepilot.agents.registry import AgentRegistry
from forgepilot.agents.review import ReviewerService, require_complete_validation, validate_review
from forgepilot.artifacts.models import CommandResult, ReviewReport
from forgepilot.artifacts.validation import create_artifact
from forgepilot.errors import ForgePilotError
from forgepilot.models.gateway import ModelGateway
from forgepilot.models.profiles import ModelConfiguration, ModelProfile, ModelRoute
from forgepilot.validation import PUBLIC_PIPELINE_STAGES
from conftest import call, reply
from test_artifact_protocol import TASK_ID, all_contents


def inputs(**validation_changes):
    spec, evidence, _, changes, validation, review, _ = all_contents()
    spec = spec.model_copy(update={"data_policy": spec.data_policy.model_copy(
        update={"cloud_model_allowed": True})})
    validation = validation.model_copy(update={"commands": tuple(CommandResult(
        kind=kind, command_summary=kind, started_at="2026-10-27T01:00:00Z",
        finished_at="2026-10-27T01:01:00Z", exit_code=0)
        for kind in ("integrity", "configure", "build", "test", "protected_test")),
        **validation_changes})
    artifact = lambda content, **kwargs: create_artifact(task_id=TASK_ID,
        producer="validator.v1" if type(content).__name__ == "ValidationReport" else "orchestrator.v1",
        producer_node="test-node", content=content, **kwargs)
    changes = artifact(changes.model_copy(update={"summary": "PRIVATE IMPLEMENTER CHAT MUST NOT PASS"}))
    return (artifact(spec), changes, artifact(validation, parent_artifact_ids=(changes.artifact_id,)),
            artifact(evidence), review)


class Provider:
    def __init__(self, result, *, block=False):
        self.result, self.block = result, block
        self.seen = []
        self.started, self.cancelled = asyncio.Event(), asyncio.Event()

    async def chat(self, messages, tools=None):
        self.seen.append((deepcopy(messages), tools))
        self.started.set()
        if self.block:
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()
        return self.result

    async def aclose(self):
        pass


def gateway_for(provider, *, repair_family="writer", same_family=False, enforce=True):
    profiles = tuple(ModelProfile(profile_id=name, base_url="https://offline.test/v1",
        api_key_ref="env:FORGEPILOT_API_KEY", model="fake-" + name, family=family,
        max_output_tokens=1024) for name, family in (
            ("writer", "writer"), ("reviewer", "writer" if same_family else "reviewer"),
            ("repairer", repair_family)))
    configuration = ModelConfiguration(profiles=profiles, routes=(
        ModelRoute(route_id="implementation", primary="writer"),
        ModelRoute(route_id="repair", primary="repairer"),
        ModelRoute(route_id="review", primary="reviewer")),
        enforce_review_family_separation=enforce)
    return ModelGateway(configuration, secret_resolver=lambda _: "offline-only-key",
                        provider_factory=lambda *_: provider)


@pytest.mark.asyncio
async def test_independent_artifact_only_review_has_no_tools_history_plan_or_implementer_claims():
    spec, changes, validation, evidence, expected = inputs()
    provider = Provider(reply(expected.model_dump_json()))
    async with gateway_for(provider) as gateway:
        service = ReviewerService(gateway)
        actual = await service.review(spec, changes, validation, evidence=(evidence,))
        assert actual == expected
        assert gateway.accounting.snapshot()[0].role == "reviewer"
        assert gateway.accounting.snapshot()[0].model == "fake-reviewer"
    messages, tools = provider.seen[0]
    assert not tools
    payload = json.loads(messages[1]["content"])
    assert set(payload) == {"task_spec", "final_diff", "validation", "evidence"}
    assert "PRIVATE IMPLEMENTER" not in messages[1]["content"]
    assert "execution_plan_artifact_id" not in messages[1]["content"]
    assert "plan_step_ids" not in messages[1]["content"]
    assert "log_artifact_ids" not in payload["validation"]
    assert payload["final_diff"][0]["unified_diff"] == changes.content.files[0].unified_diff


@pytest.mark.asyncio
async def test_reviewer_blocking_issue_survives_for_bounded_repair():
    spec, changes, validation, _, expected = inputs()
    value = expected.model_dump(mode="json")
    value.update(verdict="request_changes", recommend_delivery=False,
        blocking_issues=[{"issue_id": "RI-1", "description": "Check null before cancellation", "path": "src/window.cpp"}])
    value["acceptance_criteria"][0]["status"] = "not_met"
    provider = Provider(reply(json.dumps(value)))
    async with gateway_for(provider) as gateway:
        report = await ReviewerService(gateway).review(spec, changes, validation)
        assert report.verdict == "request_changes"
        assert report.blocking_issues[0].issue_id == "RI-1"
        assert not report.recommend_delivery


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"hidden_oracle": "not_run"}, {"test_integrity": "not_checked"},
    {"tests_passed": 0}, {"status": "failed", "failure_classification": "test"},
])
async def test_incomplete_validation_cannot_be_overruled_by_reviewer(change):
    spec, changes, validation, _, expected = inputs(**change)
    provider = Provider(reply(expected.model_dump_json()))
    async with gateway_for(provider) as gateway:
        with pytest.raises(ForgePilotError, match="review_validation"):
            await ReviewerService(gateway).review(spec, changes, validation)
    assert not provider.seen


@pytest.mark.asyncio
async def test_public_validation_review_requires_explicit_policy_and_no_protected_criterion():
    spec, changes, validation, _, expected = inputs(hidden_oracle="not_run")
    report = validation.content.model_copy(update={"commands": tuple(item for item in
        validation.content.commands if item.kind != "protected_test")})
    with pytest.raises(ForgePilotError, match="review_validation"):
        require_complete_validation(report)
    with pytest.raises(ForgePilotError, match="review_validation"):
        require_complete_validation(report, require_oracle=False)
    report = report.model_copy(update={"workspace_integrity": "passed", "diff_scope": "passed",
        "pipeline_stages": PUBLIC_PIPELINE_STAGES})
    require_complete_validation(report, require_oracle=False)
    validation = create_artifact(task_id=TASK_ID, producer="validator.v1",
        producer_node="test-node", content=report,
        parent_artifact_ids=(changes.artifact_id,))
    provider = Provider(reply(expected.model_dump_json()))
    async with gateway_for(provider) as gateway:
        with pytest.raises(ForgePilotError, match="review_validation"):
            await ReviewerService(gateway).review(spec, changes, validation, require_oracle=False)
    assert not provider.seen
    public_spec = create_artifact(task_id=TASK_ID, producer="orchestrator.v1",
        producer_node="test-node", content=spec.content.model_copy(update={
            "acceptance_criteria": tuple(item.model_copy(update={"verification": "ctest"})
                if item.verification == "protected_test" else item
                for item in spec.content.acceptance_criteria)}))
    public_review = expected.model_copy(update={
        "test_review": "Public CTest passed; Protected Oracle not run",
        "acceptance_criteria": tuple(item.model_copy(update={"evidence": "Public validation passed"})
            for item in expected.acceptance_criteria)})
    provider = Provider(reply(public_review.model_dump_json()))
    async with gateway_for(provider) as gateway:
        assert (await ReviewerService(gateway).review(public_spec, changes, validation,
            require_oracle=False)).recommend_delivery
    assert len(provider.seen) == 1


@pytest.mark.asyncio
async def test_review_rejects_stale_or_cross_task_validation_before_model():
    spec, changes, validation, _, expected = inputs()
    provider = Provider(reply(expected.model_dump_json()))
    async with gateway_for(provider) as gateway:
        service = ReviewerService(gateway)
        for stale in (validation.model_copy(update={"parent_artifact_ids": ()}),
                      validation.model_copy(update={"task_id": "20000000-0000-4000-a000-000000000001"})):
            with pytest.raises(ForgePilotError, match="review_input"):
                await service.review(spec, changes, stale)
    assert not provider.seen


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["unknown_ac", "missing_ac", "unverified", "false_completion", "tool_call"])
async def test_invalid_coverage_completion_claim_and_tool_calls_never_approve(kind):
    spec, changes, validation, _, expected = inputs()
    value = expected.model_dump(mode="json")
    if kind == "unknown_ac":
        value["acceptance_criteria"][0]["criterion_id"] = "AC-99"
    elif kind == "missing_ac":
        value["acceptance_criteria"] = value["acceptance_criteria"][:1]
    elif kind == "unverified":
        value["acceptance_criteria"][0]["status"] = "not_verified"
    elif kind == "false_completion":
        value = {"status": "completed", "summary": "All requirements done"}
    result = reply(json.dumps(value)) if kind != "tool_call" else reply(None, calls=[
        call("repository_apply_patch", '{"path":"src/window.cpp"}')])
    provider = Provider(result)
    async with gateway_for(provider) as gateway:
        with pytest.raises((ForgePilotError, ValueError)):
            await ReviewerService(gateway).review(spec, changes, validation)
    assert all(not tools for _, tools in provider.seen)
    assert len(provider.seen) <= 2  # At most one bounded schema repair, never code/tool execution.


@pytest.mark.asyncio
async def test_repair_route_cannot_secretly_share_review_family():
    provider = Provider(reply("{}"))
    async with gateway_for(provider, repair_family="reviewer") as gateway:
        with pytest.raises(ForgePilotError, match="families must differ"):
            ReviewerService(gateway)
    assert not provider.seen


@pytest.mark.asyncio
async def test_same_family_requires_explicit_host_configuration_override():
    spec, changes, validation, _, expected = inputs()
    provider = Provider(reply(expected.model_dump_json()))
    async with gateway_for(provider, same_family=True, enforce=False) as gateway:
        assert (await ReviewerService(gateway).review(spec, changes, validation)).recommend_delivery


@pytest.mark.asyncio
async def test_review_preserves_task_cloud_data_policy():
    spec, changes, validation, _, expected = inputs()
    local = spec.content.model_copy(update={"data_policy": spec.content.data_policy.model_copy(
        update={"cloud_model_allowed": False})})
    spec = create_artifact(task_id=TASK_ID, producer="orchestrator.v1", producer_node="test", content=local)
    provider = Provider(reply(expected.model_dump_json()))
    async with gateway_for(provider) as gateway:
        with pytest.raises(ForgePilotError, match="policy_denied"):
            await ReviewerService(gateway).review(spec, changes, validation)
    assert not provider.seen


@pytest.mark.asyncio
@pytest.mark.parametrize("outer", [False, True])
async def test_review_cancellation_drains_provider_and_never_returns_approval(outer):
    spec, changes, validation, _, expected = inputs()
    provider = Provider(reply(expected.model_dump_json()), block=True)
    cancelled = False
    async with gateway_for(provider) as gateway:
        task = asyncio.create_task(ReviewerService(gateway).review(
            spec, changes, validation, is_cancelled=lambda: cancelled))
        await asyncio.wait_for(provider.started.wait(), 2)
        if outer:
            task.cancel()
        else:
            cancelled = True
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert provider.cancelled.is_set()


def test_reviewer_and_repair_have_separate_bounded_prompt_contracts():
    registry = AgentRegistry()
    reviewer = registry.get("reviewer.v1")
    assert set(reviewer.input_artifacts) == {"TaskSpec", "ChangeSet", "ValidationReport", "EvidenceBundle"}
    assert reviewer.limits.max_tool_calls == 0
    assert "repository.patch" not in reviewer.capabilities
    assert "no\ntools" in get_prompt("reviewer.v1", "reviewer")
    repair = get_prompt("repair.v1", "repair")
    assert all(phrase in repair for phrase in ("previous ChangeSet", "remaining repair rounds", "full revalidation"))
    with pytest.raises(ValueError):
        get_prompt("reviewer.v1", "repair")


def test_host_recheck_blocks_unverified_approval_even_if_gateway_schema_is_bypassed():
    spec, _, _, _, expected = inputs()
    unverified = expected.model_copy(update={"acceptance_criteria": (
        expected.acceptance_criteria[0].model_copy(update={"status": "not_verified"}),
        expected.acceptance_criteria[1])})
    with pytest.raises((ForgePilotError, ValueError)):
        validate_review(spec.content, unverified)
