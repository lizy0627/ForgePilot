"""Optional MCP outages preserve local analysis; permission/catalog errors do not."""
from copy import deepcopy
import json
from uuid import uuid4

import pytest

from conftest import call, reply
from forgepilot.agents.analysis import AnalysisRequest
from forgepilot.agents.contracts import validate_task_spec
from forgepilot.artifacts.models import DataPolicy, RepositorySpec
from forgepilot.errors import ForgePilotError
from forgepilot.mission_events import publish_mission_event
from forgepilot.orchestration import validate_graph
from forgepilot.orchestration.models import NodeStatus
from forgepilot.tools.mcp_tools import MCPToolAdapter
from test_analysis_workflow import AnalysisProvider, service_for, spec_data


class FailingSource:
    def __init__(self, code):
        self.code, self.calls = code, []

    async def call_tool(self, name, arguments):
        self.calls.append((name, deepcopy(arguments)))
        raise ForgePilotError(self.code, "Local MCP fixture failed")


class FallbackAnalyst(AnalysisProvider):
    def __init__(self, root, alias, error_code):
        super().__init__(root)
        self.alias, self.error_code = alias, error_code
        self.root_turns = 0
        self.root_requests = []
        self.both.set()

    async def chat(self, messages, tools=None):
        if "You are root-cause-analyst.v1" in messages[0]["content"]:
            self.root_turns += 1
            self.root_requests.append(deepcopy(messages))
            assert self.alias in {tool.name for tool in tools}
            if self.root_turns == 1:
                return reply(calls=[call(self.alias, '{"query":"Counter lifecycle"}', id="external-notes")])
            observation = next(message["content"] for message in reversed(messages) if message["role"] == "tool")
            if self.root_turns == 2:
                failure = json.loads(observation)
                assert failure["error_code"] == self.error_code and failure["available"] is False
                return reply(calls=[call("repository_read_range", '{"path":"Counter.cpp"}', id="local-range")])
            assert "Counter" in observation
        return await super().chat(messages, tools)


@pytest.mark.asyncio
@pytest.mark.parametrize("code,continued", [
    ("mcp_unavailable", True),
    ("mcp_catalog_changed", False),
    ("policy_denied", False),
])
async def test_mission_analyst_mcp_outage_continues_locally_but_boundary_errors_stop(
        cpp_repo, tmp_path, code, continued):
    source = FailingSource(code)
    tool = MCPToolAdapter(server_id=uuid4(), remote_tool_name="lookup",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}},
                      "required": ["query"], "additionalProperties": False},
        annotations={"readOnlyHint": True}, connection=source, approved=True)
    provider = FallbackAnalyst(cpp_repo, tool.name, code)
    service, _, events, gateway = await service_for(cpp_repo, tmp_path, provider=provider)
    service.approved_mcp, service.approved_server_ids = (tool,), (tool.server_id,)
    payload = spec_data()
    payload["data_policy"]["external_mcp_allowed"] = True
    frozen = validate_task_spec(payload)
    request = AnalysisRequest(request="Inspect lifecycle using optional external notes",
        repository=RepositorySpec(base_ref="HEAD", toolchain_profile="windows-msvc-qt6"),
        data_policy=DataPolicy(cloud_model_allowed=True, external_mcp_allowed=True))
    task = str(uuid4())
    graph = service.graph(request, locked_task_spec=frozen)
    service.runner.store.create_run(task, validate_graph(graph), policy_hash=service.runner.policy.definition_hash)
    publish_mission_event(events, task, "task.created", {"tool_context": {
        "mcp": {"used": False, "server_ids": [tool.server_id], "fallback_reasons": []}}})
    before = service.repository.audit_manifest()
    try:
        result = await service.run(task, request, locked_task_spec=frozen)
        assert len(source.calls) == 1
        context = events.missions.tool_context(task, events.missions.metadata(task))
        assert context["mcp"]["used"] is False
        assert context["mcp"]["fallback_reasons"] == [code]
        if continued:
            assert result.status is NodeStatus.AWAITING_HUMAN, [
                (n.node_id, n.result.error_summary) for n in result.nodes
                if n.result and n.result.status is NodeStatus.FAILED]
            assert provider.root_turns == 3
            assert events.artifacts.get(service.review(task).plan_artifact_id).content.steps
            local_calls = events._db.execute("SELECT payload_json FROM graph_events "
                "WHERE task_id=? AND event_type='tool.completed'", (task,)).fetchall()
            assert any(json.loads(row[0])["tool"] == "repository_read_range" for row in local_calls)
        else:
            assert result.status is NodeStatus.FAILED
            assert provider.root_turns == 1
            assert not events._db.execute("SELECT 1 FROM graph_events WHERE task_id=? "
                "AND event_type='tool.completed' AND json_extract(payload_json,'$.tool')='repository_read_range'",
                (task,)).fetchone()
        assert service.repository.audit_manifest() == before
    finally:
        await gateway.aclose()
        events.close()
