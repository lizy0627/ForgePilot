"""Probe both original pytest symlink operations on the test volume."""
import errno
import os
from pathlib import Path
import stat
import tempfile


def probe_symlinks(owned_root: Path) -> dict[str, dict]:
    """Use a fresh directory inside the caller's private test root."""
    root = owned_root.resolve(strict=True)
    work = Path(tempfile.mkdtemp(prefix="symlink-probe-", dir=root))
    results = {}
    try:
        for kind in ("file", "directory"):
            directory = kind == "directory"
            target = work / f"{kind}-target"
            link = work / f"{kind}-link"
            if directory:
                target.mkdir()
            else:
                target.touch()
            result = {"path": str(link), "target": str(target)}
            try:
                link.symlink_to(target, target_is_directory=directory)
            except OSError as exc:
                winerror = getattr(exc, "winerror", None)
                denied = winerror in (5, 1314) if winerror is not None else exc.errno in (errno.EACCES, errno.EPERM)
                if not denied:
                    raise
                result.update(status="permission_denied", errno=exc.errno, winerror=winerror, reason=str(exc))
            else:
                info = link.lstat()
                if (not stat.S_ISLNK(info.st_mode)
                        or (os.name == "nt" and info.st_reparse_tag != stat.IO_REPARSE_TAG_SYMLINK)
                        or link.resolve(strict=True) != target
                        or link.is_dir() != directory):
                    raise RuntimeError(f"Expected a {kind} symbolic link to {target}: {link}")
                result.update(status="available", reparse_tag=getattr(info, "st_reparse_tag", None))
            results[kind] = result
        return results
    finally:
        if work.resolve(strict=True) != work or work.parent != root:
            raise RuntimeError(f"Unexpected symlink probe directory during cleanup: {work}")
        # All four entries are direct children. Remove only these known paths.
        for name in ("file-link", "directory-link", "file-target", "directory-target"):
            path = work / name
            if path.resolve(strict=False).parent != work:
                raise RuntimeError(f"Symlink probe cleanup would leave its directory: {path}")
            if path.is_dir() and not path.is_symlink():
                path.rmdir()
            else:
                path.unlink(missing_ok=True)
        work.rmdir()
