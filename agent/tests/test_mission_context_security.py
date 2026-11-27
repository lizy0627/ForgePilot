"""Mission context and explicit MCP admission stay outside model-controlled data."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from forgepilot.agents.analysis import AnalysisService
from forgepilot.agents.delivery import DeliveryWorkflow
from forgepilot.api_v2_models import CreateMission
from forgepilot.errors import ForgePilotError
from forgepilot.mission_host import MissionHost


def _snapshot(server_id, *, read_only=True, secret_error=None, revision=3):
    return SimpleNamespace(
        server_id=server_id, revision=revision, secret_error=secret_error,
        config=SimpleNamespace(selected_tools=("lookup",)),
        catalog=[{"name": "lookup", "compatible": read_only,
                  "annotations": {"readOnlyHint": read_only}}],
    )


def test_mission_mcp_requires_exact_user_selected_task_server():
    selected, other = str(uuid4()), str(uuid4())
    available = [_snapshot(selected), _snapshot(other)]
    service = SimpleNamespace(snapshots=lambda mode: available if mode == "task" else [])
    host = MissionHost.__new__(MissionHost)
    host.mcp_disabled, host.mcp_service = False, service

    assert host._selected_mcp_snapshots(()) == ()
    assert host._selected_mcp_snapshots((selected,)) == (available[0],)
    assert host._selected_mcp_snapshots((selected,), (3,)) == (available[0],)
    for ids in ((str(uuid4()),), (selected, selected), ("not-a-uuid",)):
        with pytest.raises(ForgePilotError):
            host._selected_mcp_snapshots(ids)
    with pytest.raises(ForgePilotError, match="mcp_catalog_changed"):
        host._selected_mcp_snapshots((selected,), (2,))

    host.mcp_disabled = True
    with pytest.raises(ForgePilotError, match="policy_denied"):
        host._selected_mcp_snapshots((selected,))
    host.mcp_disabled = False
    available[0] = _snapshot(selected, read_only=False)
    with pytest.raises(ForgePilotError, match="mcp_selection"):
        host._selected_mcp_snapshots((selected,))


def test_selected_mcp_snapshots_reach_custom_production_factory():
    selected = str(uuid4())
    snapshot = _snapshot(selected)
    captured = []
    host = MissionHost.__new__(MissionHost)
    host.mcp_disabled = False
    host.mcp_service = SimpleNamespace(snapshots=lambda mode: (snapshot,))
    host.store = object()
    host._services = {}
    host.factory = lambda events, *, mcp_snapshots=(): captured.append(mcp_snapshots) or SimpleNamespace()

    workflow = host._new_workflow((selected,))
    assert captured == [(snapshot,)]
    assert workflow.mission_mcp_revisions == (3,)
    assert not host._services
    host.factory = lambda events: SimpleNamespace()
    with pytest.raises(ForgePilotError, match="mcp_selection"):
        host._new_workflow((selected,))
    snapshot = _snapshot(selected, secret_error="mcp_secret_unavailable")
    with pytest.raises(ForgePilotError, match="mcp_selection"):
        host._selected_mcp_snapshots((selected,))


def test_direct_analysis_and_delivery_artifact_inputs_reject_other_task():
    foreign = SimpleNamespace(task_id="other-task", content=object())
    events = SimpleNamespace(artifacts=SimpleNamespace(get=lambda _: foreign))
    context = SimpleNamespace(task_id="this-task", input_artifact_ids=("foreign-id",))

    analysis = AnalysisService.__new__(AnalysisService)
    analysis.events = events
    with pytest.raises(ForgePilotError, match="artifact_scope"):
        analysis._inputs(context, object)

    delivery = DeliveryWorkflow.__new__(DeliveryWorkflow)
    delivery.events = events
    with pytest.raises(ForgePilotError, match="artifact_scope"):
        delivery._package("this-task", ("foreign-id",))


@pytest.mark.parametrize("extra", ["conversation_id", "chat_history", "memory", "memories",
                                    "capabilities", "approval", "scope"])
def test_mission_transport_rejects_chat_context_and_model_grants(extra):
    payload = {"repository_path": "C:\\trusted-repository", "request": "Fix the build",
               "language": "en-US", extra: "injected"}
    with pytest.raises(ValidationError):
        CreateMission.model_validate(payload)

    accepted = CreateMission.model_validate({"repository_path": "C:\\trusted-repository",
                                             "request": "Fix the build", "language": "en-US"})
    assert accepted.language == "en-US"
    assert accepted.mcp_server_ids == []
