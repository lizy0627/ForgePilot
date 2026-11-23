"""Private, fixed Git operations; no command or commit tool is exposed to Agents."""

from contextlib import ExitStack
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import threading

from .errors import ForgePilotError
from .path_identity import path_within, same_path
from .process import ProcessResult, executable as resolve_executable, run_process, safe_environment
from .repository import is_link, opened_path


def _git_prefix(executable, root, git_dir=None, *, checkout_eol=False):
    prefix = [executable, "--no-optional-locks", "--no-pager", "-C", str(root),
              "--work-tree=" + str(root)]
    if git_dir is not None:
        prefix += ["--git-dir=" + str(git_dir)]
    for value in ("core.fsmonitor=false", "core.untrackedCache=false",
                  "core.hooksPath=" + os.devnull, "core.attributesFile=" + os.devnull,
                  "commit.gpgSign=false", "tag.gpgSign=false", "gpg.program=false",
                  "gpg.ssh.program=false", "credential.helper=", "credential.interactive=false",
                  "protocol.allow=never", "submodule.recurse=false", "core.sshCommand=false",
                  "gc.auto=0", "maintenance.auto=false", "fetch.writeCommitGraph=false",
                  "merge.renormalize=false", "core.alternateRefsCommand=false",
                  "core.editor=false", "sequence.editor=false", "extensions.worktreeConfig=false",
                  "merge.union.driver=false", "merge.union.recursive=binary", "core.protectNTFS=true"):
        prefix += ["-c", value]
    if not checkout_eol:
        prefix += ["-c", "core.autocrlf=false"]
    return prefix


# Git's POSIX regex and Python both need literal CR/LF in the character class.
_DRIVERS = ("^(filter[.][^\r\n=]+[.](clean|smudge|process|required)"
            "|merge[.][^\r\n=]+[.](driver|recursive)|diff[.][^\r\n=]+[.](command|textconv))$")


def _driver_overrides(result):
    if result.truncated or result.returncode not in (0, 1):
        raise ForgePilotError("git_config", "Cannot inspect Git driver configuration safely")
    options = []
    try:
        keys = result.stdout.decode("utf-8", errors="strict").splitlines()
    except UnicodeError:
        raise ForgePilotError("git_config", "Unsupported Git driver key") from None
    for key in keys:
        if not re.fullmatch(_DRIVERS, key):
            raise ForgePilotError("git_config", "Unsupported Git driver key")
        value = ""
        if key.startswith("merge."):
            value = "binary" if key.endswith(".recursive") else "false"
        elif key.startswith("diff."):
            value = "false"
        elif key.endswith(".required"):
            value = "false"
        options += ["-c", key + "=" + value]
    return options


def _run_sync(argv, cwd, *, data=b"", env=None, timeout=30, output_limit=16 * 1024 * 1024,
              read_checkout_config=False):
    """Bounded shell-free pipe runner; drain both outputs and kill on overflow."""
    environment = safe_environment()
    if read_checkout_config:
        # Only the read-only, no-includes EOL config query sees user/system files.
        del environment["GIT_CONFIG_NOSYSTEM"], environment["GIT_CONFIG_GLOBAL"]
        for key in ("HOME", "XDG_CONFIG_HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH"):
            if key in os.environ:
                environment[key] = os.environ[key]
    environment.update(env or {})
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=environment, shell=False,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        raise ForgePilotError("tool_process", "Unable to run configured Git executable") from None
    overflow = threading.Event()
    outputs = [bytearray(), bytearray()]

    def read(stream, destination):
        while chunk := stream.read(16_384):
            remaining = output_limit - len(destination)
            destination.extend(chunk[:remaining])
            if len(chunk) > remaining:
                overflow.set()
                try:
                    proc.kill()
                except OSError:
                    pass
                break

    def write():
        try:
            proc.stdin.write(data)
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            proc.stdin.close()

    threads = [threading.Thread(target=read, args=(proc.stdout, outputs[0]), daemon=True),
               threading.Thread(target=read, args=(proc.stderr, outputs[1]), daemon=True),
               threading.Thread(target=write, daemon=True)]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        proc.wait()
    finally:
        for thread in threads:
            thread.join(timeout=2)
        proc.stdout.close()
        proc.stderr.close()
    if timed_out:
        raise ForgePilotError("tool_timeout", "Git operation timed out")
    return ProcessResult(proc.returncode, bytes(outputs[0]), bytes(outputs[1]), overflow.is_set())


def _checkout_eol_options(prefix, root):
    result = _run_sync(prefix + ["config", "--no-includes", "--null", "--get-regexp",
                                 r"^core\.(autocrlf|eol)$"], root,
                       read_checkout_config=True, output_limit=32_768)
    if result.truncated or result.returncode not in (0, 1):
        raise ForgePilotError("git_config", "Cannot read target Git checkout settings")
    options = []
    for record in result.stdout.split(b"\0"):
        if record:
            key, separator, value = record.partition(b"\n")
            if key not in (b"core.autocrlf", b"core.eol"):
                raise ForgePilotError("git_config", "Unexpected Git checkout setting")
            # A boolean key without a value is distinct from an explicitly empty value.
            option = key + b"=" + value if separator else key
            options += ["-c", os.fsdecode(option)]
    return options


class SafeGit:
    """Synchronous manager-only Git, with pinned roots and local-only execution.

    Callers supply fixed arrays constructed by WorkspaceManager, never model text.
    Root and Git administration directories are revalidated before and after each
    invocation. Worktree pointers must match Git's normal .git/worktrees layout.
    """
    def __init__(self, root, executable=None):
        from .workspace_safety import Guard, _identity
        self.guard = Guard(root)
        self.root = self.guard.root
        self.executable = resolve_executable(executable, "git")
        marker = self.root / ".git"
        try:
            self.marker_identity = _identity(marker)
            self.marker_data = None
            if self.marker_identity[2] == stat.S_IFDIR:
                self.git_dir = self.common_dir = marker
            elif self.marker_identity[2] == stat.S_IFREG:
                self.marker_data = self.guard.read_marker()
                text = self.marker_data.decode("utf-8").strip()
                if not text.startswith("gitdir: ") or "\n" in text:
                    raise ValueError()
                self.git_dir = Path(text[8:])
                if not self.git_dir.is_absolute():
                    raise ValueError()
                self.git_guard = Guard(self.git_dir)
                common = self.git_guard.read("commondir", max_bytes=16_384).decode("utf-8").strip()
                # Git's own worktree layout has this one fixed relative pointer.
                if common.replace("\\", "/") != "../..":
                    raise ValueError()
                self.common_dir = self.git_dir.parent.parent
                if self.git_dir.parent.name != "worktrees" or self.common_dir.name != ".git":
                    raise ValueError()
                self.backpointer = self.git_guard.read("gitdir", max_bytes=16_384)
                from .workspace_safety import _same_path
                if not _same_path(Path(self.backpointer.decode("utf-8").strip()), marker):
                    raise ValueError()
            else:
                raise ValueError()
            self.git_guard = Guard(self.git_dir)
            self.common_guard = self.git_guard if self.common_dir == self.git_dir else Guard(self.common_dir)
            self._cached_signature = None
            self._cached_overrides = None
            self._check()
        except (OSError, ValueError, UnicodeError, RuntimeError):
            raise ForgePilotError("workspace_boundary", "Cannot verify Git administration directories") from None

    def _check(self):
        from .workspace_safety import _identity
        self.guard.check()
        self.git_guard.check()
        if self.common_guard is not self.git_guard:
            self.common_guard.check()
        if _identity(self.root / ".git") != self.marker_identity:
            raise ForgePilotError("workspace_boundary", "Git administration marker was replaced")
        if self.marker_data is not None:
            if (self.guard.read_marker() != self.marker_data
                    or self.git_guard.read("gitdir", max_bytes=16_384) != self.backpointer
                    or self.git_guard.read("commondir", max_bytes=16_384).strip().replace(b"\\", b"/") != b"../.."):
                raise ForgePilotError("workspace_boundary", "Git worktree pointer changed")
        signature = []
        for guard in dict.fromkeys((self.common_guard, self.git_guard)):
            for filename in ("config", "config.worktree"):
                path = guard.path(filename, missing=True)
                try:
                    _identity(path)
                except FileNotFoundError:
                    continue
                else:
                    if filename == "config.worktree":
                        # -c extensions.worktreeConfig=false is applied after
                        # Git loads worktree config on supported Git versions.
                        # A missing file could otherwise be introduced after
                        # inspection, so this optional feature is unsupported.
                        raise ForgePilotError("git_config", "Git worktree configuration is outside the controlled boundary")
                    config = guard.read(filename, max_bytes=1_048_576)
                    signature.append((str(path), hashlib.sha256(config).digest()))
                    # Do this before invoking Git: even read-only config lookup
                    # can follow an include.path to a UNC/network location.
                    if re.search(rb"(?im)^\s*\[\s*include(?:if)?(?:\s|\]|\")", config.removeprefix(b"\xef\xbb\xbf")):
                        raise ForgePilotError("git_config", "Git configuration includes are outside the controlled boundary")
                    if re.search(rb"(?im)(?:^|\])[ \t]*worktreeconfig(?:[ \t]*[=#;]|[ \t]*$)",
                                 config.removeprefix(b"\xef\xbb\xbf")):
                        raise ForgePilotError("git_config", "Git worktree configuration is outside the controlled boundary")
        self._config_signature = tuple(signature)
        for name in ("alternates", "http-alternates"):
            path = self.common_guard.path("objects/info/" + name, missing=True)
            try:
                _identity(path)
            except FileNotFoundError:
                continue
            raise ForgePilotError("git_config", "External Git object alternates are outside the controlled boundary")
        grafts = self.common_guard.path("info/grafts", missing=True)
        try:
            _identity(grafts)
        except FileNotFoundError:
            pass
        else:
            raise ForgePilotError("git_config", "Git grafts are outside the controlled object boundary")

    def _administration_directories(self):
        from .repository import is_link
        directories = []
        pending = [self.common_dir]
        count = 0
        while pending:
            directory = pending.pop()
            directories.append(directory)
            with os.scandir(directory) as listing:
                for item in listing:
                    count += 1
                    if count > 200_000:
                        raise ForgePilotError("git_output_limit", "Git metadata scan limit exceeded")
                    info = item.stat(follow_symlinks=False)
                    if is_link(info) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                        raise ForgePilotError("workspace_boundary", "Git metadata contains a link or special file")
                    if (item.name.endswith(".lock") or item.name in {
                            "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-apply", "rebase-merge", "sequencer"}):
                        raise ForgePilotError("workspace_git_busy", "Git has an existing lock or unresolved operation")
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(Path(item.path))
        return directories

    def run(self, args: list[str], *, data=b"", allowed=(0,), env=None,
            checkout_eol=False, checkout_root=None) -> ProcessResult:
        from .workspace_safety import Guard, _identity, _open_nofollow, _same_path, _windows_directory_handles
        if (not isinstance(args, list) or not args or not all(isinstance(v, str) and "\0" not in v for v in args)
                or not isinstance(data, bytes)):
            raise ValueError("Expected fixed Git arguments and byte input")
        command = args[0]
        if command not in {"rev-parse", "symbolic-ref", "status", "diff", "config", "show", "ls-files", "ls-tree",
                           "worktree", "branch", "checkout", "checkout-index", "switch", "read-tree", "write-tree", "commit-tree",
                           "update-ref", "add", "apply", "reset", "merge-tree", "merge-base", "cat-file", "log",
                           "format-patch", "check-ref-format", "for-each-ref", "rev-list", "diff-tree", "hash-object"}:
            raise ForgePilotError("git_denied", "Git command is outside manager operations")
        if command == "config" and not any(value in args for value in ("--get", "--get-regexp", "--name-only")):
            raise ForgePilotError("git_denied", "Git configuration mutation is forbidden")
        if command == "checkout-index" and args != ["checkout-index", "--index", "--ignore-skip-worktree-bits", "-z", "--stdin"]:
            raise ForgePilotError("git_denied", "Expected checkout of missing index paths")
        environment = {}
        index_guard = None
        for key, value in (env or {}).items():
            if key == "GIT_INDEX_FILE":
                path = Path(value)
                if not path.is_absolute() or any(same_path(path, index) for index in (self.git_dir / "index", self.common_dir / "index")):
                    raise ForgePilotError("git_denied", "Private Git index must be outside source indexes")
                index_guard = Guard(path.parent)
                index_guard.path(path.name, missing=True)
            elif key not in {"GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
                             "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE"}:
                raise ForgePilotError("git_denied", "Git environment override is forbidden")
            if not isinstance(value, str) or "\0" in value or "\n" in value:
                raise ForgePilotError("git_denied", "Invalid Git environment override")
            environment[key] = value
        with ExitStack() as stack:
            if checkout_root is not None:
                if (len(args) != 4 or args[:3] != ["read-tree", "--reset", "-u"]
                        or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", args[3])
                        or not checkout_eol or index_guard is None):
                    raise ForgePilotError("git_denied", "Expected a private index and checkout tree")
                checkout_guard = Guard(checkout_root)
                if not same_path(checkout_guard.root.parent, index_guard.root) or path_within(checkout_guard.root, self.root):
                    raise ForgePilotError("git_denied", "Private checkout must use the private index directory")
            stack.enter_context(self.guard.protect())
            stack.enter_context(self.git_guard.protect())
            if self.common_guard is not self.git_guard:
                stack.enter_context(self.common_guard.protect())
            if checkout_root is not None:
                stack.enter_context(checkout_guard.protect_tree())
            elif command in {"add", "apply", "reset", "checkout", "checkout-index", "switch"} or (command == "read-tree" and "-u" in args):
                stack.enter_context(self.guard.protect_tree())
            if index_guard is not None:
                stack.enter_context(index_guard.protect(path.name, missing=True))
            # Pin Git's configuration leaf while both config inspection and the
            # command execute. Windows denies competing writers/replacement;
            # the bytes also key the per-instance neutralized-driver cache.
            config_path = self.common_guard.path("config")
            config_before = _identity(config_path)
            config_fd = _open_nofollow(config_path, write_shared=False)
            stack.callback(os.close, config_fd)
            info = os.fstat(config_fd)
            actual = opened_path(config_fd)
            if (is_link(info) or not stat.S_ISREG(info.st_mode)
                    or (info.st_dev, info.st_ino) != config_before[:2]
                    or actual is None or not _same_path(actual, config_path)):
                raise ForgePilotError("workspace_boundary", "Git configuration leaf changed")
            self._check()
            directories = self._administration_directories()
            if command == "worktree" and len(args) > 1 and args[1] == "remove":
                # Git must remove the verified task administration directory.
                # Holding that directory without SHARE_DELETE would forbid its
                # own removal. The manager separately pins the controlled parent.
                worktrees = self.common_dir / "worktrees"
                directories = [item for item in directories if not path_within(item, worktrees)]
            identities = [(path, _identity(path)) for path in directories]
            stack.enter_context(_windows_directory_handles(directories))
            if any(_identity(path) != identity for path, identity in identities):
                raise ForgePilotError("workspace_boundary", "Git metadata changed before execution")
            prefix = _git_prefix(self.executable, self.root, self.git_dir, checkout_eol=checkout_eol)
            if self._cached_signature != self._config_signature:
                signature = self._config_signature
                config = _run_sync(prefix + ["config", "--name-only", "--get-regexp", _DRIVERS], self.root,
                                   env=environment, output_limit=32_768)
                overrides = _driver_overrides(config)
                self._check()
                if self._config_signature != signature:
                    raise ForgePilotError("git_config", "Git configuration changed during driver inspection")
                self._cached_overrides = overrides
                self._cached_signature = signature
            prefix += self._cached_overrides
            if checkout_eol:
                prefix += _checkout_eol_options(prefix, self.root)
            if checkout_root is not None:
                prefix += ["--work-tree=" + str(checkout_guard.root)]
            self._check()
            result = _run_sync(prefix + args, self.root, data=data, env=environment)
            self._check()
            self._administration_directories()
            if result.truncated:
                raise ForgePilotError("git_output_limit", "Git output exceeds the manager byte limit")
            if result.returncode not in allowed:
                raise ForgePilotError("git_failed", "Manager Git operation failed")
            return result


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
        prefix = _git_prefix(self.executable, self.repository.root)
        # Even ordinary git diff may run a repository-configured clean/process filter.
        # Discover key names only, then neutralize all filter drivers for this invocation.
        config = await run_process(prefix + ["config", "--name-only", "--get-regexp", _DRIVERS],
                                   self.repository.root, output_limit=32_768)
        prefix += _driver_overrides(config)
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
