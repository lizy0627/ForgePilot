"""Bounded build processes in a Windows Job Object; not a public shell tool."""

import asyncio
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
from time import perf_counter
from uuid import uuid4

from .errors import ForgePilotError


def build_environment() -> dict[str, str]:
    # Preserve an already initialized MSVC kit, without credentials or arbitrary startup hooks.
    names = {"SYSTEMROOT", "SYSTEMDRIVE", "PUBLIC", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "COMSPEC",
             "INCLUDE", "LIB", "LIBPATH", "VSLANG", "VISUALSTUDIOVERSION", "VSINSTALLDIR",
             "VCINSTALLDIR", "VCTOOLSINSTALLDIR", "VCTOOLSVERSION", "VCTOOLSREDISTDIR",
             "WINDOWSSDKDIR", "WINDOWSSDKVERSION", "WINDOWSSDKBINPATH", "WINDOWSSDKVERBINPATH",
             "UNIVERSALCRTSDKDIR", "UCRTVERSION", "FRAMEWORKDIR", "FRAMEWORKVERSION",
             "FRAMEWORKDIR64", "FRAMEWORKVERSION64", "PLATFORM", "NUMBER_OF_PROCESSORS",
             "PROCESSOR_ARCHITECTURE", "PROCESSOR_ARCHITEW6432", "PROGRAMDATA", "ALLUSERSPROFILE",
             "USERPROFILE", "LOCALAPPDATA", "APPDATA", "HOMEDRIVE", "HOMEPATH", "PROGRAMFILES",
             "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)",
             "COMMONPROGRAMW6432"}
    env = {k: v for k, v in os.environ.items() if k.upper() in names}
    env.update(PYTHONIOENCODING="utf-8", CLICOLOR="0", CMAKE_COLOR_DIAGNOSTICS="OFF", MSBUILDDISABLENODEREUSE="1")
    return env


class _BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                ("active", wintypes.DWORD), ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", _BasicLimits), ("io", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]


class ProcessJob:
    def __init__(self):
        if os.name != "nt":
            raise ForgePilotError("platform_unsupported", "Build process ownership requires Windows")
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.api.SetInformationJobObject.restype = wintypes.BOOL
        self.api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.api.TerminateJobObject.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.name = "Local\\ForgePilotBuild-" + uuid4().hex
        self.handle = self.api.CreateJobObjectW(None, self.name)
        limits = _ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway.
        if not self.handle or not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise ForgePilotError("build_job", "Cannot create a process ownership job")

    def terminate(self):
        if self.handle:
            self.api.TerminateJobObject(self.handle, 0xC000013A)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


@dataclass
class CommandResult:
    command: list[str]
    cwd: str
    returncode: int | None
    stdout: str
    stderr: str
    duration_ms: int
    status: str
    truncated: bool = False


class BuildCancelled(asyncio.CancelledError):
    def __init__(self, result: CommandResult):
        super().__init__("Build process cancelled")
        self.result = result


async def run_build_process(command: list[str], cwd: Path, *, timeout: float,
                            env: dict[str, str] | None = None, output_limit: int = 1_048_576) -> CommandResult:
    if (type(command) is not list or not command or len(command) > 257 or not command[0]
            or any(not isinstance(arg, str) or "\x00" in arg for arg in command)
            or len(json.dumps(command, ensure_ascii=True)) > 120_000):
        raise ForgePilotError("command_denied", "Build processes require a bounded executable/argument array")
    started = perf_counter()
    job = ProcessJob()
    proc = None
    launcher = None
    readers = []
    buffers = [bytearray(), bytearray()]
    status = "failed"
    truncated = False
    cancelled = False
    diagnostic = ""

    async def read(stream, buffer):
        nonlocal truncated
        while chunk := await stream.read(16_384):
            remaining = output_limit - len(buffer)
            buffer.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
                job.terminate()
                break

    try:
        async with asyncio.timeout(timeout):
            # The trusted worker assigns itself to the job BEFORE spawning the requested
            # executable. This avoids a launch/AssignProcess race in compiler descendants.
            launcher = asyncio.create_task(asyncio.create_subprocess_exec(
                sys.executable, "-I", str(Path(__file__).with_name("_build_worker.py")), job.name,
                cwd=cwd, env=env or build_environment(), shell=False, creationflags=subprocess.CREATE_NO_WINDOW,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE))
            proc = await asyncio.shield(launcher)
            readers = [asyncio.create_task(read(proc.stdout, buffers[0])),
                       asyncio.create_task(read(proc.stderr, buffers[1]))]
            proc.stdin.write(json.dumps({"command": command, "cwd": str(cwd)}, ensure_ascii=True).encode() + b"\n")
            await proc.stdin.drain()
            proc.stdin.close()
            while proc.returncode is None:
                await asyncio.sleep(0.02)
            status = "succeeded" if proc.returncode == 0 else "failed"
    except TimeoutError:
        status = "timed_out"
        diagnostic = "Process wall-clock timeout; owned process tree terminated"
    except asyncio.CancelledError:
        status, cancelled = "cancelled", True
        diagnostic = "Cancelled; owned process tree terminated"
    except (OSError, ValueError):
        status = "spawn_failed"
        diagnostic = "Cannot start the configured build executable/worker"
    finally:
        if proc is None and launcher is not None:
            while True:
                try:
                    proc = await asyncio.shield(launcher)
                    break
                except asyncio.CancelledError:
                    cancelled, status = True, "cancelled"
                except OSError:
                    break
        job.terminate()  # Also remove lingering compiler/test children after a normal root exit.
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()  # Covers a worker cancelled before it joined the job.
            except ProcessLookupError:
                pass
        job.close()
        async def drain():
            if proc is not None:
                await proc.wait()
            if readers:
                await asyncio.gather(*readers)
        cleanup = asyncio.create_task(drain())
        while True:
            try:
                await asyncio.shield(cleanup)
                break
            except asyncio.CancelledError:
                # Repeated Ctrl+C must not interrupt owned-tree cleanup or lose command evidence.
                cancelled, status = True, "cancelled"
                diagnostic = "Cancelled; owned process tree terminated"
    if truncated:
        status = "cancelled" if cancelled else "output_limit"
        diagnostic += "\n[TRUNCATED: process output limit; process tree terminated]"
    def decode(data):
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("mbcs", errors="replace")
    result = CommandResult(list(command), str(cwd), proc.returncode if proc else None,
        decode(buffers[0]), decode(buffers[1]) + ("\n" + diagnostic if diagnostic else ""),
        int((perf_counter() - started) * 1_000), status, truncated)
    if cancelled:
        raise BuildCancelled(result)
    return result
