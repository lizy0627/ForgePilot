import json

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from forgepilot.artifacts import (
    AcceptanceCriterion, AllowedScope, ArtifactRelationshipError, ChangeSet, ChangedFile,
    CommandResult, CriterionReview, DataPolicy, DeliveryBundle, EvidenceBundle, EvidenceItem,
    ExecutionPlan, InMemoryArtifactStore, PlanStep, RepositorySpec, ReviewReport, TaskProblem,
    TaskSpec, TestPolicy as ArtifactTestPolicy, UnsupportedSchemaVersion, ValidationReport, canonical_json_bytes,
    content_sha256, create_artifact, validate_artifact, validate_plan_coverage,
)
from forgepilot.artifacts.schemas import generated_schemas, schema_drift

TASK_ID = "10000000-0000-4000-a000-000000000001"
TASK_SPEC_ID = "10000000-0000-4000-a000-000000000002"
PLAN_ID = "10000000-0000-4000-a000-000000000003"
CHANGE_ID = "10000000-0000-4000-a000-000000000004"
VALIDATION_ID = "10000000-0000-4000-a000-000000000005"
REVIEW_ID = "10000000-0000-4000-a000-000000000006"
HASH_A = "a" * 64
HASH_B = "b" * 64


def task_spec():
    return TaskSpec(
        task_type="bug_fix", title="Stop background worker", objective="Cancel work before teardown.",
        repository=RepositorySpec(base_ref="HEAD", toolchain_profile="windows-msvc-qt6"),
        problem=TaskProblem(
            observed_behavior="Worker continues after close.", expected_behavior="Worker stops safely.",
            reproduction_steps=("Start work", "Close window"),
        ),
        acceptance_criteria=(
            AcceptanceCriterion(id="AC-1", description="Worker stops.", verification="protected_test"),
            AcceptanceCriterion(id="AC-2", description="Build remains green.", verification="build"),
        ),
        allowed_scope=AllowedScope(path_patterns=("src/**", "tests/**"), max_changed_files=8),
        forbidden_scope=(".git/**",), test_policy=ArtifactTestPolicy(), data_policy=DataPolicy(),
        assumptions=(), risk_level="medium",
    )


def plan():
    return ExecutionPlan(
        task_spec_artifact_id=TASK_SPEC_ID, summary="Cancel, then verify.",
        steps=(
            PlanStep(
                step_id="STEP-1", owner="implementer.v1", depends_on=(),
                expected_files=("src/window.cpp",), action="Cancel worker during teardown.",
                verification=("protected_test",), risk="Shutdown ordering.", adds_tests=True,
                acceptance_criteria_ids=("AC-1",),
            ),
            PlanStep(
                step_id="STEP-2", owner="implementer.v1", depends_on=("STEP-1",),
                expected_files=("tests/window_test.cpp",), action="Build and run regression tests.",
                verification=("build", "ctest"), risk="Test timing.", adds_tests=True,
                acceptance_criteria_ids=("AC-2",),
            ),
        ),
    )


def all_contents():
    evidence = EvidenceBundle(
        summary="Destructor omits cancellation.",
        items=(EvidenceItem(
            evidence_id="EV-1", claim="No cancellation call.", path="src/window.cpp",
            start_line=10, end_line=20, file_hash=HASH_A, symbol="Window::~Window",
            source="local_repository", confidence=0.9, reason="Only UI cleanup is present.",
        ),),
    )
    changes = ChangeSet(
        execution_plan_artifact_id=PLAN_ID, summary="Cancel worker and test it.",
        files=(ChangedFile(
            path="src/window.cpp", change_type="modified", unified_diff="@@ -1 +1 @@\n-old\n+new",
            before_sha256=HASH_A, after_sha256=HASH_B, plan_step_ids=("STEP-1",),
            acceptance_criteria_ids=("AC-1",),
        ),),
    )
    validation = ValidationReport(
        toolchain_profile="windows-msvc-qt6",
        commands=(CommandResult(
            kind="test", command_summary="ctest", started_at="2026-10-19T01:00:00Z",
            finished_at="2026-10-19T01:01:00Z", exit_code=0, output_summary="All passed.",
        ),), status="passed", tests_passed=3, tests_failed=0, hidden_oracle="passed",
        test_integrity="passed", failure_classification="none", summary="Validation passed.",
    )
    review = ReviewReport(
        verdict="approve", acceptance_criteria=(
            CriterionReview(criterion_id="AC-1", status="met", evidence="Protected test passes."),
            CriterionReview(criterion_id="AC-2", status="met", evidence="Build passes."),
        ), blocking_issues=(), non_blocking_suggestions=(), test_review="Tests are intact.",
        security_review="No new privilege.", recommend_delivery=True, summary="Ready.",
    )
    delivery = DeliveryBundle(
        change_set_artifact_id=CHANGE_ID, validation_report_artifact_id=VALIDATION_ID,
        review_report_artifact_id=REVIEW_ID, commit=None, build_test_summary="All passed.",
        reviewer_conclusion="Approved.", changed_files=("src/window.cpp",),
        usage_instructions="Build and run normally.", risks=("Shutdown timing.",),
        rollback="Revert the patch.", patch_path=None,
    )
    return (task_spec(), evidence, plan(), changes, validation, review, delivery)


@pytest.mark.parametrize("content", all_contents(), ids=lambda item: type(item).__name__)
def test_all_artifact_types_validate_and_match_exported_json_schema(content):
    artifact = create_artifact(
        task_id=TASK_ID, artifact_id={
            "TaskSpec": TASK_SPEC_ID, "ExecutionPlan": PLAN_ID, "ChangeSet": CHANGE_ID,
            "ValidationReport": VALIDATION_ID, "ReviewReport": REVIEW_ID,
        }.get(type(content).__name__),
        producer="orchestrator.v1", producer_node="test-node", content=content,
    )
    payload = artifact.model_dump(mode="json")
    validate_artifact(json.dumps(payload))
    schema = generated_schemas()[f"artifact-{artifact.artifact_type.lower()}.v1.schema.json"]
    Draft202012Validator(schema).validate(payload)


def test_canonical_hash_is_stable_and_unicode_preserving():
    left = {"b": [2, 1], "a": "窗口"}
    right = {"a": "窗口", "b": [2, 1]}
    assert canonical_json_bytes(left) == canonical_json_bytes(right)
    assert content_sha256(left) == content_sha256(right)


def test_envelope_rejects_tampered_content_hash():
    artifact = create_artifact(
        task_id=TASK_ID, artifact_id=TASK_SPEC_ID, producer="orchestrator.v1",
        producer_node="draft-task", content=task_spec(),
    )
    payload = artifact.model_dump(mode="json")
    payload["content_sha256"] = HASH_A
    with pytest.raises(ValidationError, match="content_sha256"):
        validate_artifact(payload)


def test_unknown_schema_version_is_rejected_before_model_dispatch():
    artifact = create_artifact(
        task_id=TASK_ID, producer="orchestrator.v1", producer_node="draft-task", content=task_spec())
    payload = artifact.model_dump(mode="json")
    payload["schema_version"] = "2.0"
    with pytest.raises(UnsupportedSchemaVersion, match="2.0"):
        validate_artifact(payload)


def test_models_are_deeply_immutable_at_collection_boundaries():
    spec = task_spec()
    with pytest.raises(ValidationError):
        spec.title = "Changed"
    assert isinstance(spec.acceptance_criteria, tuple)
    with pytest.raises(AttributeError):
        spec.acceptance_criteria.append(spec.acceptance_criteria[0])


def test_store_preserves_parent_and_revision_relations_without_mutation():
    store = InMemoryArtifactStore()
    original = create_artifact(
        task_id=TASK_ID, artifact_id=TASK_SPEC_ID, producer="orchestrator.v1",
        producer_node="draft-task", content=task_spec())
    store.put(original)
    revised_content = task_spec().model_copy(update={"title": "Stop worker safely"})
    revision = store.revise(
        TASK_SPEC_ID, artifact_id_override="20000000-0000-4000-a000-000000000001",
        producer="orchestrator.v1", producer_node="revise-task", content=revised_content)
    assert store.get(TASK_SPEC_ID).content.title == "Stop background worker"
    assert revision.artifact_id != original.artifact_id
    assert revision.parent_artifact_ids == (original.artifact_id,)
    assert store.relations_from(original.artifact_id)[0].relation == "revision_of"


def test_store_records_derived_parent_child_relationships():
    store = InMemoryArtifactStore()
    spec_artifact = create_artifact(
        task_id=TASK_ID, artifact_id=TASK_SPEC_ID, producer="orchestrator.v1",
        producer_node="draft-task", content=task_spec())
    plan_artifact = create_artifact(
        task_id=TASK_ID, artifact_id=PLAN_ID, producer="orchestrator.v1",
        producer_node="plan", parent_artifact_ids=(TASK_SPEC_ID,), content=plan())
    store.put(spec_artifact)
    store.put(plan_artifact)
    assert store.is_referenced(TASK_SPEC_ID)
    assert store.parents_of(PLAN_ID)[0].relation == "derived_from"


def test_reasoning_trace_fields_are_not_part_of_the_protocol():
    payload = EvidenceItem(
        evidence_id="EV-1", claim="A claim.", source="tool_output", confidence=0.5,
        reason="Concise evidence rationale.",
    ).model_dump(mode="json")
    payload["chain_of_thought"] = "private reasoning"
    with pytest.raises(ValidationError, match="Extra inputs"):
        EvidenceItem.model_validate(payload)


def test_plan_must_cover_every_and_only_declared_acceptance_criterion():
    incomplete = plan().model_copy(update={"steps": plan().steps[:1]})
    with pytest.raises(ArtifactRelationshipError, match="AC-2"):
        validate_plan_coverage(task_spec(), incomplete)

    store = InMemoryArtifactStore()
    spec_artifact = create_artifact(
        task_id=TASK_ID, artifact_id=TASK_SPEC_ID, producer="orchestrator.v1",
        producer_node="draft-task", content=task_spec())
    store.put(spec_artifact)
    bad_plan = create_artifact(
        task_id=TASK_ID, artifact_id=PLAN_ID, producer="orchestrator.v1",
        producer_node="plan", parent_artifact_ids=(TASK_SPEC_ID,), content=incomplete)
    with pytest.raises(ArtifactRelationshipError, match="AC-2"):
        store.put(bad_plan)


def test_invalid_criteria_and_cycle_are_rejected():
    with pytest.raises(ValidationError):
        AcceptanceCriterion(id="", description="Vague", verification="manual")
    circular = plan().steps[0].model_copy(update={"depends_on": ("STEP-2",)})
    with pytest.raises(ValidationError, match="acyclic"):
        ExecutionPlan(
            task_spec_artifact_id=TASK_SPEC_ID, summary="Cycle.",
            steps=(circular, plan().steps[1]),
        )


def test_v1_json_round_trip_remains_compatible_and_schemas_do_not_drift():
    artifact = create_artifact(
        task_id=TASK_ID, artifact_id=TASK_SPEC_ID, producer="orchestrator.v1",
        producer_node="draft-task", created_at="2026-10-19T00:00:00Z", content=task_spec())
    restored = validate_artifact(artifact.model_dump_json())
    assert restored == artifact
    assert schema_drift() == ()
