"""Strict analysis-stage contracts layered over backwards-compatible v1 artifacts.

The stored v1 models remain readable for existing tasks. Newly generated analysis
artifacts cross these stricter model/schema boundaries before publication.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from ..artifacts.models import (
    AcceptanceCriterion, ArtifactModel, DataPolicy, EvidenceBundle,
    EvidenceBundleArtifact, EvidenceItem, ExecutionPlan, RepositorySpec, TaskSpec,
    TaskSpecArtifact, TestPolicy, Text,
)
from ..artifacts.validation import ArtifactRelationshipError, validate_plan_coverage


class AnalysisCriterion(AcceptanceCriterion):
    id: Annotated[str, Field(pattern=r"^AC-[1-9][0-9]*$", max_length=32)]
    description: Annotated[str, Field(min_length=8, max_length=2_048)]

    @field_validator("description")
    @classmethod
    def observable_outcome(cls, value):
        if value.casefold().strip(".。!！ ") in {
            "功能正常", "工作正常", "works correctly", "works as expected",
            "everything works", "all requirements met",
        }:
            raise ValueError("Acceptance criteria must describe an observable outcome")
        return value


class AnalysisTaskSpec(TaskSpec):
    task_type: Literal["bug_fix", "feature"]
    acceptance_criteria: tuple[AnalysisCriterion, ...] = Field(min_length=2, max_length=64)
    forbidden_scope: tuple[Annotated[str, Field(min_length=1, max_length=512)], ...] = Field(min_length=1, max_length=64)
    test_policy: TestPolicy
    data_policy: DataPolicy
    assumptions: tuple[Text, ...] = Field(max_length=32)
    risks: tuple[Text, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def verifiable_task(self):
        methods = {item.verification for item in self.acceptance_criteria}
        if "build" not in methods or not methods & {"test", "ctest", "protected_test"}:
            raise ValueError("TaskSpec requires build and test acceptance criteria")
        if self.task_type == "bug_fix":
            if not self.problem.reproduction_steps:
                raise ValueError("Bug fixes require reproduction steps")
            if not any(item.verification == "protected_test" or (
                    item.verification in {"test", "ctest"}
                    and re.search(r"regression|回归", item.description, re.IGNORECASE))
                    for item in self.acceptance_criteria):
                raise ValueError("Bug fixes require an explicit regression acceptance criterion")
        elif self.problem.expected_behavior == self.problem.observed_behavior:
            raise ValueError("Features must identify new expected behavior")
        return self


class AnalysisEvidenceItem(EvidenceItem):
    claim: Annotated[str, Field(min_length=1, max_length=2_048)]
    reason: Annotated[str, Field(min_length=1, max_length=512)]
    path: Annotated[str, Field(min_length=1, max_length=1_024)]
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    file_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    symbol: Annotated[str, Field(min_length=1, max_length=512)]
    source: Literal["local_repository", "rag", "external"]


class AnalysisEvidenceBundle(EvidenceBundle):
    items: tuple[AnalysisEvidenceItem, ...] = Field(min_length=1, max_length=128)
    summary: Annotated[str, Field(min_length=1, max_length=2_048)]


class DraftTaskSpecInput(ArtifactModel):
    request: Text
    repository: RepositorySpec
    data_policy: DataPolicy
    language: Annotated[str, Field(min_length=1, max_length=128)] = "en"


class AnalystInput(ArtifactModel):
    task_spec: TaskSpecArtifact


class GeneratePlanInput(ArtifactModel):
    task_spec: TaskSpecArtifact
    evidence: EvidenceBundleArtifact


_INPUTS = {
    "draft_task_spec": DraftTaskSpecInput,
    "root_cause_analysis": AnalystInput,
    "impact_test_analysis": AnalystInput,
    "generate_plan": GeneratePlanInput,
}
_OUTPUTS = {
    "draft_task_spec": AnalysisTaskSpec,
    "root_cause_analysis": AnalysisEvidenceBundle,
    "impact_test_analysis": AnalysisEvidenceBundle,
    "generate_plan": ExecutionPlan,
}


def _schema(model, stage, direction):
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://forgepilot.local/schemas/agent-{stage.replace('_', '-')}-{direction}.v1.schema.json",
        **model.model_json_schema(),
    }


def input_schema(stage: str) -> dict:
    try:
        return _schema(_INPUTS[stage], stage, "input")
    except KeyError:
        raise ValueError("Unknown analysis stage") from None


def output_schema(stage: str, *, task_spec: TaskSpec | None = None) -> dict:
    try:
        schema = _schema(_OUTPUTS[stage], stage, "output")
    except KeyError:
        raise ValueError("Unknown analysis stage") from None
    if stage == "draft_task_spec":
        # Express required verification modes in JSON Schema so format repair
        # happens inside the same bounded gateway budget.
        schema["properties"]["acceptance_criteria"]["allOf"] = [
            {"contains": {"properties": {"verification": {"const": "build"}},
                          "required": ["verification"]}},
            {"contains": {"properties": {"verification": {"enum": ["test", "ctest", "protected_test"]}},
                          "required": ["verification"]}},
        ]
        schema["allOf"] = [{
            "if": {"properties": {"task_type": {"const": "bug_fix"}}},
            "then": {"properties": {
                "problem": {"properties": {"reproduction_steps": {"minItems": 1}},
                            "required": ["reproduction_steps"]},
                "acceptance_criteria": {"contains": {"anyOf": [
                    {"properties": {"verification": {"const": "protected_test"}}, "required": ["verification"]},
                    {"properties": {"verification": {"enum": ["test", "ctest"]},
                                    "description": {"pattern": "[Rr][Ee][Gg][Rr][Ee][Ss][Ss][Ii][Oo][Nn]|回归"}},
                     "required": ["verification", "description"]},
                ]}},
            }},
        }]
    elif stage == "generate_plan" and task_spec is not None:
        schema["$defs"]["PlanStep"]["properties"]["acceptance_criteria_ids"]["items"]["enum"] = [
            item.id for item in task_spec.acceptance_criteria]
        schema["properties"]["steps"]["allOf"] = [
            {"contains": {"properties": {
                "acceptance_criteria_ids": {"contains": {"const": item.id}},
                "verification": {"contains": {"const": item.verification}},
            }, "required": ["acceptance_criteria_ids", "verification"]}}
            for item in task_spec.acceptance_criteria
        ]
    return deepcopy(schema)


def validate_task_spec(spec: TaskSpec | dict) -> TaskSpec:
    payload = spec.model_dump(mode="json") if isinstance(spec, TaskSpec) else spec
    checked = AnalysisTaskSpec.model_validate(payload)
    return TaskSpec.model_validate(checked.model_dump(mode="json"))


def validate_evidence_bundle(bundle: EvidenceBundle | dict) -> EvidenceBundle:
    payload = bundle.model_dump(mode="json") if isinstance(bundle, EvidenceBundle) else bundle
    checked = AnalysisEvidenceBundle.model_validate(payload)
    return EvidenceBundle.model_validate(checked.model_dump(mode="json"))


def validate_execution_plan(spec: TaskSpec, plan: ExecutionPlan | dict) -> ExecutionPlan:
    checked = ExecutionPlan.model_validate(plan)
    validate_plan_coverage(spec, checked)
    for criterion in spec.acceptance_criteria:
        if not any(criterion.id in step.acceptance_criteria_ids
                   and criterion.verification in step.verification for step in checked.steps):
            raise ArtifactRelationshipError(
                f"ExecutionPlan does not verify {criterion.id} using {criterion.verification}")
    return checked


def contract_schemas() -> dict[str, dict]:
    return {
        f"agent-{stage.replace('_', '-')}-{direction}.v1.schema.json": factory(stage)
        for stage in _INPUTS for direction, factory in (("input", input_schema), ("output", output_schema))
    }


__all__ = [
    "AnalysisTaskSpec", "AnalysisEvidenceBundle", "AnalysisEvidenceItem", "DraftTaskSpecInput",
    "AnalystInput", "GeneratePlanInput", "contract_schemas", "input_schema", "output_schema",
    "validate_task_spec", "validate_evidence_bundle", "validate_execution_plan",
]
