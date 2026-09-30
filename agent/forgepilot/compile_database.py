"""Read existing build metadata as data. Never configure/build or execute a database command."""
from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import stat

from .build_process import build_environment
from .errors import ForgePilotError
from .presets import Presets, safe_build_path
from .repository import is_link, opened_path

MAX_DATABASE_BYTES = 4_194_304
MAX_ENTRIES = 10_000
CPP_SUFFIXES = {".cpp", ".cc", ".cxx", ".c++", ".c", ".h", ".hh", ".hpp", ".hxx"}


def metadata(repository, directory, name):
    repository._path(".")
    directory = Path(directory)
    if directory != repository.root:
        directory = safe_build_path(repository.root, str(directory))
    path = directory / name
    try:
        info = path.lstat()
        if is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ForgePilotError("compile_database_boundary", "Build metadata contains a link or non-regular file")
        with path.open("rb") as stream:
            actual = opened_path(stream.fileno())
            if actual is not None and actual != path:
                raise ForgePilotError("compile_database_boundary", "Build metadata location changed")
            repository._path(".")
            if directory != repository.root:
                safe_build_path(repository.root, str(directory))
            data = stream.read(MAX_DATABASE_BYTES + 1)
        if len(data) > MAX_DATABASE_BYTES:
            raise ForgePilotError("compile_database_limit", "Build metadata exceeds the byte limit")
        return data.decode("utf-8-sig")
    except FileNotFoundError:
        raise ForgePilotError("compile_database_missing", "No compile_commands.json in this build directory") from None
    except (OSError, UnicodeError, ValueError):
        raise ForgePilotError("compile_database_invalid", "Cannot read build metadata") from None


def command_arguments(command):
    """Windows command-line quoting only, not cmd.exe/PowerShell evaluation."""
    if not isinstance(command, str) or not command or len(command) > 32_768 or "\x00" in command:
        raise ForgePilotError("compile_database_invalid", "Invalid compilation command")
    api = ctypes.WinDLL("shell32", use_last_error=True).CommandLineToArgvW
    api.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    api.restype = ctypes.POINTER(wintypes.LPWSTR)
    count = ctypes.c_int()
    value = api(command, ctypes.byref(count))
    if not value:
        raise ForgePilotError("compile_database_invalid", "Cannot decode compilation command")
    try:
        return [value[i] for i in range(count.value)]
    finally:
        free = ctypes.WinDLL("kernel32").LocalFree
        free.argtypes = [ctypes.c_void_p]
        free(value)


def checked_arguments(entry):
    args = entry.get("arguments")
    if args is None:
        args = command_arguments(entry.get("command"))
    if (not isinstance(args, list) or not args or len(args) > 2048 or
            any(not isinstance(a, str) or not a or any(ord(c) < 32 for c in a) for a in args)
            or sum(map(len, args)) > 32_768):
        raise ForgePilotError("compile_database_invalid", "Invalid compilation arguments")
    # clangd must never load project-selected plugins, response files or virtual file overlays.
    # Reject these commands rather than silently changing their semantic meaning.
    denied = ("-xclang", "-load", "-plugin", "-fplugin", "-fpass-plugin", "-mllvm", "-cc1", "-wrapper", "-ivfsoverlay", "--vfsoverlay",
              "-fmodules", "-fmodule", "-include-pch", "-emit", "-save-temps", "--config", "/clang:", "/yc", "/yu", "/fp")
    if any(a.startswith("@") or a.casefold().startswith(denied) or a in {"&&", "||", "|", ";", ">", "<"} for a in args[1:]):
        raise ForgePilotError("compile_database_unsupported", "Compilation command requires unsupported indirect/plugin/module options")
    # Only driver names understood as C/C++ compilation, never wrappers such as cmd/cmake/python.
    if Path(args[0]).name.casefold() not in {"cl", "cl.exe", "clang", "clang.exe", "clang++", "clang++.exe",
            "clang-cl", "clang-cl.exe", "gcc", "gcc.exe", "g++", "g++.exe", "cc", "c++"}:
        raise ForgePilotError("compile_database_unsupported", "Compilation command uses an unsupported compiler wrapper")
    return args


@dataclass
class CompilationDatabase:
    path: Path
    entries: list[dict]
    files: list[str]
    omitted: int
    generator: str | None


def load_database(repository, directory):
    directory = Path(directory)
    if not directory.is_absolute():
        directory = repository.root / directory
    if directory != repository.root:
        directory = safe_build_path(repository.root, str(directory))
    cache = {}
    if (directory / "CMakeCache.txt").exists() or (directory / "CMakeCache.txt").is_symlink():
        for line in metadata(repository, directory, "CMakeCache.txt").splitlines():
            if not line.startswith(("#", "//")) and ":" in line and "=" in line:
                key, value = line.split("=", 1)
                cache[key.split(":", 1)[0]] = value
        if (Path(cache.get("CMAKE_HOME_DIRECTORY", "")).resolve() != repository.root or
                Path(cache.get("CMAKE_CACHEFILE_DIR", "")).resolve() != directory):
            raise ForgePilotError("compile_database_stale", "CMake cache belongs to another source/build directory")
        generator = cache.get("CMAKE_GENERATOR", "")
        if generator and "Ninja" not in generator and "Makefiles" not in generator:
            raise ForgePilotError("compile_database_generator", "This generator does not export compile_commands.json; use an explicitly configured separate Ninja analysis directory")
    try:
        raw = json.loads(metadata(repository, directory, "compile_commands.json"))
        if not isinstance(raw, list) or not raw or len(raw) > MAX_ENTRIES:
            raise ValueError()
        entries, files, omitted = [], [], 0
        for entry in raw:
            if not isinstance(entry, dict) or not isinstance(entry.get("file"), str) or not isinstance(entry.get("directory"), str):
                raise ValueError()
            cwd = Path(entry["directory"])
            if not cwd.is_absolute():
                raise ValueError()
            if cwd != repository.root:
                safe_build_path(repository.root, str(cwd))
            path = Path(entry["file"])
            if not path.is_absolute():
                path = cwd / path
            # Legitimate ../src entries are normalized, then checked through Repository's full boundary policy.
            path = Path(os.path.abspath(path))
            try:
                relative = path.relative_to(repository.root).as_posix()
                repository._path(relative)
            except (ValueError, ForgePilotError):
                omitted += 1
                continue
            if path.suffix.casefold() not in CPP_SUFFIXES:
                omitted += 1
                continue
            args = checked_arguments(entry)
            if relative not in files:
                files.append(relative)
                entries.append({"directory": str(cwd), "file": str(path), "arguments": args})
            else:
                omitted += 1  # Multiple configurations: first listed command, explicitly incomplete coverage.
        if not files:
            raise ForgePilotError("compile_database_empty", "No accessible C/C++ translation units in the database")
        return CompilationDatabase(directory / "compile_commands.json", entries, files, omitted, cache.get("CMAKE_GENERATOR"))
    except (ValueError, TypeError, RecursionError):
        raise ForgePilotError("compile_database_invalid", "Malformed compilation database") from None


def locate_database(repository, *, configured=None, selected=None):
    if configured:
        return load_database(repository, configured)
    if selected:
        selected = safe_build_path(repository.root, str(selected))
        # A configured active build wins. A merely planned, nonexistent build must not hide
        # the one real database already present in the project before this task.
        if any((selected / name).exists() or (selected / name).is_symlink()
               for name in ("compile_commands.json", "CMakeCache.txt")):
            return load_database(repository, selected)
    candidates = [repository.root, repository.root / ".forgepilot/build", repository.root / "build"]
    try:
        presets = Presets(repository.root, build_environment())
        for name in list(presets.raw["configure"])[:64]:
            preset = presets.effective("configure", name)
            if preset.get("binaryDir"):
                candidates.append(safe_build_path(repository.root, presets._expand(preset["binaryDir"], preset)))
    except ForgePilotError:
        pass  # A broken unrelated preset does not invalidate an explicitly present database.
    found = []
    for directory in dict.fromkeys(candidates):
        if (directory / "compile_commands.json").exists() or (directory / "compile_commands.json").is_symlink():
            found.append(directory)
    if len(found) > 1:
        raise ForgePilotError("compile_database_ambiguous", "Multiple compilation databases; configure --compile-commands-dir explicitly")
    if not found:
        raise ForgePilotError("compile_database_missing", "No existing compilation database; analysis does not run CMake without build approval")
    return load_database(repository, found[0])
