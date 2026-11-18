"""Evaluator-only Oracle feedback and CMake metadata checks."""
from importlib.util import module_from_spec, spec_from_file_location
import json
import subprocess
import sys

import pytest

from forgepilot_eval.manifest import DATA_ROOT


ORACLES = DATA_ROOT / "real_tasks" / "protected_oracles"


def _load(name, monkeypatch):
    monkeypatch.syspath_prepend(str(ORACLES))
    spec = spec_from_file_location("real_oracle_" + name, ORACLES / (name + ".py"))
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_oracle_invalid_candidate_has_finite_redacted_feedback(tmp_path):
    oracle = ORACLES / "bug_unmatched_short.py"
    process = subprocess.run([sys.executable, str(oracle), "--repository", str(tmp_path)],
                             capture_output=True, text=True, timeout=10, check=False)
    assert process.returncode == 1
    assert process.stdout.strip() == "oracle_failed:execution_error"
    assert process.stderr == ""


def test_oracle_command_timeout_is_finite_and_process_is_stopped(tmp_path, monkeypatch, capsys):
    common = _load("common", monkeypatch)
    with pytest.raises(common.OracleTimeout):
        common.command([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, timeout=0.05)
    assert common.guarded(lambda: (_ for _ in ()).throw(common.OracleTimeout())) == 1
    assert capsys.readouterr().out == "oracle_failed:timeout\n"


def test_string_view_cmake_default_uses_codemodel_not_trace_text(tmp_path, monkeypatch):
    feature = _load("feature_string_view", monkeypatch)
    reply = tmp_path / "reply"
    reply.mkdir()
    (reply / "index-1.json").write_text(json.dumps({"reply": {"codemodel-v2": {
        "jsonFile": "model.json"}}}), encoding="utf-8")
    (reply / "model.json").write_text(json.dumps({"configurations": [{"targets": [
        {"name": "pugixml-static", "jsonFile": "target.json"}]}]}), encoding="utf-8")
    target = reply / "target.json"
    target.write_text(json.dumps({"compileGroups": [{"language": "CXX",
        "languageStandard": {"standard": "17"}}]}), encoding="utf-8")
    assert feature._default_cxx17(reply)
    target.write_text(json.dumps({"compileGroups": [{"language": "CXX",
        "languageStandard": {"standard": "11"}}]}), encoding="utf-8")
    assert not feature._default_cxx17(reply)
