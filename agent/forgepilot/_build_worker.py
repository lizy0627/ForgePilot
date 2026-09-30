"""Private Windows launcher. Invoked by build_process with -I, never exposed as a tool."""

import ctypes
from ctypes import wintypes
import json
import subprocess
import sys


def main():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    api.OpenJobObjectW.restype = wintypes.HANDLE
    api.GetCurrentProcess.restype = wintypes.HANDLE
    api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    api.AssignProcessToJobObject.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    job = api.OpenJobObjectW(1, False, sys.argv[1])  # JOB_OBJECT_ASSIGN_PROCESS
    if not job or not api.AssignProcessToJobObject(job, api.GetCurrentProcess()):
        print(f"forgepilot-worker: process ownership setup failed (Windows error {ctypes.get_last_error()})",
              file=sys.stderr, flush=True)
        return 125
    api.CloseHandle(job)  # Parent owns the sole lifetime handle; its death kills this tree.
    try:
        data = json.loads(sys.stdin.buffer.readline(131_073))
        return subprocess.run(data["command"], cwd=data["cwd"], stdin=subprocess.DEVNULL,
                              creationflags=subprocess.CREATE_NO_WINDOW).returncode
    except OSError as exc:
        # Numeric diagnostics explain OS launch failures without echoing command
        # arguments, environment values or arbitrary exception text.
        print(f"forgepilot-worker: configured executable could not be launched "
              f"(Windows error {exc.winerror}, errno {exc.errno})", file=sys.stderr, flush=True)
        return 125
    except (ValueError, KeyError):
        print("forgepilot-worker: configured executable could not be launched", file=sys.stderr, flush=True)
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
