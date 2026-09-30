"""Current-user DPAPI JSON credentials. No shell, files or machine-wide scope."""
import ctypes
from ctypes import wintypes
import json
import os

from .errors import ForgePilotError

MAX_SECRET_BLOB = 524_288
_ENTROPY = b"ForgePilot MCP credentials v1"


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(value):
    buffer = ctypes.create_string_buffer(value)
    return buffer, _Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))


def _transform(value, *, encrypt):
    if os.name != "nt":
        raise ForgePilotError("mcp_secret_unavailable", "MCP credentials require current-user Windows DPAPI")
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    function = crypt.CryptProtectData if encrypt else crypt.CryptUnprotectData
    function.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR if encrypt else ctypes.c_void_p,
                        ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    function.restype = wintypes.BOOL
    source_buffer, source = _blob(value)
    entropy_buffer, entropy = _blob(_ENTROPY)
    destination = _Blob()
    try:
        # CRYPTPROTECT_UI_FORBIDDEN only: no LOCAL_MACHINE flag, no UI prompts.
        if not function(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 0x01, ctypes.byref(destination)):
            raise ForgePilotError("mcp_secret_unavailable", "Cannot protect or read saved MCP credentials")
        if destination.size > MAX_SECRET_BLOB:
            raise ForgePilotError("mcp_secret_unavailable", "MCP credential data exceeds its limit")
        return ctypes.string_at(destination.data, destination.size)
    finally:
        ctypes.memset(source_buffer, 0, len(source_buffer))
        ctypes.memset(entropy_buffer, 0, len(entropy_buffer))
        if destination.data:
            ctypes.memset(destination.data, 0, destination.size)
            kernel.LocalFree(destination.data)


def _checked(values):
    if (not isinstance(values, dict) or len(values) > 16
            or any(not isinstance(key, str) or not 1 <= len(key) <= 132
                   or not isinstance(value, str) or len(value) > 4096 or "\0" in value
                   for key, value in values.items())):
        raise ForgePilotError("mcp_secret_unavailable", "Invalid MCP credential data")
    return values


def protect_secrets(values):
    try:
        raw = json.dumps(_checked(values), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, UnicodeError):
        raise ForgePilotError("mcp_secret_unavailable", "Invalid MCP credential data") from None
    if len(raw) > MAX_SECRET_BLOB:
        raise ForgePilotError("mcp_secret_unavailable", "MCP credential data exceeds its limit")
    return _transform(raw, encrypt=True)


def unprotect_secrets(ciphertext):
    if not isinstance(ciphertext, bytes) or not 1 <= len(ciphertext) <= MAX_SECRET_BLOB:
        raise ForgePilotError("mcp_secret_unavailable", "Cannot read saved MCP credentials")
    try:
        return _checked(json.loads(_transform(ciphertext, encrypt=False).decode("utf-8")))
    except (ValueError, UnicodeError, RecursionError):
        raise ForgePilotError("mcp_secret_unavailable", "Cannot read saved MCP credentials") from None
