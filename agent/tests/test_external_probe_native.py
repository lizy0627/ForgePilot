"""Native smoke for a separate, evaluator-checked behavioral probe."""

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "eval"))

from forgepilot_eval.external_probe import make_challenge, verify_response, write_probe_project


ROOT = Path(__file__).resolve().parents[2]


def _command(argv, cwd):
    result = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=180, check=False)
    assert result.returncode == 0, (result.stdout + result.stderr)[-3000:].decode("utf-8", errors="replace")


@pytest.mark.skipif(os.name != "nt" or shutil.which("cmake") is None,
                    reason="Native MSVC/CMake probe requires Windows")
def test_micro_native_external_probe_valid_response(tmp_path):
    cmake = shutil.which("cmake")
    candidate = tmp_path / "candidate"
    shutil.copytree(ROOT / "eval" / "fixtures" / "cpp-compile-01", candidate)
    (candidate / "src" / "subject.cpp").write_text(
        '#include "subject.h"\nint add(int a, int b) { return a + b; }\n', encoding="utf-8")
    build = tmp_path / "candidate-build"
    _command([cmake, "-S", str(candidate), "-B", str(build),
              "-G", "Visual Studio 17 2022", "-A", "x64"], tmp_path)
    _command([cmake, "--build", str(build), "--config", "Debug", "--target", "subject"], tmp_path)
    library = build / "Debug" / "subject.lib"
    assert library.is_file()

    probe_source = write_probe_project("cpp-compile-01", tmp_path / "probe-source",
                                        candidate_include=candidate / "src", candidate_library=library)
    probe_build = tmp_path / "probe-build"
    _command([cmake, "-S", str(probe_source), "-B", str(probe_build),
              "-G", "Visual Studio 17 2022", "-A", "x64"], tmp_path)
    _command([cmake, "--build", str(probe_build), "--config", "Debug", "--target", "probe"], tmp_path)
    executable = probe_build / "Debug" / "probe.exe"
    challenge = make_challenge("cpp-compile-01", seed=99, nonce="c" * 32)
    valid = subprocess.run([str(executable)], input=challenge.stdin,
                           capture_output=True, timeout=10, check=False)
    assert verify_response(challenge, returncode=valid.returncode, stdout=valid.stdout)


@pytest.mark.skipif(os.name != "nt" or shutil.which("cmake") is None,
                    reason="Native MSVC/CMake probe requires Windows")
def test_candidate_static_initializer_exit_zero_cannot_pass_external_probe(tmp_path):
    cmake = shutil.which("cmake")
    candidate = tmp_path / "candidate"
    shutil.copytree(ROOT / "eval" / "fixtures" / "cpp-compile-01", candidate)
    (candidate / "src" / "subject.cpp").write_text(
        '#include "subject.h"\n#include <cstdlib>\n'
        'struct Escape { Escape() { std::exit(0); } } escape;\n'
        'int add(int a, int b) { return a + b; }\n', encoding="utf-8")
    build = tmp_path / "candidate-build"
    _command([cmake, "-S", str(candidate), "-B", str(build),
              "-G", "Visual Studio 17 2022", "-A", "x64"], tmp_path)
    _command([cmake, "--build", str(build), "--config", "Debug", "--target", "subject"], tmp_path)
    probe_source = write_probe_project("cpp-compile-01", tmp_path / "probe-source",
                                        candidate_include=candidate / "src",
                                        candidate_library=build / "Debug" / "subject.lib")
    probe_build = tmp_path / "probe-build"
    _command([cmake, "-S", str(probe_source), "-B", str(probe_build),
              "-G", "Visual Studio 17 2022", "-A", "x64"], tmp_path)
    _command([cmake, "--build", str(probe_build), "--config", "Debug", "--target", "probe"], tmp_path)
    challenge = make_challenge("cpp-compile-01", seed=99)
    result = subprocess.run([str(probe_build / "Debug" / "probe.exe")],
                            input=challenge.stdin, capture_output=True, timeout=10, check=False)
    assert result.returncode == 0
    assert not verify_response(challenge, returncode=result.returncode, stdout=result.stdout)


@pytest.mark.skipif(os.name != "nt" or shutil.which("cmake") is None,
                    reason="Native Qt/CMake probe requires Windows")
def test_qt_external_probe_observes_signal_values(tmp_path):
    qt = Path("D:/Qt/6.8.3/msvc2022_64")
    if not (qt / "lib/cmake/Qt6/Qt6Config.cmake").is_file():
        pytest.skip("Qt kit unavailable")
    cmake = shutil.which("cmake")
    candidate = tmp_path / "candidate"
    shutil.copytree(ROOT / "eval/fixtures/qt-signal-01", candidate)
    source = candidate / "src/subject.cpp"
    source.write_text(source.read_text(encoding="utf-8").replace(
        "QObject(parent) {  }", "QObject(parent) { connect(this, &Counter::pressed, this, &Counter::onPressed); }"),
        encoding="utf-8")
    build = tmp_path / "candidate-build"
    _command([cmake, "-S", str(candidate), "-B", str(build),
              "-G", "Visual Studio 17 2022", "-A", "x64",
              "-DQt6_DIR=" + str(qt / "lib/cmake/Qt6")], tmp_path)
    _command([cmake, "--build", str(build), "--config", "Debug", "--target", "subject"], tmp_path)
    probe_source = write_probe_project("qt-signal-01", tmp_path / "probe-source",
        candidate_include=candidate / "src", candidate_library=build / "Debug/subject.lib")
    probe_build = tmp_path / "probe-build"
    _command([cmake, "-S", str(probe_source), "-B", str(probe_build),
              "-G", "Visual Studio 17 2022", "-A", "x64",
              "-DQt6_DIR=" + str(qt / "lib/cmake/Qt6")], tmp_path)
    _command([cmake, "--build", str(probe_build), "--config", "Debug", "--target", "probe"], tmp_path)
    challenge = make_challenge("qt-signal-01", seed=73)
    result = subprocess.run([str(probe_build / "Debug/probe.exe")], input=challenge.stdin,
                            capture_output=True, timeout=15, check=False)
    assert verify_response(challenge, returncode=result.returncode, stdout=result.stdout)
