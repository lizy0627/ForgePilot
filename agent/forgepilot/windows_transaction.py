"""Bounded NTFS transactions for atomic file replacement with a locked CAS target.

There is deliberately no nontransactional fallback: allowing delete sharing for
a pathname-based replace otherwise leaves a final rename/rebind race.
"""
import ctypes
from ctypes import wintypes
import os

from .errors import ForgePilotError


class FileTransaction:
    def __init__(self):
        if os.name != "nt":
            raise ForgePilotError("platform_unsupported", "Atomic mutations require Windows NTFS transactions")
        try:
            self.files = ctypes.WinDLL("kernel32", use_last_error=True)
            self.ktm = ctypes.WinDLL("KtmW32", use_last_error=True)
            self.ktm.CreateTransaction.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPWSTR]
            self.ktm.CreateTransaction.restype = wintypes.HANDLE
            for name in ("CommitTransaction", "RollbackTransaction"):
                function = getattr(self.ktm, name)
                function.argtypes = [wintypes.HANDLE]
                function.restype = wintypes.BOOL
            self.files.CreateFileTransactedW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                wintypes.HANDLE, wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p]
            self.files.CreateFileTransactedW.restype = wintypes.HANDLE
            self.files.MoveFileTransactedW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE]
            self.files.MoveFileTransactedW.restype = wintypes.BOOL
            self.files.CloseHandle.argtypes = [wintypes.HANDLE]
            self.files.CloseHandle.restype = wintypes.BOOL
        except (AttributeError, OSError):
            raise ForgePilotError("atomic_unsupported", "Transactional NTFS is unavailable; mutation refused") from None
        self.handle = self.ktm.CreateTransaction(None, None, 0, 0, 0, 30_000, "ForgePilot atomic patch")
        if not self.handle or self.handle == ctypes.c_void_p(-1).value:
            raise ForgePilotError("atomic_unsupported", "Cannot create an NTFS transaction; mutation refused")
        self.committed = False

    def open_handle(self, path, access, share, disposition, flags):
        handle = self.files.CreateFileTransactedW(str(path), access, share, None,
            disposition, flags, None, self.handle, None, None)
        if handle == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(), "Cannot acquire a transactional file handle")
        return handle

    def replace(self, source, destination):
        if not self.files.MoveFileTransactedW(str(source), str(destination), None, None, 1, self.handle):
            raise OSError(ctypes.get_last_error(), "Transactional rename failed")

    def commit(self):
        if not self.ktm.CommitTransaction(self.handle):
            raise OSError(ctypes.get_last_error(), "Atomic patch transaction commit failed")
        self.committed = True

    def close(self):
        if self.handle is not None:
            try:
                if not self.committed:
                    self.ktm.RollbackTransaction(self.handle)
            finally:
                self.files.CloseHandle(self.handle)
                self.handle = None
