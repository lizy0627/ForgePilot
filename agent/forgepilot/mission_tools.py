"""Frozen Mission tool choices and task-worktree service binding."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .artifacts.models import DataPolicy
from .errors import ForgePilotError


@dataclass(frozen=True)
class MissionToolSelection:
    use_rag: bool
    data_policy: DataPolicy
    mcp_servers: tuple[dict, ...] = ()

    def to_dict(self):
        return {"version": 1, "use_rag": self.use_rag,
                "data_policy": self.data_policy.model_dump(mode="json"),
                "mcp_servers": list(self.mcp_servers)}

    @classmethod
    def from_dict(cls, value):
        try:
            if (not isinstance(value, dict) or value.get("version") != 1
                    or type(value.get("use_rag")) is not bool
                    or not isinstance(value.get("mcp_servers"), list)
                    or len(value["mcp_servers"]) > 4):
                raise ValueError()
            for server in value["mcp_servers"]:
                if (not isinstance(server, dict) or set(server) != {"server_id", "name", "tools"}
                        or not isinstance(server["server_id"], str) or not isinstance(server["name"], str)
                        or not isinstance(server["tools"], list)
                        or not all(isinstance(tool, str) for tool in server["tools"])):
                    raise ValueError()
            return cls(value["use_rag"], DataPolicy.model_validate(value["data_policy"]),
                       tuple(value["mcp_servers"]))
        except (KeyError, TypeError, ValueError):
            raise ForgePilotError("mission_frozen_config_missing", "Frozen Mission tool selection is invalid") from None



def bind_mission_tools(workflow, task_id, selection: MissionToolSelection):
    """Bind borrowed resources after the confirmed source baseline exists."""
    from .agents.implementation import ImplementationService
    from .code_intelligence import CodeIntelligence
    from .validation import ValidationService
    from .rag.mission import build_mission_retrieval

    workflow.mission_tool_selection = selection.to_dict()
    context = {
        "rag": {"requested": selection.use_rag, "enabled": False, "used": False, "fallback_reasons": []},
        "clangd": {"enabled": True, "used": False, "fallback_reasons": []},
        "mcp": {"server_ids": [s["server_id"] for s in selection.mcp_servers],
                "servers": list(selection.mcp_servers), "used": False, "fallback_reasons": []},
    }
    if not isinstance(workflow, ImplementationService):
        return context  # Explicit offline/custom workflows need no repository tools.
    record = workflow.workspace.record(task_id)
    if workflow.events.missions.is_admitted(task_id):
        for component, previous in workflow.events.missions.tool_context(task_id,
                workflow.events.missions.metadata(task_id)).items():
            context.setdefault(component, {}).update(previous)

    def observe(component):
        def publish(status):
            # Status contains only capability/source metadata, never code or queries.
            safe = {key: status[key] for key in ("requested", "enabled", "used", "fallback_reasons") if key in status}
            safe["used"] = context[component].get("used", False) or safe.get("used", False)
            context[component].update(safe)
            workflow.events.missions.record_observation(task_id, "task.tools_updated",
                {"component": component, "status": safe}, node_id=None, actor="mission-tools")
        return publish

    repository = workflow._repository(task_id)
    intelligence = CodeIntelligence(repository, getattr(workflow, "mission_intelligence_config", None),
        selected_directory=lambda: (workflow.validator.compilation_directory(task_id)
                                   if isinstance(workflow.validator, ValidationService) else None),
        on_status=observe("clangd"))
    context["clangd"].update({key: value for key, value in intelligence.availability().items() if key != "used"})
    workflow.analysis_kwargs["intelligence"] = intelligence
    if selection.use_rag:
        def retrieval_factory(task_repository):
            return build_mission_retrieval(getattr(workflow, "mission_rag_manager", None),
                Path(record["repository_path"]), task_repository, data_policy=selection.data_policy,
                code_intelligence=intelligence, on_status=observe("rag"))
        retrieval = retrieval_factory(repository)
        workflow.analysis_kwargs["retrieval"] = retrieval
        workflow.retrieval_factory = retrieval_factory
        context["rag"].update({key: value for key, value in retrieval.status().items() if key not in {"trace", "used"}})
    return context
