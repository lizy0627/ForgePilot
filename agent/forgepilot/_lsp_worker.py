"""Trusted stdio launcher: join the owned Job before starting clangd/MCP, never a shell."""
import ctypes
from ctypes import wintypes
import subprocess
import sys


def main():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    api.OpenJobObjectW.restype = wintypes.HANDLE
    api.GetCurrentProcess.restype = wintypes.HANDLE
    api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    job = api.OpenJobObjectW(1, False, sys.argv[1])
    if not job or not api.AssignProcessToJobObject(job, api.GetCurrentProcess()):
        return 125
    api.CloseHandle(job)
    try:
        return subprocess.run(sys.argv[2:], stdin=sys.stdin.buffer,
                              creationflags=subprocess.CREATE_NO_WINDOW).returncode
    except OSError:
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
