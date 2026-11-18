"""Prepare a one-run Windows CMake runtime for the offline AppContainer tests."""

import hashlib
import json
from pathlib import Path
import shutil
import stat


def _inventory(root: Path) -> dict[str, str | None]:
    """Record relative names, empty directories and file bytes without following links."""
    contents = {}
    pending = [root / "bin", root / "share"]
    while pending:
        path = pending.pop()
        metadata = path.lstat()
        name = path.relative_to(root).as_posix()
        if getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError(f"Unexpected reparse point in CMake runtime: {path}")
        if stat.S_ISDIR(metadata.st_mode):
            contents[name] = None
            pending.extend(path.iterdir())
        elif stat.S_ISREG(metadata.st_mode):
            with path.open("rb") as source:
                contents[name] = hashlib.file_digest(source, "sha256").hexdigest()
        else:
            raise ValueError(f"Unexpected entry in CMake runtime: {path}")
    return contents


def copy_cmake_runtime(owned_root: Path, cmake: Path, ctest: Path) -> dict:
    """Copy bin and share into a fresh child of the caller's private run directory.

    Existing destinations are never reused. The caller owns cleanup of the run
    directory, including a partially copied runtime after an exception.
    """
    for path in (owned_root, cmake, ctest):
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise ValueError(f"Expected a resolved absolute path: {path}")
    # Windows PATH lookup can return the uppercase PATHEXT suffix even when
    # the directory entry is lowercase. Retain the on-disk spelling for hashes.
    cmake = cmake.resolve(strict=True)
    ctest = ctest.resolve(strict=True)
    if (cmake.name.lower() != "cmake.exe" or ctest.name.lower() != "ctest.exe"
            or not cmake.is_file() or not ctest.is_file()
            or cmake.parent.name.lower() != "bin" or ctest.parent != cmake.parent):
        raise ValueError("CMake and CTest must come from the same installed bin directory")
    installed = cmake.parent.parent
    if installed.is_relative_to(owned_root) or owned_root.is_relative_to(installed):
        raise ValueError("The installed and private CMake runtime roots overlap")

    private = owned_root / "cmake-runtime"
    private.mkdir(exist_ok=False)
    source = _inventory(installed)
    if not any(Path(name).match("share/cmake-*/Modules/CMake.cmake") for name in source):
        raise ValueError(f"Missing CMake runtime modules in {installed}")
    for name, digest in sorted(source.items()):
        target = private / name
        if digest is None:
            target.mkdir()
        else:
            shutil.copyfile(installed / name, target)

    copied = _inventory(private)
    if copied != source:
        raise ValueError(f"CMake runtime content identity mismatch: {private}")
    manifest = json.dumps(copied, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "root": str(private), "bin": str(private / "bin"), "source_root": str(installed),
        "tree_sha256": hashlib.sha256(manifest).hexdigest(),
        "file_count": sum(value is not None for value in copied.values()),
        "cmake_sha256": copied[f"bin/{cmake.name}"],
        "ctest_sha256": copied[f"bin/{ctest.name}"],
    }
