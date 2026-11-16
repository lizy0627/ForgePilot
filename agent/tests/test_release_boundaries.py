"""Keep relocated protocol data available in both supported Windows bundles."""

import importlib.util
import json
from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_RESOURCE = "protocol_examples.v1.json"


def test_release_collects_python_modules_and_protocol_example_resource():
    script = (ROOT / "scripts/Package-Windows.ps1").read_text(encoding="utf-8-sig")
    source_bundle = "function Copy-PythonTree(" in script
    frozen_bundle = "'python', '-m', 'PyInstaller'" in script
    assert source_bundle != frozen_bundle, "Unknown or ambiguous Windows release collection mode"
    if source_bundle:
        assert "Get-ChildItem -LiteralPath $Source -Recurse -File -Filter '*.py'" in script
        assert "Copy-PythonTree (Join-Path $root 'agent\\forgepilot') 'agent\\forgepilot'" in script
        assert (
            "Copy-File (Join-Path $root 'agent\\forgepilot\\protocol_examples.v1.json') "
            "'agent\\forgepilot\\protocol_examples.v1.json'"
        ) in script
    else:
        assert "'--collect-submodules', 'forgepilot'" in script
        assert (
            "'--add-data', ((Join-Path $root 'agent\\forgepilot\\protocol_examples.v1.json') "
            "+ ':forgepilot')"
        ) in script
        assert "(Join-Path $PSScriptRoot 'desktop_launcher.py')" in script


def test_protocol_examples_use_colocated_data_after_package_relocation(tmp_path, monkeypatch):
    package = tmp_path / "relocated" / "forgepilot"
    package.mkdir(parents=True)
    source = ROOT / "agent/forgepilot"
    for name in ("protocol_examples.py", EXAMPLE_RESOURCE):
        shutil.copyfile(source / name, package / name)
    unrelated = tmp_path / "unrelated-working-directory"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    specification = importlib.util.spec_from_file_location(
        "relocated_protocol_examples", package / "protocol_examples.py"
    )
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    examples = module.examples()
    legacy = json.loads((package / EXAMPLE_RESOURCE).read_text(encoding="utf-8"))
    assert all(examples[key] == value for key, value in legacy.items())
    assert examples["mission_create_request"]["task_type"] == "auto"
    assert examples["mission_revision_conflict_error"]["error"]["code"] == "mission_revision_conflict"
