"""Windows AppContainer process boundary for evaluator-owned build commands.

Each domain gets a fresh package SID with no capabilities (including network).
Only trusted evaluator code may grant ACLs, and only on disposable workspaces or
read-only toolchain copies. All child processes are owned by run_build_process's
Windows Job Object. This module does not by itself validate a Protected Oracle.
"""

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import stat
import subprocess
from uuid import uuid4

from forgepilot.build_process import build_environment, run_build_process


class IsolationError(RuntimeError):
    pass


def validate_environment(env: dict[str, str], *untrusted_roots: Path) -> None:
    # Only the variables that the product's build_environment explicitly keeps
    # may reach a candidate process. This excludes API keys, proxy passwords,
    # arbitrary startup hooks and all FORGEPILOT_* variables.
    allowed = {key.upper() for key in build_environment()}
    if any(key.upper() not in allowed for key in env):
        raise IsolationError("Evaluator environment contains a non-toolchain variable")
    roots = [path.resolve(strict=True) for path in untrusted_roots if path.exists()]
    for key in ("PATH", "INCLUDE", "LIB", "LIBPATH"):
        raw = next((v for k, v in env.items() if k.upper() == key), None)
        if raw is None: continue
        for value in raw.split(";"):
            if not value:
                raise IsolationError("Evaluator toolchain path has an empty component")
            component = Path(value)
            if not component.is_absolute():
                raise IsolationError("Evaluator toolchain path has a relative component")
            resolved = component.resolve(strict=False)
            if any(resolved == root or resolved.is_relative_to(root) for root in roots):
                raise IsolationError("Evaluator toolchain path enters candidate writable tree")


@dataclass(frozen=True)
class IsolationConfig:
    launcher: Path
    cmake: Path
    ctest: Path
    make: Path
    compiler: Path
    rc: Path
    mt: Path
    env: dict[str, str]
    qt_kit: Path | None = None
    expected_sha256: dict[str, str] | None = None
    read_only_roots: tuple[Path, ...] = ()

    def validate_for(self, *candidate_writable_roots: Path) -> None:
        paths = {name: getattr(self, name) for name in
            ("launcher", "cmake", "ctest", "make", "compiler", "rc", "mt")}
        if self.expected_sha256 is None or set(self.expected_sha256) != set(paths):
            raise IsolationError("Pinned evaluator tool hashes are missing")
        roots = [p.resolve(strict=True) for p in candidate_writable_roots if p.exists()]
        for tool_root in self.read_only_roots:
            if not tool_root.is_absolute():
                raise IsolationError("Read-only toolchain root is relative")
            assert_plain_tree(tool_root)
            resolved_tool = tool_root.resolve(strict=True)
            if any(resolved_tool == root or resolved_tool.is_relative_to(root)
                   or root.is_relative_to(resolved_tool) for root in roots):
                raise IsolationError("Read-only toolchain root overlaps candidate writable tree")
        for name, path in paths.items():
            if not path.is_file():
                raise IsolationError("Pinned evaluator executable is missing")
            resolved = path.resolve(strict=True)
            if any(resolved == root or resolved.is_relative_to(root) for root in roots):
                raise IsolationError("Evaluator executable is inside candidate writable tree")
            if hashlib.sha256(resolved.read_bytes()).hexdigest() != self.expected_sha256[name]:
                raise IsolationError("Pinned evaluator executable hash changed")
        if self.qt_kit is not None:
            qt = self.qt_kit.resolve(strict=True)
            if any(qt == root or qt.is_relative_to(root) for root in roots):
                raise IsolationError("Evaluator Qt toolchain is inside candidate writable tree")
        validate_environment(self.env, *roots)

    def grant_read_only(self, domain: "AppContainerDomain", *, qt: bool = False) -> None:
        for root in self.read_only_roots:
            domain.grant(root, writable=False)
        if qt and self.qt_kit is not None:
            domain.grant(self.qt_kit, writable=False)

    def cmake_options(self, *, qt: bool = False) -> list[str]:
        if qt and self.qt_kit is None:
            raise IsolationError("Qt task has no isolated Qt toolchain")
        options = ["-G", "NMake Makefiles", "-DCMAKE_BUILD_TYPE=Debug",
            "-DCMAKE_CXX_FLAGS=/Z7 /EHsc /GR",
            "-DCMAKE_CXX_FLAGS_DEBUG=/Z7 /Ob0 /Od /RTC1",
            "-DCMAKE_MAKE_PROGRAM=" + self.make.as_posix(),
            "-DCMAKE_CXX_COMPILER=" + self.compiler.as_posix(),
            "-DCMAKE_RC_COMPILER=" + self.rc.as_posix(),
            "-DCMAKE_MT=" + self.mt.as_posix()]
        if qt:
            options.append("-DQt6_DIR=" + (self.qt_kit / "lib/cmake/Qt6").as_posix())
            options.append("-DCMAKE_AUTOGEN_PARALLEL=1")
            options.append("-DCMAKE_AUTOGEN_VERBOSE=ON")
        return options


def _apis():
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    userenv.CreateAppContainerProfile.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    userenv.CreateAppContainerProfile.restype = ctypes.c_long
    userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p)]
    userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
    userenv.DeleteAppContainerProfile.argtypes = [wintypes.LPCWSTR]
    userenv.DeleteAppContainerProfile.restype = ctypes.c_long
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    return userenv, advapi, kernel


def create_profile(name: str) -> str:
    """Create a new per-run profile and return its unforgeable package SID."""
    userenv, advapi, kernel = _apis()
    sid = ctypes.c_void_p()
    hr = userenv.CreateAppContainerProfile(name, "ForgePilot evaluator",
        "Temporary isolated evaluator process", None, 0, ctypes.byref(sid))
    if hr != 0 or not sid:
        raise IsolationError(f"AppContainer profile creation failed: HRESULT 0x{hr & 0xffffffff:08x}")
    value = wintypes.LPWSTR()
    try:
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(value)):
            raise IsolationError(f"AppContainer SID conversion failed: Windows error {ctypes.get_last_error()}")
        return value.value
    finally:
        if value: kernel.LocalFree(value)
        kernel.LocalFree(sid)


def grant_acl(path: Path, sid: str, *, writable: bool) -> None:
    """Grant a profile on one caller-vetted path; fail if any child ACL fails."""
    if not path.exists() or not path.is_absolute() or not sid.startswith("S-1-15-2-"):
        raise IsolationError("AppContainer ACL path or SID is invalid")
    # icacls /T traverses descendants. Reject every reparse point before it can
    # redirect an ACL grant outside the disposable workspace. The caller runs
    # this before starting the untrusted child, so no candidate races the scan.
    assert_plain_tree(path)
    rights = "M" if writable else "RX"
    ace = f"*{sid}:(OI)(CI){rights}" if path.is_dir() else f"*{sid}:{rights}"
    system_directory = ctypes.create_unicode_buffer(32768)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    kernel.GetSystemDirectoryW.restype = wintypes.UINT
    length = kernel.GetSystemDirectoryW(system_directory, len(system_directory))
    if not length or length >= len(system_directory):
        raise IsolationError("Windows system directory unavailable")
    system_root = Path(system_directory.value)
    icacls = system_root / "icacls.exe"
    if not icacls.is_file():
        raise IsolationError("Windows ACL tool unavailable")
    command = [str(icacls), str(path), "/grant", ace, "/T", "/Q"]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=180, check=False,
                            cwd=system_root, env=build_environment())
    # icacls output is localized; the last two integers on its summary line
    # are processed and failed counts. It can return zero with failed entries.
    summary = result.stdout.strip().splitlines()
    counts = re.findall(r"\d+", summary[-1]) if summary else []
    if result.returncode or len(counts) < 2 or int(counts[-1]) != 0:
        raise IsolationError(f"AppContainer ACL grant failed for {path}")


def assert_plain_tree(path: Path) -> None:
    """Reject reparse points, hardlinks, and resolved escapes before trusted reads."""
    if not path.is_absolute() or not path.exists():
        raise IsolationError("Isolation tree root is missing or relative")
    current = path
    while current != current.parent:
        if current.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise IsolationError("Isolation tree path contains a reparse point")
        current = current.parent
    root = path.resolve(strict=True)
    if path.is_dir():
        pending = [path]
        while pending:
            directory = pending.pop()
            for child in directory.iterdir():
                metadata = child.lstat()
                if metadata.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise IsolationError("Isolation tree contains a reparse point")
                if not child.resolve(strict=True).is_relative_to(root):
                    raise IsolationError("Isolation tree child escaped root")
                if child.is_dir(): pending.append(child)
                elif child.is_file():
                    if metadata.st_nlink != 1:
                        raise IsolationError("Isolation tree contains a hardlink")
                else:
                    raise IsolationError("Isolation tree contains an unsupported file type")
    elif path.lstat().st_nlink != 1:
        raise IsolationError("Isolation tree root is a hardlink")


class AppContainerDomain:
    def __init__(self, launcher: Path):
        if not launcher.is_file():
            raise IsolationError("AppContainer launcher binary is missing")
        self.launcher = launcher.resolve(strict=True)
        self.name = "forgepiloteval" + uuid4().hex
        self.sid = create_profile(self.name)
        self._grants: dict[Path, bool] = {}

    def grant(self, path: Path, *, writable: bool) -> None:
        if path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise IsolationError("AppContainer ACL root is a reparse point")
        resolved = path.resolve(strict=True)
        if resolved in self._grants:
            if self._grants[resolved] or not writable: return
        grant_acl(resolved, self.sid, writable=writable)
        self._grants[resolved] = writable

    async def run(self, argv: list[str], cwd: Path, *, timeout: float,
                  env: dict[str, str] | None = None, input_data: bytes | None = None):
        resolved = cwd.resolve(strict=True)
        if not any(resolved == p or resolved.is_relative_to(p)
                   for p, _ in self._grants.items()):
            raise IsolationError("AppContainer working directory has no granted ACL")
        clean = env if env is not None else build_environment()
        validate_environment(clean)
        return await run_build_process([str(self.launcher), self.name, *argv], resolved,
                                       timeout=timeout, env=clean, input_data=input_data)

    def close(self) -> None:
        userenv, _, _ = _apis()
        userenv.DeleteAppContainerProfile(self.name)

    def __enter__(self): return self
    def __exit__(self, *_): self.close()
