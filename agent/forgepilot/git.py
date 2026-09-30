"""Fixed read-only Git operations. Baseline is separate from the task's byte snapshots."""

from dataclasses import dataclass
import json
import os
import re

from .errors import ForgePilotError
from .process import run_process


@dataclass(frozen=True)
class GitBaseline:
    head: str | None
    head_state: str
    status: bytes
    diff: bytes
    cached_diff: bytes


class GitRepository:
    def __init__(self, repository, executable):
        self.repository = repository
        self.executable = executable
        self.baseline: GitBaseline | None = None

    async def _run(self, args, *, allowed=(0,)):
        self.repository._path(".")
        prefix = [self.executable, "--no-optional-locks", "--no-pager", "-C", str(self.repository.root),
                  "--work-tree=" + str(self.repository.root),
                  "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
                  "-c", "core.hooksPath=" + os.devnull, "-c", "core.attributesFile=" + os.devnull]
        # Even ordinary git diff may run a repository-configured clean/process filter.
        # Discover key names only, then neutralize all filter drivers for this invocation.
        config = await run_process(prefix + ["config", "--name-only", "--get-regexp", r"^filter\..*\.(clean|smudge|process|required)$"],
                                   self.repository.root, output_limit=32_768)
        if config.truncated or config.returncode not in (0, 1):
            raise ForgePilotError("git_config", "Cannot inspect Git filter configuration safely")
        for key in config.stdout.decode("utf-8", errors="strict").splitlines():
            if not re.fullmatch(r"filter\.[^\r\n=]+\.(clean|smudge|process|required)", key):
                raise ForgePilotError("git_config", "Unsupported Git filter key")
            prefix += ["-c", key + ("=false" if key.endswith(".required") else "=")]
        result = await run_process(prefix + args, self.repository.root, output_limit=1_048_576)
        if result.truncated:
            raise ForgePilotError("git_output_limit", "Git baseline/output exceeds 1 MiB; operation stopped")
        if result.returncode not in allowed:
            raise ForgePilotError("git_failed", "Read-only Git operation failed")
        return result

    async def capture(self) -> GitBaseline:
        head = await self._run(["rev-parse", "--verify", "--quiet", "HEAD"], allowed=(0, 1))
        if head.returncode:
            symbolic = await self._run(["symbolic-ref", "-q", "HEAD"], allowed=(0, 1))
            if symbolic.returncode or not symbolic.stdout.startswith(b"refs/heads/"):
                raise ForgePilotError("git_head", "HEAD cannot be resolved and is not an unborn branch")
        status = await self._run(["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=all"])
        options = ["--no-ext-diff", "--no-textconv", "--no-color", "--ignore-submodules=all"]
        diff = await self._run(["diff", *options])
        cached = await self._run(["diff", "--cached", *options])
        self.baseline = GitBaseline(head.stdout.decode().strip() if head.returncode == 0 else None,
                                    "present" if head.returncode == 0 else "unborn",
                                    status.stdout, diff.stdout, cached.stdout)
        return self.baseline

    async def status(self) -> str:
        result = await self._run(["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=all"])
        fields = iter(result.stdout.decode("utf-8", errors="replace").split("\0"))
        entries, omitted = [], 0
        for row in fields:
            if not row:
                continue
            paths = [row[3:]]
            if "R" in row[:2] or "C" in row[:2]:
                paths.append(next(fields, ""))
            try:
                paths = [self.repository.normalize_plan_path(p) for p in paths]
            except ForgePilotError:
                omitted += 1
                continue
            if len(entries) >= 100 or len(json.dumps(entries, ensure_ascii=False)) > 10_000:
                omitted += 1
                continue
            entries.append({"status": row[:2], "path": paths[0],
                            **({"original_path": paths[1]} if len(paths) == 2 else {})})
        return json.dumps({"entries": entries, "omitted": omitted, "truncated": bool(omitted),
                           "note": "Current Git status includes user changes; task diff is separate"}, ensure_ascii=False)
