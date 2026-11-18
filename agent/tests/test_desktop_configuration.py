"""GUI configuration uses launcher contracts, atomic secrets and offline checks."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import httpx
import pytest

from forgepilot import desktop_configuration as core
from forgepilot.server import parser as server_parser
from forgepilot.settings import model_gateway_from_args
from forgepilot.windows_secrets import protect_launcher_key, unprotect_launcher_key


KEY = "offline-desktop-secret-123456"
EMBEDDING_KEY = "offline-embedding-secret-654321"


def fields(**changes):
    return {**core.RuntimeFields().model_dump(), "chat_model": "chat-model", "task_model": "task-model",
            "review_model": "review-model", "summary_model": "summary-model", "task_model_family": "writer",
            "review_model_family": "reviewer", **changes}


async def save(config, *, values=None, key=KEY, **changes):
    loaded = await core.handle_request(config, {"action": "load"})
    return await core.handle_request(config, {"action": "save", "fields": values or fields(),
        "api_key": key, "config_revision": loaded["config_revision"], **changes})


@pytest.mark.asyncio
async def test_first_load_is_read_only_and_invalid_configuration_can_be_replaced(tmp_path):
    config = tmp_path / "missing" / "config.json"
    state = await core.handle_request(config, {"action": "load"})
    assert state["ok"] and not state["configured"] and not state["has_api_key"]
    assert state["fields"]["port"] == 8765 and not config.parent.exists()
    assert not state["fields"]["cloud_model_allowed"]
    config.parent.mkdir()
    config.write_bytes(b"malformed existing local config")
    state = await core.handle_request(config, {"action": "load"})
    assert state["ok"] and not state["configured"] and state["errors"]
    made = await save(config)
    assert made["ok"] and made["configured"] and made["has_api_key"]
    assert core.load_saved_key(config) == KEY


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_cloud", [False, True])
async def test_gui_saved_models_generate_every_mission_role_and_separate_reviewer(tmp_path, monkeypatch, allow_cloud):
    config = tmp_path / "config.json"
    made = await save(config, values=fields(cloud_model_allowed=allow_cloud))
    assert made["ok"] and made["configured"] and made["restart_required"] and made["saved_at"]
    stored = core.read_config(config)
    args = stored["server_args"]
    assert args[args.index("--database") + 1] == str(tmp_path / "history.sqlite3")
    assert ("--mission-allow-cloud-model" in args) == allow_cloud
    assert KEY not in config.read_text(encoding="utf-8") and "api_key" not in stored
    assert stored["credential_file"].startswith("api-key-") and stored["credential_file"].endswith(".dpapi")
    assert KEY not in (config.parent / stored["credential_file"]).read_text(encoding="ascii")
    monkeypatch.setenv("FORGEPILOT_API_KEY", core.load_saved_key(config))
    models_file = stored["server_args"][stored["server_args"].index("--model-config") + 1]
    assert KEY not in Path(models_file).read_text(encoding="utf-8")
    gateway = model_gateway_from_args(SimpleNamespace(**{**stored, "timeout": 120, "model_config": models_file}))
    try:
        routes = gateway.router.configuration.router()
        for role in ("task", "orchestration", "analysis", "implementation", "repair"):
            assert routes.resolve(role).primary.model == "task-model"
        assert routes.resolve("review").primary.family == "reviewer"
        assert routes.resolve("implementation").primary.family == "writer"
        assert routes.resolve("chat").primary.model == "chat-model"
        assert routes.resolve("summary").primary.model == "summary-model"
        assert not gateway._providers
    finally:
        await gateway.aclose()
    assert KEY not in json.dumps(made)


@pytest.mark.asyncio
@pytest.mark.parametrize("joined", [False, True], ids=["separate-argument", "equals-argument"])
async def test_save_preserves_trusted_other_args_and_freezes_running_configuration(tmp_path, git_exe, monkeypatch, joined):
    config = tmp_path / "config.json"
    models = tmp_path / "custom models.json"
    custom = core.model_configuration(fields(), []).model_dump(mode="json")
    primary = {"profile_id": "custom-review", "family": "custom", "model": "independent-review",
               "base_url": "http://127.0.0.1:32101/v1", "api_key_ref": "env:REVIEW_KEY"}
    fallback = {**primary, "profile_id": "backup-review", "model": "backup",
                "base_url": "http://127.0.0.1:32102/v1", "api_key_ref": "env:BACKUP_KEY"}
    custom["profiles"].extend([primary, fallback])
    custom["routes"].append({"route_id": "review", "role": "reviewer",
                             "primary": "custom-review", "fallback": "backup-review"})
    original = json.dumps(custom, indent=4).encode("utf-8")
    models.write_bytes(original)
    model_args = ["--model-config=" + str(models)] if joined else ["--model-config", str(models)]
    trusted = ["--database", str(tmp_path / "history.sqlite3"), "--timeout", "200", "--configure-preset", "existing"]
    config.write_text(json.dumps({"base_url": "http://127.0.0.1:32103/v1", "model": "legacy", "port": 8765,
        "server_args": [*model_args, *trusted]}), encoding="utf-8")
    config.with_name("api-key.dpapi").write_text(protect_launcher_key(KEY), encoding="ascii")
    status = lambda: {"managed": True, "active_count": 2, "restart_allowed": False}
    new_key = "offline-rotated-desktop-key"
    monkeypatch.setenv("REVIEW_KEY", "offline-review-key")
    monkeypatch.setenv("BACKUP_KEY", "offline-backup-key")
    for changes, key in [({"port": 9876}, ""), ({"cloud_model_allowed": True}, ""),
                         ({"git": str(git_exe)}, ""), ({}, new_key)]:
        loaded = await core.handle_request(config, {"action": "load"}, status=status)
        assert loaded["configured"] and loaded["fields"]["external_model_config"] == str(models)
        result = await core.handle_request(config, {"action": "save", "fields": {**loaded["fields"], **changes},
            "api_key": key, "config_revision": loaded["config_revision"]}, status=status)
        assert result["ok"] and result["active_count"] == 2 and not result["restart_allowed"]
        assert result["restart_required"] and "原配置" in result["notice"]
        assert models.read_bytes() == original
        stored = core.read_config(config)
        args = server_parser().parse_args(stored["server_args"])
        assert str(args.model_config) == str(models)
        monkeypatch.setenv("FORGEPILOT_API_KEY", core.load_saved_key(config))
        async with model_gateway_from_args(args) as gateway:
            review = gateway.router.resolve("review", "reviewer")
            for selected, expected in ((review.primary, primary), (review.fallback, fallback)):
                assert (selected.model, selected.base_url, selected.api_key_ref) == (
                    expected["model"], expected["base_url"], expected["api_key_ref"])
    assert all(arg in stored["server_args"] for arg in trusted)
    assert stored["port"] == 9876 and args.mission_allow_cloud_model and Path(args.git) == Path(git_exe)
    assert core.load_saved_key(config) == new_key


@pytest.mark.asyncio
async def test_failed_atomic_replace_keeps_old_config_and_credentials_byte_identical(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    assert (await save(config))["ok"]
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    def fail(*_):
        raise PermissionError("do not echo " + KEY)
    monkeypatch.setattr(core, "_replace_config", fail)
    response = await save(config, key="different-new-secret")
    assert not response["ok"] and KEY not in json.dumps(response)
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before
    assert core.load_saved_key(config) == KEY


@pytest.mark.asyncio
async def test_stale_form_cannot_overwrite_new_config_and_blank_key_preserves_original(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    first = await save(config)
    original = core.read_config(config)
    original_key_name = original["credential_file"]
    models = Path(server_parser().parse_args(original["server_args"]).model_config)
    contents = models.read_bytes()
    assert first["fields"]["external_model_config"] == ""
    changed = await core.handle_request(config, {"action": "save", "fields": {**first["fields"], "port": 9876},
        "config_revision": first["config_revision"]})
    assert changed["ok"] and changed["fields"]["external_model_config"] == ""
    assert Path(server_parser().parse_args(core.read_config(config)["server_args"]).model_config) == models
    assert models.read_bytes() == contents
    second = await core.handle_request(config, {"action": "save", "fields": {**changed["fields"], "chat_model": "changed-chat"},
        "api_key": "", "config_revision": changed["config_revision"]})
    assert second["ok"] and core.read_config(config)["credential_file"] == original_key_name
    monkeypatch.setenv("FORGEPILOT_API_KEY", core.load_saved_key(config))
    async with model_gateway_from_args(server_parser().parse_args(core.read_config(config)["server_args"])) as gateway:
        assert gateway.router.resolve("chat").primary.model == "changed-chat"
    assert models.read_bytes() == contents
    before = config.read_bytes()
    stale = await core.handle_request(config, {"action": "save", "fields": fields(),
        "api_key": "another-new-key", "config_revision": first["config_revision"]})
    assert not stale["ok"] and stale["errors"][0]["code"] == "config_conflict"
    assert config.read_bytes() == before and core.load_saved_key(config) == KEY


@pytest.mark.asyncio
@pytest.mark.parametrize("change,purpose", [({"review_model": "task-model"}, "review"),
    ({"review_model": ""}, "review"), ({"task_model_family": ""}, "review"),
    ({"review_model_family": "writer"}, "review"), ({"task_model": ""}, "task"),
    ({"embedding_enabled": True}, "embedding"), ({"port": True}, "configuration")])
async def test_invalid_draft_reports_purpose_without_changing_old_configuration(tmp_path, change, purpose):
    config = tmp_path / "config.json"
    assert (await save(config))["ok"]
    before = config.read_bytes()
    result = await save(config, values=fields(**change), key="")
    assert not result["ok"] and result["errors"][0]["purpose"] == purpose
    assert config.read_bytes() == before


@pytest.mark.asyncio
async def test_check_draft_uses_mock_transport_and_reports_exact_purpose_without_saving(tmp_path):
    config = tmp_path / "config.json"
    state = await save(config)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    requests = []
    def transport(request):
        model = json.loads(request.content)["model"]
        requests.append(model)
        assert request.headers["Authorization"] == "Bearer " + KEY
        if model == "draft-task":
            return httpx.Response(403, text="private server body " + KEY)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]})
    result = await core.handle_request(config, {"action": "check", "fields": fields(task_model="draft-task"),
        "api_key": "", "config_revision": state["config_revision"]}, transport=httpx.MockTransport(transport))
    assert not result["ok"] and [item["purpose"] for item in result["errors"]] == ["task"]
    assert requests == ["chat-model", "draft-task", "summary-model", "review-model"]
    assert "HTTP 403" in result["errors"][0]["message"] and KEY not in json.dumps(result)
    assert "private server body" not in json.dumps(result)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


@pytest.mark.asyncio
async def test_optional_embedding_key_is_separate_dpapi_and_uses_shared_check_transport(tmp_path):
    config = tmp_path / "config.json"
    selected = fields(embedding_enabled=True, embedding_model="fixture-embedding", embedding_base_url="https://embedding.invalid/v1")
    state = await save(config, values=selected, embedding_api_key=EMBEDDING_KEY)
    assert state["ok"] and state["has_embedding_key"] and core.load_embedding_key(config) == EMBEDDING_KEY
    assert EMBEDDING_KEY not in config.read_text(encoding="utf-8")
    calls = []
    def transport(request):
        calls.append(request)
        if request.url.path.endswith("/embeddings"):
            assert request.headers["Authorization"] == "Bearer " + EMBEDDING_KEY
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]})
    checked = await core.handle_request(config, {"action": "check", "fields": selected,
        "config_revision": state["config_revision"]}, transport=httpx.MockTransport(transport))
    assert checked["ok"] and len(calls) == 5 and {c["purpose"] for c in checked["checks"]} == {"chat", "task", "review", "summary", "embedding"}
    assert EMBEDDING_KEY not in json.dumps(state) + json.dumps(checked)


@pytest.mark.asyncio
async def test_legacy_key_is_loaded_without_rewriting_file_and_new_pointer_is_atomic(tmp_path):
    config = tmp_path / "config.json"
    legacy = tmp_path / "api-key.dpapi"
    legacy.write_text(protect_launcher_key(KEY), encoding="ascii")
    config.write_text(json.dumps({"base_url": "https://api.openai.com/v1", "model": "legacy", "port": 8765,
                                  "server_args": []}), encoding="utf-8")
    old = legacy.read_bytes()
    state = await core.handle_request(config, {"action": "load"})
    assert state["has_api_key"] and state["configured"]
    assert [state["fields"][name] for name in ("chat_model", "task_model", "summary_model")] == ["legacy"] * 3
    requests = []
    def respond(request):
        requests.append(json.loads(request.content)["model"])
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "OK"},
                                                      "finish_reason": "stop"}]})
    checked = await core.handle_request(config, {"action": "check", "fields": state["fields"],
        "config_revision": state["config_revision"]}, transport=httpx.MockTransport(respond))
    assert checked["ok"] and {check["purpose"] for check in checked["checks"]} == {"chat", "task", "summary"}
    assert requests == ["legacy"]
    result = await save(config, values=state["fields"], key="")
    assert result["ok"] and result["configured"] and legacy.read_bytes() == old
    assert "--model-config" not in core.read_config(config)["server_args"]
    assert core.load_saved_key(config) == KEY


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI compatibility")
def test_launcher_dpapi_hex_is_bidirectionally_powershell_secure_string_compatible():
    common = "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
    environment = {key: value for key, value in os.environ.items() if key.casefold() != "psmodulepath"}
    encrypted = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", common +
        "$value=[Console]::In.ReadToEnd(); ConvertTo-SecureString -String $value -AsPlainText -Force | ConvertFrom-SecureString"],
        input=KEY.encode(), capture_output=True, check=True, env=environment).stdout.decode("utf-8").strip()
    assert unprotect_launcher_key(encrypted) == KEY
    ciphertext = protect_launcher_key(KEY)
    plaintext = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", common +
        "$value=[Console]::In.ReadToEnd() | ConvertTo-SecureString; [Net.NetworkCredential]::new('', $value).Password"],
        input=ciphertext.encode(), capture_output=True, check=True, env=environment).stdout.decode("utf-8").strip()
    assert plaintext == KEY


@pytest.mark.asyncio
async def test_restart_uses_trusted_callback_and_rejects_stale_or_unconfigured_draft(tmp_path):
    config = tmp_path / "config.json"
    calls = []
    def restart():
        calls.append(True)
        return {"ok": True, "notice": "owner accepted", "restart_required": True}
    denied = await core.handle_request(config, {"action": "restart"}, restart=restart)
    assert not denied["ok"] and calls == []
    state = await save(config)
    accepted = await core.handle_request(config, {"action": "restart", "config_revision": state["config_revision"]}, restart=restart)
    assert accepted["ok"] and calls == [True]


@pytest.mark.asyncio
async def test_pending_saved_configuration_survives_reload_until_owner_uses_same_revision(tmp_path):
    config = tmp_path / "config.json"
    initial = await save(config)
    revision = core.configuration_revision(config)
    assert initial["restart_required"]
    for status in ({"managed": False}, {"managed": True, "running_revision": "old-revision"}, {"managed": True}):
        loaded = await core.load_configuration(config, status=lambda: status)
        assert loaded["configured"] and loaded["restart_required"]
    loaded = await core.load_configuration(config, status=lambda: {"managed": True, "running_revision": revision})
    assert loaded["configured"] and not loaded["restart_required"]


@pytest.mark.asyncio
async def test_concurrent_writer_lock_refuses_save_before_touching_valid_configuration(tmp_path):
    config = tmp_path / "config.json"
    state = await save(config)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    with core.ConfigurationLock(config):
        response = await core.handle_request(config, {"action": "save", "fields": fields(),
            "api_key": "new-credential", "config_revision": state["config_revision"]})
    assert not response["ok"] and response["errors"][0]["code"] == "config_busy"
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


@pytest.mark.asyncio
async def test_invalid_saved_credential_reference_is_repairable_without_accessing_external_file(tmp_path):
    config = tmp_path / "config.json"
    outside = tmp_path.parent / ("unrelated-" + tmp_path.name + ".txt")
    outside.write_text("unrelated-user-file", encoding="utf-8")
    config.write_text(json.dumps({**fields(), "server_args": [], "credential_file": "../" + outside.name}), encoding="utf-8")
    state = await core.handle_request(config, {"action": "load"})
    assert state["ok"] and not state["configured"] and state["errors"]
    made = await save(config)
    assert made["ok"] and made["configured"] and core.load_saved_key(config) == KEY
    assert outside.read_text(encoding="utf-8") == "unrelated-user-file"


@pytest.mark.asyncio
async def test_removed_optional_tools_do_not_invalidate_models_but_bad_save_preserves_config(tmp_path):
    config = tmp_path / "config.json"
    tool = tmp_path / "configured-cmake.exe"
    tool.write_bytes(b"local fixture executable identity, never executed")
    initial = await save(config, values=fields(cmake=str(tool)))
    assert initial["ok"] and initial["configured"]
    tool.unlink()
    loaded = await core.handle_request(config, {"action": "load"})
    assert loaded["configured"] and loaded["has_api_key"] and loaded["fields"]["cmake"] == str(tool)
    assert any("cmake" in warning and "普通聊天" in warning for warning in loaded["warnings"])
    before = config.read_bytes()
    failed = await save(config, values=fields(cmake=str(tool)), key="")
    assert not failed["ok"] and failed["errors"][0]["purpose"] == "cmake"
    assert config.read_bytes() == before


def test_configuration_entry_resolves_source_and_frozen_distribution_without_python_environment(tmp_path, monkeypatch):
    monkeypatch.delattr(core.sys, "frozen", raising=False)
    assert core.application_root() == Path(core.__file__).resolve().parents[2]
    release = tmp_path / "unpacked release with spaces"
    monkeypatch.setattr(core.sys, "frozen", True, raising=False)
    monkeypatch.setattr(core.sys, "executable", str(release / "bin" / "ForgePilotAgent.exe"))
    assert core.application_root() == release
    assert core.application_root() / "client/build/Release/ForgePilotClient.exe" == release / "client/build/Release/ForgePilotClient.exe"
