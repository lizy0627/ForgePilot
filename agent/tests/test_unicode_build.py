"""Build/cache/preset paths must not use Python's Unicode-expanding Windows equality."""

import json
from pathlib import Path
import subprocess

import pytest

from forgepilot.build import BuildPipeline
from forgepilot.errors import ForgePilotError
from forgepilot.presets import Presets, safe_build_path
from forgepilot.security import Redactor
from forgepilot.toolchain import CustomCommand
from forgepilot.trace import MemoryTrace
from test_build import build_config


_PAIRS = [("İ", "i\u0307"), ("ß", "ss")]


@pytest.mark.parametrize("first,second", _PAIRS)
def test_build_destination_cannot_escape_to_distinct_unicode_twin(tmp_path, first, second):
    root, twin = tmp_path / first, tmp_path / second
    root.mkdir()
    (twin / "out").mkdir(parents=True)
    assert not root.samefile(twin)
    with pytest.raises(ForgePilotError, match="build_path"):
        safe_build_path(root, str(twin / "out"))


@pytest.mark.parametrize("first,second", _PAIRS)
@pytest.mark.parametrize("field", ["CMAKE_HOME_DIRECTORY", "CMAKE_CACHEFILE_DIR"])
def test_cache_cannot_claim_distinct_unicode_source_or_binary_dir(tmp_path, build_config, first, second, field):
    root, twin = tmp_path / first, tmp_path / second
    (root / "out").mkdir(parents=True)
    (twin / "out").mkdir(parents=True)
    values = {"CMAKE_HOME_DIRECTORY": root, "CMAKE_CACHEFILE_DIR": root / "out"}
    values[field] = twin if field == "CMAKE_HOME_DIRECTORY" else twin / "out"
    (root / "out/CMakeCache.txt").write_text("\n".join(f"{name}:INTERNAL={path.as_posix()}" for name, path in values.items()), encoding="utf-8")
    pipeline = BuildPipeline(root, build_config, lambda _: None, MemoryTrace(), "unicode", Redactor())
    pipeline.binary_dir = root / "out"
    with pytest.raises(ForgePilotError, match="build_cache"):
        pipeline._cache()


@pytest.mark.parametrize("first,second", _PAIRS)
def test_preset_include_cannot_read_distinct_unicode_twin_root(tmp_path, first, second):
    root, twin = tmp_path / first, tmp_path / second
    root.mkdir()
    twin.mkdir()
    (root / "outside.json").write_text(json.dumps({"version": 6}))
    (twin / "outside.json").write_text(json.dumps({"version": 6, "configurePresets": [{"name": "outside"}]}))
    (root / "CMakePresets.json").write_text(json.dumps({"version": 6, "include": [str(twin / "outside.json")]}))
    with pytest.raises(ForgePilotError, match="preset_invalid"):
        Presets(root, {})


@pytest.mark.parametrize("first,second", _PAIRS)
def test_preset_inventory_retains_both_distinct_unicode_includes(tmp_path, first, second):
    names = [first + ".json", second + ".json"]
    for name, preset in zip(names, ["one", "two"]):
        (tmp_path / name).write_text(json.dumps({"version": 6, "configurePresets": [{"name": preset}]}))
    (tmp_path / "CMakePresets.json").write_text(json.dumps({"version": 6, "include": names}))
    loaded = Presets(tmp_path, {})
    assert set(loaded.raw["configure"]) == {"one", "two"} and len(loaded.files) == 3
    loaded.unchanged()
    (tmp_path / names[1]).write_text(json.dumps({"version": 6, "configurePresets": [{"name": "changed"}]}))
    with pytest.raises(ForgePilotError, match="build_selection_changed"):
        loaded.unchanged()


@pytest.mark.parametrize("first,second", _PAIRS)
@pytest.mark.asyncio
async def test_ancestor_rebound_to_unicode_twin_junction_cannot_preserve_build_authority(tmp_path, build_config, first, second):
    original, moved = tmp_path / first, tmp_path / second
    root = original / "project"
    (root / "out").mkdir(parents=True)
    pipeline = BuildPipeline(root, build_config, lambda _: None, MemoryTrace(), "unicode", Redactor())
    pipeline.binary_dir = root / "out"
    identity = root.stat().st_dev, root.stat().st_ino
    original.rename(moved)
    subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(original), str(moved)],
                   check=True, capture_output=True)
    try:
        # The actual root inode survives; the ancestor namespace is now different.
        assert (root.stat().st_dev, root.stat().st_ino) == identity
        with pytest.raises(ForgePilotError, match="build_path"):
            pipeline._paths()
        with pytest.raises(ForgePilotError, match="build_path"):
            await pipeline._command("preset_discovery", [build_config.cmake, "--list-presets=all"], 30)
        with pytest.raises(ForgePilotError, match="command_denied"):
            pipeline._profile_command("cmake_build")
        with pytest.raises(ForgePilotError, match="build_path"):
            safe_build_path(root, str(root / "out"))
        with pytest.raises(ForgePilotError, match="build_selection_changed"):
            pipeline.presets.unchanged()
        with pytest.raises(ForgePilotError, match="command_denied"):
            CustomCommand(root, build_config.cmake, ("--build", str(root / "out"))).review()
    finally:
        original.rmdir()  # Unlink only this fixture junction, never its target.
        moved.rename(original)
