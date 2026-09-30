from dataclasses import replace
import json

import pytest

from forgepilot.build_process import build_environment
from forgepilot.errors import ForgePilotError
from forgepilot.presets import Presets, available_presets
from test_build import build_config, build_repo, presets


def available(data):
    return {kind: {p["name"] for p in data[kind + "Presets"]} for kind in ("configure", "build", "test")}


def test_configuration_mismatch_never_selects_arbitrary_trio(build_repo, build_config):
    config, data = presets(build_repo, build_config)
    data["testPresets"][0]["configuration"] = "Debug"
    (build_repo / "CMakePresets.json").write_text(json.dumps(data))
    loaded = Presets(build_repo, build_environment())
    assert loaded.choose(available(data), config)["source"] == "default"
    with pytest.raises(ForgePilotError, match="preset_no_match"):
        loaded.choose(available(data), replace(config, configure_preset="selected"))


def test_hidden_unavailable_presets_and_single_default(build_repo, build_config):
    config, data = presets(build_repo, build_config)
    loaded = Presets(build_repo, build_environment())
    assert loaded.choose({"configure": set(), "build": set(), "test": set()}, config)["configuration"] == "Debug"
    with pytest.raises(ForgePilotError, match="preset_no_match"):
        loaded.choose(available(data), replace(config, configure_preset="base"))
    parsed = available_presets('Available configure presets:\n\n  "selected" - label\n\nAvailable build presets:\n  "selected-build"\nAvailable test presets:\n  "selected-test"\n')
    assert parsed == available(data)


@pytest.mark.parametrize("problem", ["duplicate", "include_escape", "include_cycle", "inherit_cycle", "macro_cycle"])
def test_invalid_preset_graphs_are_explicit(build_repo, build_config, problem):
    config, data = presets(build_repo, build_config)
    path = build_repo / "CMakePresets.json"
    if problem == "duplicate":
        path.write_text('{"version":6,"version":6}')
    else:
        if problem == "include_escape":
            data["include"] = ["../elsewhere.json"]
        if problem == "include_cycle":
            data["include"] = ["CMakePresets.json"]
        if problem == "inherit_cycle":
            data["configurePresets"][0]["inherits"] = "selected"
        if problem == "macro_cycle":
            data["configurePresets"][0]["environment"] = {"LOOP": "$env{LOOP}"}
            data["configurePresets"][0]["binaryDir"] = "$env{LOOP}"
        path.write_text(json.dumps(data))
    with pytest.raises(ForgePilotError, match="preset_"):
        Presets(build_repo, build_environment()).choose(available(data), config)


def test_earlier_parent_precedence_and_null_environment(build_repo, build_config):
    config, data = presets(build_repo, build_config)
    data["configurePresets"] += [
        {"name": "a", "hidden": True, "cacheVariables": {"FLAG": "first"}, "environment": {"DIR": "a"}},
        {"name": "b", "hidden": True, "cacheVariables": {"FLAG": "second"}, "environment": {"DIR": "b"}}]
    data["configurePresets"][0].update(inherits=["a", "b", "base"], environment={"DIR": None},
                                     binaryDir="${sourceDir}/out/$env{DIR}${presetName}")
    (build_repo / "CMakePresets.json").write_text(json.dumps(data))
    loaded = Presets(build_repo, build_environment())
    effective = loaded.effective("configure", "selected")
    assert effective["cacheVariables"]["FLAG"] == "first" and effective["environment"]["DIR"] is None
    selected = loaded.choose({"configure": {"selected"}, "build": {"selected-build"}, "test": {"selected-test"}}, config)
    assert selected["binary_dir"] == build_repo.as_posix() + "/out/selected"


def test_windows_parent_and_preset_environment_have_different_case_rules(build_repo, build_config):
    config, data = presets(build_repo, build_config)
    data["configurePresets"][0].update(binaryDir="${sourceDir}/$env{build_dir}", environment={"BUILD_DIR": "out/case build"})
    path = build_repo / "CMakePresets.json"
    path.write_text(json.dumps(data))
    loaded = Presets(build_repo, {"SYSTEMROOT": "C:/Windows"})
    # CMake's preset map is case-sensitive; an unknown lowercase name falls back
    # to the parent environment, then to empty. This would be an unsafe in-source build.
    assert loaded.choose(available(data), config)["binary_dir"] == build_repo.as_posix() + "/"
    assert loaded._expand("$penv{SystemRoot}", {}) == "C:/Windows"
    assert loaded._expand("$env{SystemRoot}", {"environment": {"SystemRoot": None}}) == "C:/Windows"
    data["configurePresets"][0]["environment"]["build_dir"] = "another directory"
    path.write_text(json.dumps(data))
    assert Presets(build_repo, {}).choose(available(data), config)["binary_dir"] == build_repo.as_posix() + "/another directory"


@pytest.mark.asyncio
async def test_native_condition_filters_candidates(build_repo, build_config, rg_exe, git_exe):
    from test_build import service_for_build
    from forgepilot.context import TaskState
    config, data = presets(build_repo, build_config)
    data["buildPresets"].append({"name": "inactive", "configurePreset": "selected", "condition": False})
    (build_repo / "CMakePresets.json").write_text(json.dumps(data))
    service, _ = service_for_build(build_repo, config, rg_exe, git_exe)
    try:
        result = await service.start(str(build_repo), "inspect")
        assert result.state == TaskState.WAITING_APPROVAL, result.error
        assert result.build_result["selection"]["build_preset"] == "selected-build"
    finally:
        service.close()
