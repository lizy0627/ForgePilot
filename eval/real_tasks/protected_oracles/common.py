"""Evaluator-only Oracle helpers. Never copy these files into a candidate checkout."""
import argparse
import os
from pathlib import Path
import signal
import shutil
import subprocess
import tempfile


def repository_argument():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--cmake", default="cmake")
    parser.add_argument("--ctest", default="ctest")
    parser.add_argument("--generator")
    args = parser.parse_args()
    args.repository = args.repository.resolve(strict=True)
    if not (args.repository / "CMakeLists.txt").is_file():
        raise ValueError("Candidate source is missing CMakeLists.txt")
    return args


class OracleTimeout(Exception):
    pass


def _stop_process_tree(process):
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10, check=False,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
        process.wait(timeout=5)


def command(argv, cwd, timeout=180):
    with subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding="utf-8", errors="replace",
                          start_new_session=os.name != "nt",
                          creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                         if os.name == "nt" else 0)) as process:
        try:
            process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _stop_process_tree(process)
            raise OracleTimeout from exc
        return process.returncode == 0


def run_cpp_oracle(args, source, *, include_dir="include", extra_sources=(), standard=11):
    cmake = shutil.which(args.cmake)
    ctest = shutil.which(args.ctest)
    if not cmake or not ctest:
        return "tool_missing"
    # Repository paths are embedded in CMake syntax below. Reject characters
    # with special meaning there instead of interpolating an untrusted path.
    if any(char in str(args.repository) for char in ('"', ';', '$', '\n', '\r')):
        return "path_unsupported"
    with tempfile.TemporaryDirectory(prefix="forgepilot-protected-oracle-") as scratch_name:
        scratch = Path(scratch_name).resolve()
        if scratch.is_relative_to(args.repository) or args.repository.is_relative_to(scratch):
            return "isolation_failed"
        src = scratch / "src"
        src.mkdir()
        (src / "oracle.cpp").write_text(source, encoding="utf-8")
        sources = ["oracle.cpp"] + [f'"{(args.repository / p).as_posix()}"' for p in extra_sources]
        cmake_source = (
            "cmake_minimum_required(VERSION 3.5)\n"
            "project(ProtectedOracle LANGUAGES CXX)\n"
            f"set(CMAKE_CXX_STANDARD {standard})\n"
            "set(CMAKE_CXX_STANDARD_REQUIRED ON)\n"
            f"add_executable(oracle {' '.join(sources)})\n"
            f'target_include_directories(oracle PRIVATE "{(args.repository / include_dir).as_posix()}")\n'
            "enable_testing()\n"
            "add_test(NAME oracle COMMAND oracle)\n"
        )
        (src / "CMakeLists.txt").write_text(cmake_source, encoding="utf-8")
        build = scratch / "build"
        configure = [cmake, "-S", str(src), "-B", str(build)]
        if args.generator:
            configure.extend(["-G", args.generator])
        if not command(configure, scratch):
            return "configure_failed"
        if not command([cmake, "--build", str(build), "--config", "Debug"], scratch):
            return "build_failed"
        if not command([ctest, "--test-dir", str(build), "-C", "Debug", "--output-on-failure"], scratch):
            return "test_failed"
    return "passed"


def finish(status):
    # Deliberately finite feedback. The candidate never sees Oracle source or logs.
    if status == "passed":
        print("oracle_passed")
        return 0
    print("oracle_failed:" + status)
    return 1


def guarded(probe):
    """Keep all Oracle failures finite; never emit source, logs or tracebacks."""
    try:
        return finish(probe())
    except OracleTimeout:
        return finish("timeout")
    except Exception:
        return finish("execution_error")
