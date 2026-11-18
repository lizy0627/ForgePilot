"""Independent, artifact-only final review with no repository or tool authority.

The Reviewer can judge a validated diff, but cannot replace deterministic checks
or certify its own output. Every approval crosses the host's coverage checks.
"""
from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit

from ..artifacts.models import ChangeSet, EvidenceBundle, ReviewReport, TaskSpec, ValidationReport
from ..artifacts.validation import validate_artifact, validate_review_coverage
from ..errors import ForgePilotError
from ..models.accounting import ModelBudget
from ..workspace_safety import validate_relative
from .prompts import get_prompt
from .registry import AgentRegistry


def review_schema(task_spec: TaskSpec) -> dict:
    """Express coverage in the gateway schema so format repair stays bounded."""
    schema = ReviewReport.model_json_schema()
    ids = [criterion.id for criterion in task_spec.acceptance_criteria]
    criteria = schema["properties"]["acceptance_criteria"]
    criteria.update(minItems=len(ids), maxItems=len(ids), allOf=[
        {"contains": {"properties": {"criterion_id": {"const": identifier}},
                      "required": ["criterion_id"]}} for identifier in ids
    ])
    schema["$defs"]["CriterionReview"]["properties"]["criterion_id"]["enum"] = ids
    schema["allOf"] = [{
        "if": {"properties": {"verdict": {"const": "approve"}}, "required": ["verdict"]},
        "then": {"properties": {
            "blocking_issues": {"maxItems": 0},
            "acceptance_criteria": {"items": {"properties": {"status": {"const": "met"}}}},
        }},
    }]
    return schema


def validate_review(task_spec: TaskSpec, review: ReviewReport) -> ReviewReport:
    """Approval is impossible with unknown, missing or unverified criteria."""
    validate_review_coverage(task_spec, review)
    if review.verdict == "approve" and (
            review.blocking_issues or any(item.status != "met" for item in review.acceptance_criteria)):
        raise ForgePilotError("review_contract", "Reviewer approval requires all criteria met and no blockers")
    if review.recommend_delivery and any(item.status != "met" for item in review.acceptance_criteria):
        raise ForgePilotError("review_contract", "Unverified criteria cannot be recommended for delivery")
    for issue in review.blocking_issues:
        if issue.path is not None:
            validate_relative(issue.path)
    return review


def require_complete_validation(validation: ValidationReport, *, require_oracle=True) -> None:
    if not require_oracle:
        from .delivery import validation_passed
        if not validation_passed(validation, require_oracle=False):
            raise ForgePilotError("review_validation", "Independent review requires complete deterministic validation")
        return
    kinds = {command.kind for command in validation.commands if command.exit_code == 0}
    oracle_ok = (validation.hidden_oracle == "passed" if require_oracle else
                 validation.hidden_oracle in {"passed", "not_run"})
    required_kinds = {"configure", "build", "test", "integrity"}
    if validation.hidden_oracle == "passed":
        required_kinds.add("protected_test")
    if (validation.status != "passed" or validation.failure_classification != "none"
            or validation.tests_failed or validation.tests_passed < 1
            or not oracle_ok or validation.test_integrity != "passed"
            or any(command.exit_code for command in validation.commands)
            or not required_kinds <= kinds
            or (validation.hidden_oracle == "not_run" and "protected_test" in kinds)):
        raise ForgePilotError("review_validation", "Independent review requires complete deterministic validation")


class ReviewerService:
    """No repository, writer, command runner, arbitrary history or tool adapters.

    Callers persist the returned ReviewReport with the input artifact IDs. Model
    accounting stays in the shared gateway and optional caller-owned node budget.
    """

    def __init__(self, gateway, *, limits=None, single_agent=False):
        self.gateway = gateway
        self.single_agent = single_agent
        registry = AgentRegistry(include_generalist=single_agent)
        definition = registry.get("generalist.v1" if single_agent else "reviewer.v1")
        if limits is not None:
            definition = definition.model_copy(update={
                "limits": registry.validate_limits(definition.agent_id, limits)})
        self.definition = definition
        self.route = gateway.router.validate_agent(definition)
        if not single_agent:
            self._check_families()

    def _check_families(self):
        configuration = self.gateway.router.configuration
        if not configuration.enforce_review_family_separation:
            return
        writers = [route for route in configuration.routes
                   if route.route_id in {"implementation", "repair"}]
        if not any(route.route_id == "implementation" for route in writers):
            raise ForgePilotError("llm_config", "Reviewer independence requires an implementation route")
        writer_families = {
            configuration.profile(profile).family.casefold()
            for route in writers for profile in (route.primary, route.fallback) if profile
        }
        if any(profile.family.casefold() in writer_families
               for profile in (self.route.primary, self.route.fallback) if profile):
            raise ForgePilotError("llm_config", "Reviewer and all writer model families must differ")

    def _check_data_policy(self, spec):
        if spec.data_policy.cloud_model_allowed:
            return
        if any(urlsplit(profile.base_url).hostname not in {"localhost", "127.0.0.1", "::1"}
               for profile in (self.route.primary, self.route.fallback) if profile):
            raise ForgePilotError("policy_denied", "Data policy forbids the configured cloud review route")

    async def review(self, task_spec_artifact, change_set_artifact, validation_artifact, *,
                     evidence=(), is_cancelled=lambda: False, budget=None,
                     require_oracle=True) -> ReviewReport:
        if is_cancelled():
            raise asyncio.CancelledError()
        spec, changes, validation = tuple(validate_artifact(item) for item in (
            task_spec_artifact, change_set_artifact, validation_artifact))
        if (not isinstance(spec.content, TaskSpec) or not isinstance(changes.content, ChangeSet)
                or not isinstance(validation.content, ValidationReport)
                or len({item.task_id for item in (spec, changes, validation)}) != 1
                or changes.artifact_id not in validation.parent_artifact_ids):
            raise ForgePilotError("review_input", "Review requires same-task, revision-bound artifacts")
        if changes.content.requires_additional_approval:
            raise ForgePilotError("review_input", "Unapproved scope cannot enter final review")
        require_complete_validation(validation.content, require_oracle=(require_oracle or
            any(item.verification == "protected_test" for item in spec.content.acceptance_criteria)))
        self._check_data_policy(spec.content)
        evidence = tuple(validate_artifact(item) for item in evidence)
        if len(evidence) > 8 or any(not isinstance(item.content, EvidenceBundle)
                                  or item.task_id != spec.task_id for item in evidence):
            raise ForgePilotError("review_input", "Only bounded same-task evidence may enter review")
        # Strip Implementer's summaries/plan references and artifact metadata.
        # No tool exists to follow a log ID, source path or Oracle location.
        payload = {
            "task_spec": spec.content.model_dump(mode="json"),
            "final_diff": [item.model_dump(mode="json", exclude={"plan_step_ids"})
                           for item in changes.content.files],
            "validation": validation.content.model_dump(mode="json", exclude={"log_artifact_ids"}),
            "evidence": [item.content.model_dump(mode="json") for item in evidence],
        }
        prompt_stage = "generalist_reviewer" if self.single_agent else "reviewer"
        messages = [{"role": "system", "content": get_prompt(self.definition.agent_id, prompt_stage)},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
        budget = budget or ModelBudget.from_limits(self.definition.limits)
        client = self.gateway.for_agent(self.definition, budget=budget)
        operation = asyncio.create_task(client.chat(messages, [], output_schema=review_schema(spec.content),
                                                     is_cancelled=is_cancelled))
        try:
            while not operation.done():
                if is_cancelled():
                    raise asyncio.CancelledError()
                await asyncio.wait((operation,), timeout=0.05)
            response = operation.result()
            if is_cancelled():
                raise asyncio.CancelledError()
            if response.message.tool_calls:
                raise ForgePilotError("review_tool_denied", "Reviewer has no tool execution authority")
            result = ReviewReport.model_validate_json(response.message.content)
            return validate_review(spec.content, result)
        finally:
            if not operation.done():
                operation.cancel()
            # Cancellation must drain the provider before the graph may advance.
            drain = asyncio.gather(operation, return_exceptions=True)
            while not drain.done():
                try:
                    await asyncio.shield(drain)
                except asyncio.CancelledError:
                    continue


__all__ = ["ReviewerService", "require_complete_validation", "review_schema", "validate_review"]
