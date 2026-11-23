"""Keep relocated protocol data available in both supported Windows bundles."""

import json
from pathlib import Path
import shutil
import subprocess
import sys


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


def test_protocol_examples_use_colocated_data_after_package_relocation(tmp_path):
    package = tmp_path / "relocated" / "forgepilot"
    shutil.copytree(ROOT / "agent/forgepilot", package, ignore=shutil.ignore_patterns("__pycache__"))
    resource = package / EXAMPLE_RESOURCE
    legacy = json.loads(resource.read_text(encoding="utf-8"))
    legacy["relocation_probe"] = "from the relocated resource"
    resource.write_text(json.dumps(legacy), encoding="utf-8")
    unrelated = tmp_path / "unrelated-working-directory"
    unrelated.mkdir()
    probe = (
        "import json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from forgepilot.protocol_examples import examples\n"
        "print(json.dumps(examples()))"
    )
    output = subprocess.check_output(
        [sys.executable, "-I", "-c", probe, str(package.parent)], cwd=unrelated, timeout=30,
    )
    examples = json.loads(output)
    assert all(examples[key] == value for key, value in legacy.items())
    assert examples["mission_create_request"]["task_type"] == "auto"
    assert examples["mission_revision_conflict_error"]["error"]["code"] == "mission_revision_conflict"
