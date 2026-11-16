"""Confirmed source snapshots precede every Agent, without modifying the user's tree."""
from hashlib import sha256
from dataclasses import replace
import json

import httpx
import pytest

from closed_loop_support import ORIGINAL, make_loop_case
from conftest import reply
from forgepilot.agents.delivery import DeliveryWorkflow
from forgepilot.api import create_app
from forgepilot.errors import ForgePilotError
from forgepilot.models.gateway import ModelGateway
from forgepilot.models.profiles import ModelConfiguration, ModelProfile, ModelRoute
from forgepilot.orchestration.models import GraphDefinition
from forgepilot.workspace_manager import WorkspaceManager
from test_mission_api_workflow import AnalysisProvider, poll
from test_mission_service import create, setup, settled
from test_workspace_manager import committed_repo, git, user_state


def source_fields(root, paths=()):
    return dict(repository_path=str(root), base_ref="HEAD", baseline_paths=list(paths))


def test_source_baseline_commit_cannot_move_main_when_task_head_becomes_symbolic(tmp_path, git_exe, committed_repo, monkeypatch):
    from pathlib import Path

    manager = WorkspaceManager(tmp_path / "controlled", git_executable=git_exe)
    state = manager.create(committed_repo, "source-race")
    worktree = Path(state["worktree_path"])
    (committed_repo / "source.txt").write_bytes(b"confirmed user source\n")
    prepared = manager.prepare_baseline("source-race", ["source.txt"])
    applied = manager.apply_baseline("source-race", prepared["sha256"], approved=True,
                                     expected=manager.inspect_task("source-race"))
    before = user_state(git_exe, committed_repo)
    main_ref = git(git_exe, committed_repo, "rev-parse", "refs/heads/main")
    main_head = (committed_repo / ".git/HEAD").read_bytes()
    task_entries = git(git_exe, worktree, "ls-files", "--stage", "-z")
    commit = manager._commit
    injected = []

    def switch_task_head(task_git, tree, parent, message):
        result = commit(task_git, tree, parent, message)
        # Simulate the external ref change after the preflight checks. This
        # changes only this worktree's HEAD pointer, before the final ref write.
        git(git_exe, worktree, "symbolic-ref", "HEAD", "refs/heads/main")
        injected.append(result)
        return result

    monkeypatch.setattr(manager, "_commit", switch_task_head)
    try:
        frozen = manager.freeze_source_baseline("source-race", prepared["sha256"], approved=True, expected=applied)
    finally:
        assert git(git_exe, committed_repo, "rev-parse", "refs/heads/main") == main_ref
        assert (committed_repo / ".git/HEAD").read_bytes() == main_head
        assert user_state(git_exe, committed_repo) == before
        assert git(git_exe, worktree, "ls-files", "--stage", "-z") == task_entries
    assert injected == [frozen.head_commit]
    assert frozen.file_hashes == applied.file_hashes
    assert git(git_exe, worktree, "symbolic-ref", "-q", "HEAD", allowed=(0, 1)) == b""
    assert (worktree / "source.txt").read_bytes() == b"confirmed user source\n"


@pytest.mark.asyncio
async def test_preview_head_and_selected_confirmation_preserve_user_files_and_index(tmp_path, git_exe, committed_repo):
    tasks, host = await setup(tmp_path, git_exe)
    root = committed_repo
    (root / "source.txt").write_bytes(b"selected source\n")
    before = git(git_exe, root, "status", "--porcelain=v1")
    original_index = (root / ".git/index").read_bytes()
    try:
        head = await host.preview(**source_fields(root))
        assert head["base_commit"] == git(git_exe, root, "rev-parse", "HEAD").decode().strip()
        assert head["patch"] == "" and "source.txt" in head["changed_files"]
        assert host._running == {}
        made = await create(host, root, preview_token=head["preview_token"])
        await settled(host, made["task_id"])
        managed = host._workflow(made["task_id"]).workspace
        assert (managed.root / "worktrees").is_dir()
        assert (managed.record(made["task_id"])["base_ref"]) == head["base_commit"]
        from pathlib import Path
        assert (Path(managed.record(made["task_id"])["worktree_path"]) / "source.txt").read_bytes() == b"original source\n"
        selected = await host.preview(**source_fields(root, ["source.txt"]))
        assert "+selected source" in selected["patch"]
        with pytest.raises(ForgePilotError, match="baseline_approval"):
            await create(host, root, baseline_paths=["source.txt"], preview_token=selected["preview_token"])
        made = await create(host, root, baseline_paths=["source.txt"], preview_token=selected["preview_token"], baseline_confirmed=True)
        await settled(host, made["task_id"])
        record = managed.record(made["task_id"])
        assert record["base_ref"] != selected["base_commit"]
        assert (Path(record["worktree_path"]) / "source.txt").read_bytes() == b"selected source\n"
        assert host.get(made["task_id"])["source_baseline"]["task_commit"] == record["base_ref"]
        assert git(git_exe, root, "status", "--porcelain=v1") == before
        assert (root / "source.txt").read_bytes() == b"selected source\n"
        assert (root / ".git/index").read_bytes() == original_index
        await host.discard_preview(selected["preview_token"])  # Adopted tokens cannot delete a Mission.
        assert Path(record["worktree_path"]).exists()
    finally:
        await host.close()
        await tasks.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["file", "head", "selection", "trusted_config"])
async def test_stale_preview_requires_reconfirmation_without_analysis(tmp_path, git_exe, committed_repo, changed):
    tasks, host = await setup(tmp_path, git_exe)
    root = committed_repo
    (root / "source.txt").write_bytes(b"selected source\n")
    try:
        preview = await host.preview(**source_fields(root, ["source.txt"]))
        paths = ["source.txt"]
        if changed == "file":
            (root / "source.txt").write_bytes(b"changed after preview\n")
        elif changed == "head":
            git(git_exe, root, "commit", "--allow-empty", "-q", "-m", "new user head")
        elif changed == "selection":
            paths = []
        else:
            build_config = host._build_config
            host._build_config = lambda fields: replace(build_config(fields), configuration="Release")
        code = "build_selection_changed" if changed == "trusted_config" else "baseline_changed"
        with pytest.raises(ForgePilotError, match=code):
            await create(host, root, baseline_paths=paths, preview_token=preview["preview_token"], baseline_confirmed=True)
        assert host._running == {}
        assert host.list_page()["total"] == 0
        await host.discard_preview(preview["preview_token"])
        assert host._previews == {}
    finally:
        await host.close()
        await tasks.close()


@pytest.mark.asyncio
async def test_discard_preview_does_not_create_graph_or_task(tmp_path, git_exe, committed_repo):
    tasks, host = await setup(tmp_path, git_exe)
    try:
        first = await host.preview(**source_fields(committed_repo))
        second = await host.preview(**source_fields(committed_repo))
        assert first["preview_token"] != second["preview_token"]
        await host.discard_preview(first["preview_token"])
        await host.discard_preview(first["preview_token"])
        assert host.list_page()["items"] == [] and not host._running
        assert len(host._previews) == 1
    finally:
        await host.close()
        await tasks.close()
    assert host._previews == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("ignored", [False, True])
async def test_selected_untracked_presets_are_previewed_from_read_only_overlay(tmp_path, git_exe, committed_repo, ignored):
    tasks, host = await setup(tmp_path, git_exe)
    name = "CMakeUserPresets.json" if ignored else "CMakePresets.json"
    if ignored:
        (committed_repo / ".gitignore").write_bytes(b"CMakeUserPresets.json\n*.secret\nbuild/\n")
        git(git_exe, committed_repo, "add", ".gitignore")
        git(git_exe, committed_repo, "commit", "-q", "-m", "ignore local presets and generated files")
        (committed_repo / "private.secret").write_bytes(b"do not list or copy")
    document = {"version": 6, "configurePresets": [{"name": "checked", "generator": "Visual Studio 17 2022",
        "binaryDir": "${sourceDir}/build", "cacheVariables": {"PREVIEW_MARKER": "selected-user-file"}}],
        "buildPresets": [{"name": "checked", "configurePreset": "checked", "configuration": "Debug"}],
        "testPresets": [{"name": "checked", "configurePreset": "checked", "configuration": "Debug"}]}
    raw = json.dumps(document).encode()
    (committed_repo / name).write_bytes(raw)
    original_index = (committed_repo / ".git/index").read_bytes()
    fields = source_fields(committed_repo, [name]) | {
        "configure_preset": "checked", "build_preset": "checked", "test_preset": "checked"}
    try:
        preview = await host.preview(**fields)
        assert preview["build"]["source"] == "preset"
        assert name in preview["changed_files"]
        assert "private.secret" not in preview["changed_files"]
        assert (committed_repo / name).read_bytes() == raw
        managed = host._previews[preview["preview_token"]]
        from pathlib import Path
        assert not (Path(managed.workspace.record(managed.task_id)["worktree_path"]) / name).exists()
        made = await create(host, committed_repo, **{key: value for key, value in fields.items() if key not in {"repository_path", "base_ref"}},
            preview_token=preview["preview_token"], baseline_confirmed=True)
        waiting = await settled(host, made["task_id"])
        assert waiting["build_selection"]["configure_preset"] == "checked"
        record = managed.workspace.record(made["task_id"])
        assert (Path(record["worktree_path"]) / name).read_bytes() == raw
        assert (committed_repo / name).read_bytes() == raw
        assert (committed_repo / ".git/index").read_bytes() == original_index
    finally:
        await host.close()
        await tasks.close()


@pytest.mark.asyncio
async def test_old_delivery_without_frozen_config_is_readable_but_cannot_be_restarted(tmp_path, git_exe, committed_repo):
    tasks, host = await setup(tmp_path, git_exe)
    factory = host.factory

    def legacy_factory(events):
        workflow = factory(events)
        original = workflow._stored_graph
        workflow._stored_graph = lambda task_id: GraphDefinition("task-analysis-delivery.v2", 2, original(task_id).nodes)
        return workflow

    host.factory = legacy_factory
    try:
        made = await create(host, committed_repo)
        task_id = made["task_id"]
        await settled(host, task_id)
        with tasks.store._transaction():
            row = tasks.store._db.execute("SELECT payload_json FROM graph_events WHERE task_id=? AND event_type='task.created'", (task_id,)).fetchone()
            metadata = json.loads(row[0])
            for field in ("source_baseline", "build_selection", "frozen_build_config"):
                metadata.pop(field)
            tasks.store._db.execute("UPDATE graph_events SET payload_json=? WHERE task_id=? AND event_type='task.created'", (json.dumps(metadata), task_id))
        definition = tasks.store.missions.get_graph_run(task_id)["definition_json"]
        host._services.pop(task_id)
        assert host.get(task_id)["status"] == "awaiting_human"
        assert host.graph(task_id)["graph_id"] == "task-analysis-delivery.v2"
        with pytest.raises(ForgePilotError, match="mission_frozen_config_missing"):
            host._workflow(task_id)
        assert tasks.store.missions.get_graph_run(task_id)["definition_json"] == definition
    finally:
        await host.close()
        await tasks.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [False, True])
async def test_real_analysis_uses_frozen_head_or_confirmed_working_bytes(tmp_path, git_exe, selected):
    case = await make_loop_case(tmp_path, git_exe)
    content = ORIGINAL + "\n// selected user source\n" if selected else ORIGINAL
    if selected:
        (case.root / "src/value.cpp").write_bytes(content.encode())
    before = git(git_exe, case.root, "status", "--porcelain=v1")

    class Provider(AnalysisProvider):
        async def chat(self, messages, tools=None):
            result = await super().chat(messages, tools)
            body = json.loads(result.message.content)
            if "items" in body:
                body["items"][0]["file_hash"] = sha256(content.encode()).hexdigest()
                return reply(json.dumps(body), usage=result.usage)
            return result

    base = case.gateway.router.configuration
    providers = {**case.providers, "analysis": Provider(case)}
    config = ModelConfiguration(profiles=(*base.profiles, ModelProfile(profile_id="analysis",
        base_url="http://127.0.0.1:1/v1", api_key_ref="env:FORGEPILOT_API_KEY", model="fake-analysis",
        family="analyst", max_output_tokens=2048)), routes=(*base.routes,
        ModelRoute(route_id="analysis", primary="analysis"), ModelRoute(route_id="orchestration", primary="analysis")))
    gateway = ModelGateway(config, secret_resolver=lambda _: "offline-only",
        provider_factory=lambda profile, _: providers[profile.profile_id])
    app = create_app(lambda trace: None, store=case.events,
        mission_factory=lambda events: DeliveryWorkflow(events, case.manager, gateway, validator=case.validator))
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
                paths = ["src/value.cpp"] if selected else []
                preview = await client.post("/api/v2/missions/preview", json=source_fields(case.root, paths))
                assert preview.status_code == 200, preview.text
                preview = preview.json()
                made = await client.post("/api/v2/missions", json={**source_fields(case.root, paths),
                    "request": "Fix negative input behavior", "preview_token": preview["preview_token"],
                    "baseline_confirmed": selected})
                assert made.status_code == 202, made.text
                path = "/api/v2/missions/" + made.json()["task_id"]
                waiting = await poll(client, path, {"awaiting_human", "failed", "retryable"})
                assert waiting["status"] == "awaiting_human", (await client.get(path + "/graph")).json()
                graph = (await client.get(path + "/graph")).json()
                assert graph["graph_id"] == "task-analysis-delivery.v3"
                assert next(node for node in graph["nodes"] if node["node_id"] == "baseline_capture")["status"] == "pending"
                artifacts = (await client.get(path + "/artifacts")).json()["items"]
                spec = next(item["content"] for item in artifacts if item["artifact_type"] == "TaskSpec")
                assert spec["repository"]["base_ref"] == waiting["source_baseline"]["task_commit"]
                evidence = [item["content"] for item in artifacts if item["artifact_type"] == "EvidenceBundle"]
                assert evidence and all(item["items"][0]["file_hash"] == sha256(content.encode()).hexdigest() for item in evidence)
                assert git(git_exe, case.root, "status", "--porcelain=v1") == before
                host = app.state.mission_host
                task_id = made.json()["task_id"]
                definition = case.events.missions.get_graph_run(task_id)["definition_json"]
                host._services.pop(task_id)
                restored = host._workflow(task_id)
                assert restored._stored_graph(task_id).definition_hash == graph["definition_hash"]
                assert restored.mission_source_baseline == waiting["source_baseline"]
                assert case.events.missions.get_graph_run(task_id)["definition_json"] == definition
                assert case.validator._monitors == {}
    finally:
        await gateway.aclose()
        await case.gateway.aclose()
