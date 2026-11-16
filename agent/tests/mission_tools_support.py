"""Production Mission composition with offline models and a real local MCP peer."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from forgepilot.api import create_app
from forgepilot.code_intelligence import CodeIntelligenceConfig
from forgepilot.llm import ChatReply
from forgepilot.mission_factory import MissionDependencies, build_mission_workflow
from forgepilot.models.gateway import ModelGateway
from forgepilot.models.profiles import ModelConfiguration, ModelProfile, ModelRoute
from forgepilot.presets import BuildConfig
from forgepilot.process import executable
from forgepilot.rag.config import RagConfig, RagRuntimeAvailability
from forgepilot.rag.integration import RagManager, repository_id_for
from forgepilot.rag.models import CodeChunk, RetrievedChunk
from forgepilot.rag.store import RagFileStore
from forgepilot.sqlite_store import SqliteEventStore

SOURCE = "int value(int input) { return input; }\n"
PEER = Path(__file__).resolve().parents[1] / "examples/mcp_project_notes.py"


def response(content=None, calls=None):
    return ChatReply.model_validate({"message": {"role": "assistant", "content": content,
        "tool_calls": calls}, "usage": {"prompt_tokens": 20, "completion_tokens": 20, "total_tokens": 40}})


def tool(name, arguments, identifier):
    return {"id": identifier, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments)}}


class ToolsProvider:
    def __init__(self):
        self.seen = []

    async def chat(self, messages, tools=None):
        payload = json.loads(next(m["content"] for m in messages if m["role"] == "user"))
        names = tuple(item.name for item in tools or ())
        self.seen.append({"names": names, "schemas": [item.model_dump(mode="json") for item in tools or ()],
                          "messages": deepcopy(messages)})
        if "request" in payload:
            return response(json.dumps({"task_type": "bug_fix", "title": "Inspect current value function",
                "objective": "Review local code and plan a verified boundary fix",
                "repository": payload["repository"], "problem": {
                    "observed_behavior": "Negative values are unchanged", "expected_behavior": "Negative values become zero",
                    "reproduction_steps": ["Call value with a negative value"]},
                "acceptance_criteria": [
                    {"id": "AC-1", "description": "Boundary regression passes", "verification": "ctest"},
                    {"id": "AC-2", "description": "CMake target builds", "verification": "build"}],
                "allowed_scope": {"path_patterns": ["src/value.cpp"], "max_changed_files": 1},
                "forbidden_scope": [".git/**", "build/**", ".forgepilot/**"],
                "test_policy": {"existing_tests_read_only": True, "allow_new_tests": True,
                                "modifiable_existing_test_patterns": []},
                "data_policy": payload["data_policy"], "assumptions": ["User reviews the plan before execution"],
                "risks": ["Boundary behavior may affect callers"], "risk_level": "low"}))
        if "evidence" in payload:
            assert not any(name.startswith("mcp_") for name in names), names
            return response(json.dumps({"task_spec_artifact_id": payload["task_spec"]["artifact_id"],
                "summary": "Fix the current value function and run the existing checks",
                "steps": [{"step_id": "STEP-1", "owner": "implementer.v1", "depends_on": [],
                    "expected_files": ["src/value.cpp"], "action": "Clamp negative input and verify existing tests",
                    "verification": ["ctest", "build"], "risk": "Boundary compatibility", "adds_tests": False,
                    "acceptance_criteria_ids": ["AC-1", "AC-2"]}]}))
        observed = [message for message in messages if message["role"] == "tool"]
        if not observed:
            calls = [tool("repository_get_symbol", {"path": "src/value.cpp", "line": 1, "column": 5}, "symbol")]
            if "rag_retrieve" in names:
                calls.append(tool("rag_retrieve", {"query": "value", "path_filters": [], "languages": [], "top_k": 8}, "rag"))
            selected = [name for name in names if name.startswith("mcp_")]
            calls.extend(tool(name, {"topic": "architecture"}, "external") for name in selected)
            assert {item["function"]["name"] for item in calls} <= set(names), names
            return response(calls=calls)
        items = [{"evidence_id": "EV-1", "claim": "value returns its input unchanged",
            "reason": "The current source line directly returns input", "confidence": 0.9,
            "path": "src/value.cpp", "start_line": 1, "end_line": 1, "symbol": "value",
            "file_hash": sha256(SOURCE.encode()).hexdigest(), "source": "local_repository"}]
        for message in observed:
            content = json.loads(message["content"])
            if "evidence_location" in content:
                items.append({"evidence_id": "EV-2", "claim": "External notes describe approval before changes",
                    "reason": "The selected local MCP tool returned reference data", "confidence": 0.7,
                    **content["evidence_location"]})
        return response(json.dumps({"items": items, "summary": "Current worktree and external reference evidence"}))

    async def aclose(self):
        pass


class OfflineEmbeddings:
    def __init__(self, *, fail=False):
        self.calls = 0
        self.fail = fail

    async def embed_query(self, text):
        self.calls += 1
        if self.fail:
            raise RuntimeError("offline embedding failure")
        return [1.0, 0.0]


class SourceVectors:
    def __init__(self, source, *, fail=False):
        self.source, self.fail, self.calls = source, fail, 0

    async def search(self, repository_id, vector, *, limit):
        self.calls += 1
        if self.fail:
            raise RuntimeError("offline vector failure")
        assert repository_id == repository_id_for(self.source)
        return [RetrievedChunk(chunk=CodeChunk(repository_id=repository_id, path="src/value.cpp",
            language="cpp", start_line=1, end_line=1, content=SOURCE.rstrip("\n"),
            file_hash=sha256(SOURCE.encode()).hexdigest()), score=0.9)]

    async def aclose(self):
        pass


class MissionToolsCase:
    def __init__(self, root, *, git_exe, rg_exe, embedding_failure=False, vector_failure=False,
                 rag_enabled=True, source=None):
        self.root = Path(root)
        self.source = Path(source) if source is not None else self.root / "source"
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.source.exists():
            (self.source / "src").mkdir(parents=True)
            (self.source / "src/value.cpp").write_text(SOURCE, encoding="utf-8")
            (self.source / "CMakeLists.txt").write_text(
                "cmake_minimum_required(VERSION 3.24)\nproject(ToolFixture LANGUAGES CXX)\nenable_testing()\n"
                "add_library(value src/value.cpp)\n", encoding="utf-8")
            for args in (("init", "-q", "-b", "main"), ("add", "."),
                         ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                          "commit", "-qm", "offline tool fixture")):
                subprocess.run([git_exe, "-C", str(self.source), *args], check=True, capture_output=True)
        self.store = SqliteEventStore(self.root / "mission.sqlite3", artifact_root=self.root / "artifacts")
        self.provider = ToolsProvider()
        configuration = ModelConfiguration(profiles=tuple(ModelProfile(profile_id=name,
            base_url="http://127.0.0.1:1/v1", model=f"offline-{name}", family=name,
            api_key_ref="env:FORGEPILOT_API_KEY", max_output_tokens=1024) for name in ("writer", "reviewer")),
            routes=tuple(ModelRoute(route_id=name, primary="reviewer" if name == "review" else "writer")
                for name in ("task", "chat", "summary", "orchestration", "analysis", "implementation", "review", "repair")))
        self.gateway = ModelGateway(configuration, secret_resolver=lambda _: "offline-fixture-key",
                                    provider_factory=lambda *_: self.provider)
        self.embeddings = OfflineEmbeddings(fail=embedding_failure)
        self.vectors = SourceVectors(self.source, fail=vector_failure)
        config = RagConfig(enabled=rag_enabled, embedding_model="offline-fixture",
                           embedding_base_url="http://127.0.0.1:1/v1")
        self.rag = RagManager(RagRuntimeAvailability("unavailable", config=config),
            RagFileStore(self.store), embedding_provider=self.embeddings, vector_store=self.vectors)
        self.workflows = []

        def no_legacy(trace):
            raise AssertionError("This integration uses production API v2 Missions only")

        def factory(events, *, mcp_snapshots=()):
            dependencies = MissionDependencies(gateway=self.gateway, events=events,
                rg_executable=rg_exe, git_executable=git_exe,
                build_config=BuildConfig(cmake=executable(None, "cmake"), ctest=executable(None, "ctest")),
                redactor=self.gateway.redactor,
                mcp_connection_factory=self.app.state.task_host.mcp_service.connection_factory,
                rag_manager=self.rag,
                intelligence_config=CodeIntelligenceConfig(clangd=str(self.root / "missing-clangd.exe")))
            workflow = build_mission_workflow(dependencies, mcp_snapshots=mcp_snapshots)
            self.workflows.append(workflow)
            return workflow

        self.app = create_app(no_legacy, store=self.store, mission_factory=factory,
                              heartbeat_seconds=0.1, model_client_closer=self.gateway.aclose)
        self.requests = []

        @self.app.middleware("http")
        async def capture(request, call_next):
            result = await call_next(request)
            self.requests.append({"method": request.method, "path": request.url.path,
                                  "status": result.status_code})
            return result

    async def seed_mcp(self):
        service = self.app.state.task_host.mcp_service
        existing = service.list_servers()["items"]
        if existing:
            return existing[0]["server_id"]
        candidate = {"name": "Offline project notes", "transport": "stdio", "modes": ["task"],
            "selected_tools": ["project_info"], "effect": "read", "enabled": True,
            "connection": {"command": sys.executable, "args": ["-I", "-u", str(PEER)],
                           "cwd": str(self.root)}}
        probe = await service.probe(candidate)
        saved = await service.save(str(uuid4()), candidate, base_revision=0, catalog_hash=probe["catalog_hash"])
        return saved["server_id"]

    def evidence(self):
        return {"requests": self.requests, "model_tools": [entry["names"] for entry in self.provider.seen],
                "model_schemas": [entry["schemas"] for entry in self.provider.seen],
                "model_tool_responses": [[json.loads(message["content"]) for message in entry["messages"]
                    if message["role"] == "tool"] for entry in self.provider.seen],
                "embedding_calls": self.embeddings.calls, "vector_calls": self.vectors.calls}
