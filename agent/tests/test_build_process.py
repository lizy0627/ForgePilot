import asyncio
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys

import pytest

from forgepilot.build_process import BuildCancelled, run_build_process


def alive(pid):
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(api.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        api.CloseHandle(handle)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["cancel", "timeout", "normal_exit"])
async def test_owned_descendants_terminated(tmp_path, mode):
    root = tmp_path / "process tree with spaces"
    root.mkdir()
    child = root / "child.py"
    child.write_text('''import os, subprocess, sys, time
from pathlib import Path
grand = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(90)"])
Path("pids.json").write_text(__import__("json").dumps([os.getpid(), grand.pid]))
time.sleep(90)
''')
    parent = root / "parent.py"
    parent.write_text('''import subprocess, sys, time
from pathlib import Path
subprocess.Popen([sys.executable, "-I", "child.py"])
while not Path("pids.json").exists(): time.sleep(.01)
print("tree started", flush=True)
''' + ("" if mode == "normal_exit" else "time.sleep(90)\n"))
    task = asyncio.create_task(run_build_process([sys.executable, "-I", str(parent)], root,
                                                timeout=2 if mode == "timeout" else 30))
    async with asyncio.timeout(15):
        while not (root / "pids.json").exists():
            await asyncio.sleep(.02)
    pids = json.loads((root / "pids.json").read_text())
    if mode == "cancel":
        assert all(alive(pid) for pid in pids)
        task.cancel()
        with pytest.raises(BuildCancelled) as caught:
            await asyncio.wait_for(task, 10)
        result = caught.value.result
        assert result.status == "cancelled"
    else:
        result = await asyncio.wait_for(task, 10)
        assert result.status == ("timed_out" if mode == "timeout" else "succeeded")
    assert all(not alive(pid) for pid in pids), "Owned compiler/test descendants must be gone"
    assert "tree started" in result.stdout or mode == "cancel"


@pytest.mark.asyncio
async def test_output_limit_stops_tree_and_marks_truncation(tmp_path):
    result = await run_build_process([sys.executable, "-I", "-c", "print('x'*100000); import time; time.sleep(30)"],
                                    tmp_path, timeout=10, output_limit=1024)
    assert result.status == "output_limit" and result.truncated
    assert len(result.stdout) == 1024 and "TRUNCATED" in result.stderr


@pytest.mark.asyncio
async def test_missing_executable_has_diagnostic(tmp_path):
    result = await run_build_process([str(tmp_path / "missing.exe")], tmp_path, timeout=10)
    assert result.status == "failed" and result.returncode == 125 and result.stderr
    assert "Windows error 2" in result.stderr
    assert str(tmp_path) not in result.stderr
