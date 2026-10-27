"""Complete offline Bug Fix / Feature loops use the real compiler and test runner."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from closed_loop_support import (
    CMAKE, COMPILE_BROKEN, FEATURE, FEATURE_CMAKE, FEATURE_HEADER, FEATURE_TEST,
    FIXED, ORACLE_BROKEN, ORIGINAL, TEST_BROKEN, FakeProvider, create, done,
    make_loop_case, patch, review_response,
)
from forgepilot.orchestration.models import NodeStatus
from forgepilot.errors import ForgePilotError


def assert_delivered(case, result, *, rounds=0):
    assert result.status is NodeStatus.SUCCEEDED, [
        (node.node_id, node.status.value, node.result.error_summary if node.result else None) for node in result.nodes
        if node.status not in {NodeStatus.PENDING, NodeStatus.SKIPPED}]
    reports = case.artifacts("ValidationReport")
    assert len(reports) == rounds + 1
    final = reports[-1].content
    assert final.status == "passed" and final.failure_classification == "none"
    assert final.hidden_oracle == final.test_integrity == "passed"
    assert final.tests_passed > 0 and final.tests_failed == 0
    review = case.artifacts("ReviewReport")[-1]
    assert review.content.recommend_delivery
    bundles = case.artifacts("DeliveryBundle")
    assert len(bundles) == 1
    delivery = bundles[0].content
    assert delivery.validation_report_artifact_id == reports[-1].artifact_id
    assert delivery.review_report_artifact_id == review.artifact_id
    assert delivery.commit == case.manager.inspect_task(case.task_id).head_commit
    assert delivery.build_test_summary and delivery.reviewer_conclusion
    assert delivery.usage_instructions and delivery.rollback
    assert isinstance(delivery.risks, tuple)
    assert delivery.patch_path and Path(delivery.patch_path).is_file()
    assert Path(delivery.patch_path).read_bytes()
    assert (case.root / "src/value.cpp").read_text(encoding="utf-8") == ORIGINAL
    assert all(not tools for _, tools in case.providers["review"].seen)
    assert {"implementer", "reviewer"} <= {item["role"] for item in case.model_calls()}
    assert all(item["total_tokens"] == 40 for item in case.model_calls())
    for provider in case.providers.values():
        observed = json.dumps(provider.seen, ensure_ascii=False)
        assert "SECRET_ORACLE_ANSWER_NEVER_MODEL_CONTEXT" not in observed
        assert str(case.oracle) not in observed
        assert "hidden.cpp" not in observed
    return bundles[0]


@pytest.mark.asyncio
async def test_bug_fix_first_pass_delivery_is_based_on_real_validation(tmp_path, git_exe):
    case = await make_loop_case(tmp_path, git_exe)
    try:
        with pytest.raises(ForgePilotError):
            await case.service._repository(case.task_id).read_file(str(case.oracle / "hidden.cpp"))
        result = await case.service.approve(case.approval)
        bundle = assert_delivered(case, result)
        assert set(bundle.content.changed_files) == {"src/value.cpp"}
        assert case.providers["repair"].seen == []
        case.save_evidence("bug-fix-first-pass", result)
        repeated = await case.service.run(case.task_id)
        assert repeated.status is NodeStatus.SUCCEEDED
        assert len(case.artifacts("DeliveryBundle")) == 1
    finally:
        await case.close()


@pytest.mark.asyncio
async def test_cross_file_feature_first_pass_delivery_with_new_test(tmp_path, git_exe):
    case = await make_loop_case(tmp_path, git_exe, task_type="feature", implement=[
        patch(after=FEATURE), create("src/range.hpp", FEATURE_HEADER),
        create("tests/feature.cpp", FEATURE_TEST),
        patch(CMAKE, FEATURE_CMAKE, "CMakeLists.txt"), done(),
    ])
    try:
        result = await case.service.approve(case.approval)
        bundle = assert_delivered(case, result)
        assert set(bundle.content.changed_files) == {"src/value.cpp", "src/range.hpp", "tests/feature.cpp", "CMakeLists.txt"}
        assert case.artifacts("ValidationReport")[-1].content.tests_passed == 2
        case.save_evidence("feature-cross-file", result)
    finally:
        await case.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("broken,failure_phase", [
    (COMPILE_BROKEN, "build"), (TEST_BROKEN, "test"), (ORACLE_BROKEN, "protected_test"),
])
async def test_code_failure_repairs_then_repeats_entire_pipeline(tmp_path, git_exe, broken, failure_phase):
    case = await make_loop_case(tmp_path, git_exe, implement=[patch(after=broken), done()],
                               repair=[patch(broken, FIXED), done()])
    try:
        result = await case.service.approve(case.approval)
        assert_delivered(case, result, rounds=1)
        first, final = case.artifacts("ValidationReport")
        assert first.content.failure_classification == "code_failure"
        assert any(command.kind == failure_phase and command.exit_code != 0 for command in first.content.commands)
        assert {"configure", "build", "test", "protected_test"} <= {command.kind for command in final.content.commands}
        repair_context = json.loads(case.providers["repair"].seen[0][0][1]["content"])
        assert repair_context["remaining_repair_rounds"] == 3
        assert repair_context["previous_change_set"]
        assert repair_context["validation_report"]["failure_classification"] == "code_failure"
        assert repair_context["failure_summary"] and repair_context["allowed_paths"]
        assert "repair" in {item["role"] for item in case.model_calls()}
        checkpoints = case.service.runner.checkpoints.list_for_task(case.task_id)
        assert any("repair" in str(item) for item in checkpoints)
        if failure_phase == "build":
            case.save_evidence("bug-fix-compile-repair", result)
        else:
            case.save_evidence(f"bug-fix-{failure_phase}-repair", result)
    finally:
        await case.close()


@pytest.mark.asyncio
async def test_infrastructure_pauses_without_spending_repair_then_retries(tmp_path, git_exe):
    case = await make_loop_case(tmp_path, git_exe)
    try:
        generator = case.validator.generator
        case.validator.generator = "ForgePilot unavailable generator"
        result = await case.service.approve(case.approval)
        assert result.status is NodeStatus.RETRYABLE, result
        report = case.artifacts("ValidationReport")[-1].content
        assert report.failure_classification == "infrastructure_failure"
        assert case.providers["repair"].seen == case.providers["review"].seen == []
        assert not case.artifacts("DeliveryBundle")
        case.save_evidence("infrastructure-pause", result)
        case.validator.generator = generator
        result = await case.service.resume_infrastructure(case.task_id)
        assert result.status is NodeStatus.SUCCEEDED, result
        assert case.providers["repair"].seen == []
        assert len(case.artifacts("ValidationReport")) == 2
        attempts = case.events._db.execute(
            "SELECT attempt FROM validation_runs WHERE task_id=? ORDER BY attempt", (case.task_id,)).fetchall()
        assert [row[0] for row in attempts] == [1, 2]
        assert case.artifacts("DeliveryBundle")
    finally:
        await case.close()


@pytest.mark.asyncio
async def test_policy_violation_blocks_without_repair(tmp_path, git_exe, monkeypatch):
    case = await make_loop_case(tmp_path, git_exe)
    original_validate = case.validator.validate

    async def unauthorized_write(*args, **kwargs):
        (case.worktree / "README.txt").write_text("Unauthorized external mutation\n", encoding="utf-8")
        return await original_validate(*args, **kwargs)

    monkeypatch.setattr(case.validator, "validate", unauthorized_write)
    try:
        result = await case.service.approve(case.approval)
        assert result.status is NodeStatus.FAILED, result
        assert case.artifacts("ValidationReport")[-1].content.failure_classification == "policy_failure"
        assert case.providers["repair"].seen == case.providers["review"].seen == []
        assert not case.artifacts("DeliveryBundle")
        case.save_evidence("policy-block", result)
    finally:
        await case.close()


@pytest.mark.asyncio
async def test_reviewer_blocking_issue_uses_the_shared_repair_budget(tmp_path, git_exe):
    improved = FIXED + "// Preserve positive values and clamp the negative boundary.\n"
    case = await make_loop_case(tmp_path, git_exe,
        repair=[patch(FIXED, improved), done()], reviews=[review_response(approve=False), review_response()])
    try:
        result = await case.service.approve(case.approval)
        assert_delivered(case, result, rounds=1)
        assert len(case.artifacts("ReviewReport")) == 2
        assert case.artifacts("ReviewReport")[0].content.blocking_issues
        payload = json.loads(case.providers["repair"].seen[0][0][1]["content"])
        assert payload["remaining_repair_rounds"] == 3
        assert payload["failure_source"] == "reviewer"
        assert "RI-1" in payload["failure_summary"]
    finally:
        await case.close()


@pytest.mark.asyncio
async def test_validation_and_review_share_three_rounds_and_exhaust_fail_closed(tmp_path, git_exe):
    first = FIXED + "// First requested review detail.\n"
    second = first + "// Second requested review detail.\n"
    case = await make_loop_case(tmp_path, git_exe,
        implement=[patch(after=COMPILE_BROKEN), done()],
        repair=[patch(COMPILE_BROKEN, FIXED), done(), patch(FIXED, first), done(), patch(first, second), done()],
        reviews=[review_response(approve=False)] * 3)
    try:
        result = await case.service.approve(case.approval)
        assert result.status is NodeStatus.FAILED, result
        assert len(case.providers["repair"].seen) == 6
        assert len(case.artifacts("ValidationReport")) == 4
        assert len(case.artifacts("ReviewReport")) == 3
        assert not case.artifacts("DeliveryBundle")
        assert "exhaust" in str(result).lower() or "budget" in str(result).lower()
    finally:
        await case.close()


@pytest.mark.asyncio
async def test_cancelling_review_never_produces_delivery(tmp_path, git_exe):
    reviewer = FakeProvider(block=True)
    case = await make_loop_case(tmp_path, git_exe, review_provider=reviewer)
    work = asyncio.create_task(case.service.approve(case.approval))
    try:
        await asyncio.wait_for(reviewer.started.wait(), timeout=120)
        snapshot = case.service.runner.store.snapshot(case.task_id)
        case.service.cancel(case.task_id, expected_revision=snapshot.revision)
        result = await asyncio.wait_for(work, timeout=10)
        assert result.status is NodeStatus.CANCELLED
        assert reviewer.cancelled.is_set()
        assert not case.artifacts("DeliveryBundle")
    finally:
        if not work.done():
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
        await case.close()


@pytest.mark.asyncio
async def test_cancelling_ctest_terminates_real_test_process_before_return(tmp_path, git_exe):
    from test_build_process import alive

    case = await make_loop_case(tmp_path, git_exe, slow_test=True)
    work = asyncio.create_task(case.service.approve(case.approval))
    try:
        marker = case.validator.guard.root / case.task_id / "build" / "started.txt"
        async with asyncio.timeout(120):
            while not marker.exists():
                assert not work.done(), work.result() if work.done() else None
                await asyncio.sleep(0.02)
        pid = int(marker.read_text())
        assert alive(pid)
        snapshot = case.service.runner.store.snapshot(case.task_id)
        case.service.cancel(case.task_id, expected_revision=snapshot.revision)
        result = await asyncio.wait_for(work, timeout=10)
        assert result.status is NodeStatus.CANCELLED
        assert not alive(pid), "CTest child must be gone before cancellation is returned"
        assert not case.artifacts("DeliveryBundle")
        assert case.providers["repair"].seen == case.providers["review"].seen == []
    finally:
        if not work.done():
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
        await case.close()
