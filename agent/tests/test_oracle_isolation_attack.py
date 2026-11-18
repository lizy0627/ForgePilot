"""Offline AppContainer attack probe; no model, Oracle asset, or external host."""

from pathlib import Path
import hashlib
import shutil
import socket
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "eval"))
from forgepilot_eval.isolation import AppContainerDomain, IsolationConfig
from forgepilot_eval.grader import Grader
from forgepilot_eval.manifest import load_tasks
from forgepilot.build_process import build_environment


@pytest.mark.asyncio
async def test_candidate_executable_domain_denies_external_file_and_loopback(tmp_path):
    cmake = shutil.which("cmake")
    if sys.platform != "win32" or not cmake:
        pytest.skip("Windows CMake AppContainer probe")
    root = Path(__file__).resolve().parents[2]
    launcher_build = tmp_path / "launcher-build"
    for command in ([cmake, "-S", str(root / "eval/tools/appcontainer_launcher"),
                     "-B", str(launcher_build), "-G", "Visual Studio 17 2022", "-A", "x64"],
                    [cmake, "--build", str(launcher_build), "--config", "Debug"]):
        result = subprocess.run(command, capture_output=True, timeout=180, check=False)
        assert result.returncode == 0, result.stderr[-1000:]
    launcher = launcher_build / "Debug/appcontainer_launcher.exe"
    sentinel = tmp_path / "oracle-sentinel.txt"
    sentinel.write_text("private-sentinel", encoding="ascii")
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    build = candidate / "build"
    build.mkdir()
    script = candidate / "read.cmake"
    script.write_text(f'file(READ "{sentinel.as_posix()}" SECRET)\n'
                      f'file(WRITE "{(build / "leak.txt").as_posix()}" "${{SECRET}}")\n', encoding="utf-8")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        (candidate / "probe.cpp").write_text(
            '#include <fstream>\n#include <winsock2.h>\n'
            '#pragma comment(lib, "Ws2_32.lib")\n'
            f'int main() {{ std::ifstream file(R"({sentinel})"); if (file.good()) return 10; '
            'WSADATA data; if (WSAStartup(MAKEWORD(2,2), &data)) return 11; '
            'SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP); '
            'u_long nonblocking = 1; ioctlsocket(s, FIONBIO, &nonblocking); '
            'sockaddr_in addr{}; addr.sin_family = AF_INET; '
            f'addr.sin_port = htons({port}); addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK); '
            'int connected = connect(s, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)); '
            'if (connected != 0) { fd_set writable; FD_ZERO(&writable); FD_SET(s, &writable); '
            'timeval wait{2, 0}; if (select(0, nullptr, &writable, nullptr, &wait) > 0) '
            '{ int error = 0; int size = sizeof(error); getsockopt(s, SOL_SOCKET, SO_ERROR, '
            'reinterpret_cast<char*>(&error), &size); if (error == 0) connected = 0; } } '
            'closesocket(s); WSACleanup(); return connected == 0 ? 12 : 0; }\n', encoding="utf-8")
        (candidate / "CMakeLists.txt").write_text(
            'cmake_minimum_required(VERSION 3.24)\nproject(Attack LANGUAGES CXX)\n'
            'enable_testing()\nadd_executable(probe probe.cpp)\n'
            'add_custom_target(read_sentinel COMMAND ${CMAKE_COMMAND} -P "${CMAKE_SOURCE_DIR}/read.cmake")\n'
            'add_test(NAME probe COMMAND probe)\n', encoding="utf-8")
        for command in ([cmake, "-S", str(candidate), "-B", str(build),
                         "-G", "Visual Studio 17 2022", "-A", "x64"],
                        [cmake, "--build", str(build), "--config", "Debug", "--target", "probe"]):
            result = subprocess.run(command, capture_output=True, timeout=180, check=False)
            assert result.returncode == 0, result.stderr[-1000:]
        with AppContainerDomain(launcher) as domain:
            domain.grant(candidate, writable=True)
            domain.grant(Path(cmake).parent.parent, writable=False)
            env = build_environment()
            for key in ("PATH", "INCLUDE", "LIB", "LIBPATH"):
                if key in env:
                    env[key] = ";".join(part for part in env[key].split(";") if part)
            denied = await domain.run([cmake, "-P", str(script)], candidate, timeout=20, env=env)
            assert denied.status != "succeeded" and not (build / "leak.txt").exists()
            protected = root / "eval/oracles/cpp-compile-01.cpp"
            protected_probe = candidate / "read-protected.cmake"
            protected_probe.write_text(
                f'file(READ "{protected.as_posix()}" FIRST_BYTE LIMIT 1)\n'
                f'file(WRITE "{(build / "protected-read-result.txt").as_posix()}" "opened")\n',
                encoding="utf-8")
            protected_attempt = await domain.run([cmake, "-P", str(protected_probe)],
                                                 candidate, timeout=20, env=env)
            assert protected_attempt.status != "succeeded"
            assert not (build / "protected-read-result.txt").exists()
            executed = await domain.run([str(build / "Debug/probe.exe")], candidate,
                                        timeout=20, env=env)
            assert executed.status == "succeeded", executed.stderr[-1000:]
            ctest = str(Path(cmake).with_name("ctest.exe"))
            tested = await domain.run([ctest, "--test-dir", str(build), "-C", "Debug",
                                       "--no-tests=error"], candidate, timeout=30, env=env)
            assert tested.status == "succeeded", tested.stderr[-1000:]


@pytest.mark.asyncio
async def test_candidate_configure_and_build_in_restricted_domain(tmp_path, request):
    cmake, compiler, make, rc, mt = (shutil.which(name) for name in
                                   ("cmake", "cl", "nmake", "rc", "mt"))
    if sys.platform != "win32" or not all((cmake, compiler, make, rc, mt)):
        pytest.skip("Run from a configured MSVC developer environment")
    root = Path(__file__).resolve().parents[2]
    launcher_build = tmp_path / "launcher-build"
    for command in ([cmake, "-S", str(root / "eval/tools/appcontainer_launcher"),
                     "-B", str(launcher_build), "-G", "Visual Studio 17 2022", "-A", "x64"],
                    [cmake, "--build", str(launcher_build), "--config", "Debug"]):
        result = subprocess.run(command, capture_output=True, timeout=180, check=False)
        assert result.returncode == 0, result.stderr[-1000:]
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    sentinel = tmp_path / "outside-oracle-sentinel.txt"
    sentinel.write_text("private-sentinel", encoding="ascii")
    (candidate / "read.cmake").write_text(
        f'file(READ "{sentinel.as_posix()}" SECRET)\n'
        f'file(WRITE "{(candidate / "leak.txt").as_posix()}" "${{SECRET}}")\n', encoding="utf-8")
    listener = socket.socket()
    request.addfinalizer(listener.close)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    (candidate / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.24)\nproject(IsolatedBuild LANGUAGES CXX)\n"
        "enable_testing()\nadd_executable(probe probe.cpp)\n"
        "add_custom_target(read_sentinel COMMAND ${CMAKE_COMMAND} -P \"${CMAKE_SOURCE_DIR}/read.cmake\")\n"
        "add_test(NAME probe COMMAND probe)\n",
        encoding="utf-8")
    (candidate / "probe.cpp").write_text(
        '#include <fstream>\n#include <winsock2.h>\n'
        '#pragma comment(lib, "Ws2_32.lib")\n'
        f'int main() {{ std::ifstream file(R"({sentinel})"); if (file.good()) return 10; '
        'WSADATA data; if (WSAStartup(MAKEWORD(2,2), &data)) return 11; '
        'SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP); '
        'u_long nonblocking = 1; ioctlsocket(s, FIONBIO, &nonblocking); '
        'sockaddr_in addr{}; addr.sin_family = AF_INET; '
        f'addr.sin_port = htons({port}); addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK); '
        'int connected = connect(s, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)); '
        'if (connected != 0) { fd_set writable; FD_ZERO(&writable); FD_SET(s, &writable); '
        'timeval wait{2, 0}; if (select(0, nullptr, &writable, nullptr, &wait) > 0) '
        '{ int error = 0; int size = sizeof(error); getsockopt(s, SOL_SOCKET, SO_ERROR, '
        'reinterpret_cast<char*>(&error), &size); if (error == 0) connected = 0; } } '
        'closesocket(s); WSACleanup(); return connected == 0 ? 12 : 0; }\n', encoding="utf-8")
    build = candidate / "build"
    build.mkdir()
    tools = tmp_path / "toolchain-copy"
    tools.mkdir()
    shutil.copy2(rc, tools / "rc.exe")
    shutil.copy2(mt, tools / "mt.exe")
    for name in ("rcdll.dll", "midlrtmd.dll"):
        shutil.copy2(Path(rc).with_name(name), tools / name)
    # The installed SDK libraries lack AppContainer read ACLs on this host.
    # Copy only the linker's default set into an evaluator-owned read-only tree.
    sdk = Path(rc).parents[3] / "Lib" / Path(rc).parents[1].name
    for name in ("kernel32", "user32", "gdi32", "winspool", "shell32",
                 "ole32", "oleaut32", "uuid", "comdlg32", "advapi32", "ws2_32"):
        shutil.copy2(sdk / "um/x64" / (name + ".lib"), tools / (name + ".lib"))
    for name in ("ucrt.lib", "ucrtd.lib"):
        shutil.copy2(sdk / "ucrt/x64" / name, tools / name)
    for name in ("ucrt", "shared", "um"):
        shutil.copytree(sdk.parents[1] / "Include" / sdk.name / name, tools / name)
    with AppContainerDomain(launcher_build / "Debug/appcontainer_launcher.exe") as domain:
        domain.grant(candidate, writable=True)
        domain.grant(Path(cmake).parent.parent, writable=False)
        domain.grant(tools, writable=False)
        env = build_environment()
        env["LIB"] = str(tools) + ";" + env.get("LIB", "")
        env["INCLUDE"] = ";".join(str(tools / name) for name in ("ucrt", "shared", "um")) + ";" + env.get("INCLUDE", "")
        denied = await domain.run([cmake, "-P", str(candidate / "read.cmake")],
                                  candidate, timeout=20, env=env)
        assert denied.status != "succeeded" and not (candidate / "leak.txt").exists()
        configured = await domain.run([cmake, "-S", str(candidate), "-B", str(build),
            "-G", "NMake Makefiles", "-DCMAKE_CXX_COMPILER=" + compiler,
            "-DCMAKE_MAKE_PROGRAM=" + make, "-DCMAKE_CXX_FLAGS=/Z7",
            "-DCMAKE_CXX_FLAGS_DEBUG=/Z7 /Ob0 /Od /RTC1",
            "-DCMAKE_RC_COMPILER=" + (tools / "rc.exe").as_posix(),
            "-DCMAKE_MT=" + (tools / "mt.exe").as_posix()],
            candidate, timeout=120, env=env)
        assert configured.status == "succeeded", configured.stdout[-1000:] + configured.stderr[-1000:]
        attacked = await domain.run([cmake, "--build", str(build), "--config", "Debug",
            "--target", "read_sentinel"], candidate, timeout=30, env=env)
        assert attacked.status != "succeeded" and not (candidate / "leak.txt").exists()
        built = await domain.run([cmake, "--build", str(build), "--config", "Debug"],
                                 candidate, timeout=120, env=env)
        assert built.status == "succeeded", built.stdout[-1000:] + built.stderr[-1000:]
        tested = await domain.run([str(Path(cmake).with_name("ctest.exe")), "--test-dir", str(build),
            "-C", "Debug", "--no-tests=error"], candidate, timeout=30, env=env)
        assert tested.status == "succeeded", tested.stdout[-1000:] + tested.stderr[-1000:]

    fixed = tmp_path / "fixed-candidate"
    shutil.copytree(root / "eval/fixtures/cpp-compile-01", fixed)
    (fixed / "src/subject.cpp").write_text(
        '#include "subject.h"\nint add(int a, int b) { return a + b; }\n', encoding="utf-8")
    pinned = {"launcher": launcher_build / "Debug/appcontainer_launcher.exe",
              "cmake": Path(cmake), "ctest": Path(cmake).with_name("ctest.exe"),
              "make": Path(make), "compiler": Path(compiler),
              "rc": tools / "rc.exe", "mt": tools / "mt.exe"}
    config = IsolationConfig(**pinned, env=env,
        read_only_roots=(Path(cmake).parent.parent, tools), expected_sha256={
        name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in pinned.items()})
    grader = Grader(root / "eval", cmake, str(pinned["ctest"]), isolation=config)
    task = next(item for item in load_tasks() if item.id == "cpp-compile-01")
    grade = await grader.run_isolated(task, fixed, tmp_path / "graded-candidate")
    assert grade["oracle_status"] == "passed", grade
    assert grade["test_status"] == "passed", grade
