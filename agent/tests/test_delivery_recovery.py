"""Bundle preparation is replayable; recovery cannot approve an empty result."""
from pathlib import Path

import pytest

from closed_loop_support import make_loop_case
from forgepilot.agents.delivery import DeliveryWorkflow, validation_passed
from forgepilot.artifacts.models import DeliveryBundle
from forgepilot.errors import ForgePilotError
from forgepilot.orchestration.models import NodeStatus
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.validation import ValidationService
from forgepilot.workspace_manager import WorkspaceManager


class DeliveryProcessCrash(BaseException):
    """Model a process loss without converting it to a retryable tool error."""


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["before_bundle", "after_artifact"])
async def test_interrupted_bundle_replays_verification_and_never_succeeds_with_empty_outputs(
        tmp_path, git_exe, monkeypatch, boundary):
    case = await make_loop_case(tmp_path, git_exe)
    try:
        with monkeypatch.context() as crash:
            if boundary == "before_bundle":
                def fail(*args, **kwargs):
                    raise DeliveryProcessCrash()
                crash.setattr(case.manager, "prepare_delivery_bundle", fail)
            else:
                save = case.service._save

                def fail_after_artifact(context, content, *args, **kwargs):
                    artifact = save(context, content, *args, **kwargs)
                    if isinstance(content, DeliveryBundle):
                        raise DeliveryProcessCrash()
                    return artifact
                crash.setattr(case.service, "_save", fail_after_artifact)
            with pytest.raises(DeliveryProcessCrash):
                await case.service.approve(case.approval)

        interrupted = case.service.runner.store.snapshot(case.task_id)
        node = next(item for item in interrupted.nodes if item.node_id == "delivery_bundle")
        assert node.status is NodeStatus.RUNNING and node.result is None
        assert len(case.artifacts("DeliveryBundle")) == (boundary == "after_artifact")
        calls = {name: len(provider.seen) for name, provider in case.providers.items()}
        committed = case.manager.inspect_task(case.task_id)
        database, data_root = case.events.path, case.manager.root
        artifact_root = case.events.artifacts.root
        previous_validator = case.validator
        case.events.close()

        # Recreate the durable stores and services, including empty model budgets.
        # Already successful writers/reviewers must never be invoked again.
        case.manager = WorkspaceManager(data_root, git_executable=git_exe)
        case.events = SqliteEventStore(database, workspace_backend=case.manager, artifact_root=artifact_root)
        case.validator = ValidationService(case.manager, previous_validator.profile,
            build_root=previous_validator.guard.root, oracle=previous_validator.oracle,
            generator=previous_validator.generator, architecture=previous_validator.architecture)
        case.service = DeliveryWorkflow(case.events, case.manager, case.gateway, validator=case.validator)
        graph = case.service._stored_graph(case.task_id)
        snapshot = case.service.runner.store.snapshot(case.task_id)
        assert snapshot.status is NodeStatus.INTERRUPTED
        assert (await case.service.run(case.task_id)).status is NodeStatus.INTERRUPTED

        plan = case.service.runner.prepare_recovery(case.task_id, graph)
        assert plan.blocked_reason is None
        action = next(item for item in plan.actions if item["node_id"] == "delivery_bundle")
        assert action["action"] == "retry_frozen"
        restored = case.service.runner.confirm_recovery(
            case.task_id, graph, token=plan.token, expected_revision=plan.revision)
        node = next(item for item in restored.nodes if item.node_id == "delivery_bundle")
        assert node.status is NodeStatus.READY
        with pytest.raises(ForgePilotError, match="not awaiting"):
            case.service.runner.approve_human(case.task_id, graph, "delivery_bundle",
                                             expected_revision=restored.revision)

        result = await case.service.run(case.task_id)
        assert result.status is NodeStatus.SUCCEEDED
        node = next(item for item in result.nodes if item.node_id == "delivery_bundle")
        assert node.attempt == 2 and len(node.result.output_artifact_ids) == 1
        artifact = case.events.artifacts.get(node.result.output_artifact_ids[0])
        assert isinstance(artifact.content, DeliveryBundle)
        bundle = artifact.content
        assert bundle.commit == committed.head_commit
        assert case.manager.inspect_task(case.task_id) == committed
        assert Path(bundle.patch_path).read_bytes()
        validation = case.events.artifacts.get(bundle.validation_report_artifact_id)
        review = case.events.artifacts.get(bundle.review_report_artifact_id)
        assert validation_passed(validation.content) and review.content.recommend_delivery
        assert bundle.change_set_artifact_id in validation.parent_artifact_ids
        assert validation.artifact_id in review.parent_artifact_ids
        assert calls == {name: len(provider.seen) for name, provider in case.providers.items()}
        assert (await case.service.run(case.task_id)).status is NodeStatus.SUCCEEDED
        assert next(item for item in case.service.runner.store.snapshot(case.task_id).nodes
                    if item.node_id == "delivery_bundle").result == node.result
    finally:
        await case.close()
