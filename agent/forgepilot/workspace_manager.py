"""Deterministic local worktrees, writer ownership and durable Git checkpoints.

This service is deliberately absent from the model tool registry. Its approval
flags and snapshots are supplied by the application after a human decision.
"""
from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import threading
from typing import TypedDict
from uuid import uuid4

from .errors import ForgePilotError
from .path_identity import path_key, path_within, same_path
from .git import SafeGit
from .process import executable
from .repository import Repository, excluded
from .orchestration.workspace import WorkspaceSnapshot
from .workspace_safety import (
    Guard, WriterLease, manifest, validate_relative, remove_worktree_files, remove_worktree_registration,
    _exists,
)
from .workspace_delivery import DeliveryMixin
from .policy import Capability, PolicyEngine, Principal, Role


AUTHOR = "ForgePilot"
EMAIL = "forgepilot@localhost"
MESSAGES = {
    "plan_approved": "forgepilot(checkpoint): approved execution plan",
    "implementation": "forgepilot(checkpoint): implementation",
    "validated": "forgepilot(checkpoint): validated result",
}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_HASH = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def _id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ForgePilotError("workspace_identity", "Use a simple task identifier")
    validate_relative(value)
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _identity(path):
    info = Path(path).lstat()
    return [info.st_dev, info.st_ino]


class SourceBaselineView(TypedDict):
    source_head: str
    files: dict[str, bytes | None]
    file_hashes: dict[str, str | None]
    changed_files: list[str]


class WorkspaceManager(DeliveryMixin):
    def __init__(self, data_root=None, *, git_executable=None, artifacts=None,
                 principal=None, policy=None):
        root = data_root or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ForgePilot" / "data"
        self.guard = Guard(root, create=True)
        self.root = self.guard.root
        self.git_executable = executable(git_executable, "git")
        self.artifacts = artifacts
        self.principal = principal or Principal(Role.WORKSPACE_MANAGER)
        self.policy = policy or PolicyEngine()
        self._local = threading.local()
        self.guard.mkdir("tasks")
        self.guard.mkdir("worktrees")
        self.guard.mkdir("artifacts")
        self.guard.mkdir("leases")

    def _state_path(self, task_id):
        return f"tasks/{_id(task_id)}.json"

    def _load(self, task_id):
        try:
            state = json.loads(self.guard.read(self._state_path(task_id)))
            if state["task_id"] != task_id or state["version"] != 1:
                raise ValueError()
            _id(state["repository_id"])
            expected = self.root / "worktrees" / state["repository_id"] / task_id
            if not same_path(Path(state["worktree_path"]), expected):
                raise ValueError()
            if path_within(Path(state["repository_path"]), self.root):
                raise ValueError()
            return state
        except (ValueError, KeyError, TypeError):
            raise ForgePilotError("workspace_corrupted", "Invalid durable workspace identity") from None

    def _save(self, state):
        self.guard.write(self._state_path(state["task_id"]), _json(state))

    def record(self, task_id):
        """Return a copy suitable for MissionRepository.save_workspace."""
        state = self._load(task_id)
        return {k: state[k] for k in ("workspace_id", "task_id", "repository_path", "worktree_path", "base_ref", "status")} | {
            "head_commit": state["expected_head"]}

    def _git(self, state, main=False):
        self.guard.check()
        root = Path(state["repository_path"] if main else state["worktree_path"])
        key = "repository_identity" if main else "worktree_identity"
        if key in state and _identity(root) != state[key]:
            raise ForgePilotError("workspace_boundary", "Workspace directory was replaced")
        operation = getattr(self._local, "operation", None)
        cache = operation[2] if operation is not None and operation[0] is state else {}
        git = cache.get(str(root))
        if git is None:
            git = SafeGit(root, self.git_executable)
            cache[str(root)] = git
        common = git.common_dir
        if not same_path(common, state["common_dir"]) or _identity(common) != state["common_identity"]:
            raise ForgePilotError("workspace_boundary", "Worktree belongs to a different repository")
        admin_key = "main_admin" if main else "task_admin"
        if admin_key in state and (str(git.git_dir) != state[admin_key]["path"]
                                   or _identity(git.git_dir) != state[admin_key]["identity"]):
            raise ForgePilotError("workspace_boundary", "Git worktree administration directory was replaced")
        if not main and "marker_hash" in state:
            marker = Guard(root).read_marker()
            if sha256(marker).hexdigest() != state["marker_hash"]:
                raise ForgePilotError("workspace_boundary", "Worktree Git marker changed")
        return git

    def _check_record(self, record):
        state = self._load(record["task_id"])
        for name in ("workspace_id", "task_id", "repository_path", "worktree_path", "base_ref"):
            if record.get(name) != state[name]:
                raise ForgePilotError("workspace_corrupted", "Worktree record does not match its owner")
        return state

    @contextmanager
    def _operation(self, task_id, *, recover=False):
        operation = getattr(self._local, "operation", None)
        if operation is not None and operation[0]["task_id"] == task_id:
            operation[1].check()
            yield operation[0]
            return
        state = self._load(task_id)
        with self.guard.protect(), WriterLease(self.root, state["repository_id"], task_id, recover=recover) as lease:
            # Reload after ownership: a different process may have published state.
            state = self._load(task_id)
            if state["status"] in {"abandoned", "removed", "creating"}:
                raise ForgePilotError("workspace_state", "Worktree is not active")
            previous = operation
            self._local.operation = (state, lease, {})
            try:
                yield state
                lease.check()
            finally:
                self._local.operation = previous

    @staticmethod
    def _head(git):
        value = git.run(["rev-parse", "--verify", "HEAD"]).stdout.decode().strip()
        if not _HASH.fullmatch(value):
            raise ForgePilotError("workspace_head", "Worktree requires a committed HEAD")
        return value

    def _snapshot(self, state):
        git = self._git(state)
        before = self._head(git)
        if git.run(["ls-files", "--unmerged", "-z"]).stdout:
            raise ForgePilotError("workspace_conflict", "Unresolved conflicts require user action")
        branch = git.run(["symbolic-ref", "-q", "--short", "HEAD"], allowed=(0, 1)).stdout.decode().strip() or None
        if branch != state["branch"]:
            raise ForgePilotError("workspace_head", "Task branch changed outside WorkspaceManager")
        files = manifest(Path(state["worktree_path"]))
        if self._head(git) != before:
            raise ForgePilotError("workspace_head", "HEAD moved while inspecting worktree")
        return WorkspaceSnapshot(state["workspace_id"], state["task_id"], before, files)

    def inspect_task(self, task_id):
        # Inspection must remain possible after a crashed OS lease. It cannot
        # release/adopt the stale owner, publish state or grant write authority.
        operation = getattr(self._local, "operation", None)
        state = operation[0] if operation is not None and operation[0]["task_id"] == task_id else self._load(task_id)
        return self._snapshot(state)

    def inspect(self, record):
        self._check_record(record)
        return self.inspect_task(record["task_id"])

    @staticmethod
    def _index_hash(state):
        admin = state["task_admin"]
        guard = Guard(admin["path"])
        if _identity(guard.root) != admin["identity"]:
            raise ForgePilotError("workspace_boundary", "Git index administration directory changed")
        return sha256(guard.read("index")).hexdigest()

    @staticmethod
    def _set_expected(state, snapshot):
        state["expected_head"] = snapshot.head_commit
        state["expected_files"] = dict(snapshot.file_hashes)
        state["expected_index"] = WorkspaceManager._index_hash(state)
        if "initial_files" in state:
            state["owned_files"] = sorted(set(state.get("owned_files", ())) |
                                           (set(snapshot.file_hashes) - set(state["initial_files"])))

    def _assert_expected(self, state, expected):
        current = self._snapshot(state)
        if current != expected or current.head_commit != state["expected_head"]:
            raise ForgePilotError("workspace_changed", "Worktree HEAD or snapshot changed; user inspection required")
        if self._index_hash(state) != state.get("expected_index"):
            raise ForgePilotError("workspace_changed", "Git index changed outside WorkspaceManager; user inspection required")
        if dict(current.file_hashes) != state["expected_files"]:
            active = getattr(self._local, "active", None)
            if active is None or active[0] is not state:
                raise ForgePilotError("workspace_changed", "Files changed outside the writer lease")
            self._check_scope(state, active[1], current)
        return current

    @staticmethod
    def _check_scope(state, before, after):
        allowed = state.get("approved_paths")
        changed = {p for p in set(before.file_hashes) | set(after.file_hashes)
                   if before.file_hashes.get(p) != after.file_hashes.get(p)}
        if allowed is not None and changed - set(allowed):
            raise ForgePilotError("workspace_scope", "Writer changed files outside the approved plan")
        for path in changed:
            Repository(Path(state["worktree_path"]), "").normalize_plan_path(path)

    def create(self, repository_path, task_id, *, base_ref="HEAD", dirty_strategy="head"):
        task_id = _id(task_id)
        if dirty_strategy == "cancel":
            return None
        if dirty_strategy not in {"head", "copy_diff"}:
            raise ForgePilotError("workspace_strategy", "Choose HEAD, approved selected diff, or cancel")
        if (not isinstance(base_ref, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}", base_ref)
                or ".." in base_ref or "@{" in base_ref):
            raise ForgePilotError("workspace_base", "Select a local committed ref")
        main_guard = Guard(repository_path)
        main = main_guard.root
        if path_within(main, self.root) or path_within(self.root, main):
            raise ForgePilotError("workspace_boundary", "Data directory and source repository must be separate")
        git = SafeGit(main, self.git_executable)
        common = git.common_dir
        # Retain native separators in durable IDs so existing ASCII/Chinese
        # repositories keep their lease namespace after the Unicode key fix.
        repository_id = sha256(path_key(str(common)).replace("/", os.sep).encode()).hexdigest()[:24]
        with WriterLease(self.root, repository_id, task_id):
            if self.guard.path(self._state_path(task_id), missing=True).exists():
                raise ForgePilotError("workspace_exists", "Task already has a workspace")
            commit = git.run(["rev-parse", "--verify", "--end-of-options", base_ref + "^{commit}"]).stdout.decode().strip()
            if not _HASH.fullmatch(commit):
                raise ForgePilotError("workspace_base", "Base ref must resolve to a commit")
            tree_files = self._tree_bytes(git, commit)
            source_head = self._head(git)
            branch = git.run(["symbolic-ref", "-q", "--short", "HEAD"], allowed=(0, 1)).stdout.decode().strip() or None
            self.guard.mkdir(f"worktrees/{repository_id}")
            relative = f"worktrees/{repository_id}/{task_id}"
            destination = self.guard.path(relative, missing=True)
            if destination.exists():
                raise ForgePilotError("workspace_exists", "Controlled worktree path is already occupied")
            state = dict(version=1, workspace_id="wt-" + uuid4().hex, task_id=task_id,
                         repository_id=repository_id, repository_path=str(main), worktree_path=str(destination),
                         repository_identity=_identity(main), common_dir=str(common), common_identity=_identity(common),
                         main_admin={"path": str(git.git_dir), "identity": _identity(git.git_dir)},
                         base_ref=commit, selected_base_ref=base_ref, branch=None, status="creating",
                         source_head=source_head, target_branch=branch, target_commit=source_head,
                         dirty_strategy=dirty_strategy, validated_commit=None, validated_files=None,
                         checkpoints={}, artifacts={}, expected_head=commit, expected_files={}, approved_paths=None)
            self._save(state)  # A failed/uncertain add is never blindly retried or recursively deleted.
            with self.guard.protect(), main_guard.protect():
                self.guard.mkdir(relative)
                worktree_guard = Guard(destination)
                with worktree_guard.protect():
                    git.run(["worktree", "add", "--detach", "--no-checkout", "--", str(destination), commit])
                    # Construct files through verified handles; Git never checks
                    # out into a not-yet-verified directory hierarchy.
                    for name in tree_files:
                        parent = Path(name).parent.as_posix()
                        if parent != ".":
                            worktree_guard.mkdir(parent)
                    with worktree_guard.protect_tree():
                        for name, data in tree_files.items():
                            worktree_guard.write(name, data, overwrite=False)
                        task_git = SafeGit(destination, self.git_executable)
                        task_git.run(["read-tree", commit])
                        # read-tree seeds the index without checkout stat data.
                        # Refresh only this task index so apply --index can prove
                        # its worktree preimage, without staging user changes.
                        task_git.run(["add", "--refresh", "--", "."])
            self.guard.check()
            state["worktree_identity"] = _identity(destination)
            state["marker_hash"] = sha256(Guard(destination).read_marker()).hexdigest()
            task_git = SafeGit(destination, self.git_executable)
            state["task_admin"] = {"path": str(task_git.git_dir), "identity": _identity(task_git.git_dir)}
            state["status"] = "active"
            initial = self._snapshot(state)
            if dict(initial.file_hashes) != {name: sha256(data).hexdigest() for name, data in tree_files.items()}:
                raise ForgePilotError("workspace_changed", "Worktree files changed during isolated creation")
            self._set_expected(state, initial)
            state["initial_files"] = dict(state["expected_files"])
            state["owned_files"] = []
            self._save(state)
            return dict(state)

    def _branch(self, state):
        if state["branch"] is not None:
            return
        # Digest avoids unsafe/ambiguous user-controlled branch syntax and short-ID collisions.
        branch = "forgepilot/task-" + sha256(state["task_id"].encode()).hexdigest()[:12]
        git = self._git(state)
        git.run(["switch", "--no-guess", "-c", branch])
        state["branch"] = branch

    def approve_plan(self, task_id, *, approved, expected, approved_paths=None):
        if approved is not True:
            raise ForgePilotError("workspace_approval", "Plan approval is required to create a branch")
        with self._operation(task_id) as state:
            self._assert_expected(state, expected)
            if state["dirty_strategy"] == "copy_diff" and not state.get("baseline", {}).get("applied"):
                raise ForgePilotError("baseline_approval", "Approve and apply the selected Baseline Patch before plan approval")
            if approved_paths is not None:
                repo = Repository(Path(state["worktree_path"]), "")
                state["approved_paths"] = sorted({repo.normalize_plan_path(p) for p in approved_paths})
            self._branch(state)
            after = self._snapshot(state)
            if after != expected:
                # Branch creation changes no approved bytes or commit. Preserve
                # unknown concurrent data rather than adopting it as task-owned.
                self._save(state)
                raise ForgePilotError("workspace_changed", "Worktree changed during plan approval")
            self._set_expected(state, after)
            self._save(state)
            return self._snapshot(state)

    @contextmanager
    def writer(self, task_id, *, expected):
        with self._operation(task_id) as state:
            before = self._assert_expected(state, expected)
            if state["branch"] is None or state["status"] != "active" or state.get("intent"):
                raise ForgePilotError("workspace_approval", "Approved plan and resolved workspace state required")
            state["writer_before"] = {"head": before.head_commit, "files": dict(before.file_hashes)}
            self._save(state)
            self._local.active = (state, before)
            try:
                yield Path(state["worktree_path"])
                current = self._snapshot(state)
                if current.head_commit != state["expected_head"]:
                    raise ForgePilotError("workspace_head", "Writer may not create commits or move HEAD")
                if self._index_hash(state) != state["expected_index"]:
                    raise ForgePilotError("workspace_changed", "Writer may not change the Git index")
                self._check_scope(state, before, current)
                accepted_index = state["expected_index"]
                self._set_expected(state, current)
                state["expected_index"] = accepted_index
                state["validated_commit"] = None
                state["validated_files"] = None
                state.pop("writer_before", None)
                self._save(state)
            except BaseException:
                state["status"] = "interrupted_writer"
                try:
                    interrupted = self._snapshot(state)
                    state["writer_observed"] = {"head": interrupted.head_commit, "files": dict(interrupted.file_hashes)}
                except (ForgePilotError, OSError, ValueError):
                    state["writer_observed"] = None
                self._save(state)
                raise
            finally:
                self._local.active = None

    def _artifact(self, state, name, data):
        name = validate_relative(name)
        digest = sha256(data).hexdigest()
        relative = f"artifacts/{state['repository_id']}/{state['task_id']}/{digest}-{name}"
        self.guard.mkdir(f"artifacts/{state['repository_id']}/{state['task_id']}")
        path = self.guard.path(relative, missing=True)
        if _exists(path):
            if self.guard.read(relative, max_bytes=len(data) + 1) != data:
                raise ForgePilotError("artifact_integrity", "Artifact contents changed")
        else:
            self.guard.write(relative, data, overwrite=False)
        return {"path": str(path), "sha256": digest, "size": len(data)}

    @staticmethod
    def _baseline_path(repository: Repository, path: str) -> str:
        # Host-only preset admission does not expand model Repository access.
        return path if path == "CMakeUserPresets.json" else repository.normalize_plan_path(path)

    def source_baseline_view(self, task_id: str, paths: Iterable[str] = ()) -> SourceBaselineView:
        """Read source identity and selectable final working bytes without staging it."""
        with self._operation(task_id) as state:
            git = self._git(state, main=True)
            repo = Repository(Path(state["repository_path"]), "")
            selected = sorted({self._baseline_path(repo, path) for path in paths})
            source = Guard(state["repository_path"])
            head = self._head(git)
            files, total = {}, 0
            for path in selected:
                data = source.read(path, max_bytes=16_777_216) if source.path(path, missing=True).exists() else None
                total += len(data) if data is not None else 0
                if total > 16_777_216:
                    raise ForgePilotError("workspace_limit", "Selected baseline exceeds the 16 MiB source preview limit")
                files[path] = data
            dirty = set(git.run(["diff", "--name-only", "--no-renames", "-z", "HEAD", "--"]).stdout.decode("utf-8").split("\0"))
            dirty.update(git.run(["ls-files", "--others", "--exclude-standard", "-z"]).stdout.decode("utf-8").split("\0"))
            # This common local preset is the only ignored-file exception. Its
            # bytes are never included unless the user selects and confirms it.
            user_presets = "CMakeUserPresets.json"
            if (source.path(user_presets, missing=True).is_file()
                    and git.run(["ls-files", "--others", "--ignored", "--exclude-standard", "-z", "--", user_presets]).stdout):
                source.read(user_presets, max_bytes=16_777_216)
                dirty.add(user_presets)
            visible = []
            for path in sorted(dirty - {""}):
                if path != user_presets and any(excluded(part) for part in path.replace("\\", "/").split("/")):
                    continue
                try:
                    visible.append(self._baseline_path(repo, path))
                except ForgePilotError as exc:
                    if exc.code not in {"repository_path", "repository_boundary"}:
                        raise
            if self._head(git) != head:
                raise ForgePilotError("baseline_changed", "Source HEAD changed; preview again")
            return {"source_head": head, "files": files, "changed_files": visible,
                    "file_hashes": {path: sha256(data).hexdigest() if data is not None else None
                                    for path, data in files.items()}}

    def prepare_baseline(self, task_id, paths):
        with self._operation(task_id) as state:
            self._assert_expected(state, self._snapshot(state))
            if state["branch"] is not None or state.get("intent") or state["status"] != "active":
                raise ForgePilotError("baseline_state", "Select the baseline before approving the execution plan")
            repo = Repository(Path(state["repository_path"]), "")
            selected = sorted({self._baseline_path(repo, path) for path in paths})
            if not selected or len(selected) > 1000:
                raise ForgePilotError("baseline_paths", "Select between 1 and 1000 safe files")
            git = self._git(state, main=True)
            head = self._head(git)
            source = Guard(state["repository_path"])
            def selected_hashes():
                return {p: sha256(source.read(p, max_bytes=16_777_216)).hexdigest()
                        if source.path(p, missing=True).exists() else None for p in selected}
            before = selected_hashes()
            # A private index produces a patch of final working bytes, without changing
            # the user's real index, staged changes, stash, files or current branch.
            relative = f"tasks/{task_id}-{uuid4().hex}.index"
            index = self.guard.path(relative, missing=True)
            env = {"GIT_INDEX_FILE": str(index)}
            try:
                git.run(["read-tree", head], env=env)
                regular = [path for path in selected if path != "CMakeUserPresets.json"]
                if regular:
                    git.run(["add", "--all", "--", *regular], env=env)
                if "CMakeUserPresets.json" in selected:
                    git.run(["add", "--force", "--all", "--", "CMakeUserPresets.json"], env=env)
                patch = git.run(["diff", "--cached", "--binary", "--full-index", "--no-ext-diff",
                                 "--no-textconv", "--no-color", head, "--", *selected], env=env).stdout
            finally:
                # Only this exact private index may be removed; never the source index.
                if self.guard.path(relative, missing=True).exists():
                    self.guard.unlink(relative, sha256(self.guard.read(relative)).hexdigest())
            if self._head(git) != head or before != selected_hashes():
                raise ForgePilotError("baseline_changed", "Selected source files or HEAD changed while generating patch")
            artifact = self._artifact(state, "baseline.patch", patch)
            metadata = {"type": "BaselinePatch", "version": 1, "source_head": head,
                        "paths": selected, "file_hashes": before, "patch": artifact}
            metadata_artifact = self._artifact(state, "baseline.json", _json(metadata))
            state["baseline"] = metadata | {"metadata": metadata_artifact, "applied": False}
            self._save(state)
            return artifact | {"metadata": metadata_artifact, "paths": selected,
                               "source_head": head, "file_hashes": before, "patch_text": patch.decode("utf-8", errors="replace")}

    def apply_baseline(self, task_id, artifact_sha256, *, approved, expected):
        if approved is not True:
            raise ForgePilotError("workspace_approval", "Selected Baseline Patch approval is required")
        with self._operation(task_id) as state:
            self._assert_expected(state, expected)
            if state["branch"] is not None or state.get("intent") or state["status"] != "active":
                raise ForgePilotError("baseline_state", "Baseline application requires an unchanged pre-plan worktree")
            baseline = state.get("baseline")
            if not baseline or baseline["patch"]["sha256"] != artifact_sha256 or baseline["applied"]:
                raise ForgePilotError("baseline_integrity", "Baseline patch is missing, changed or already applied")
            patch = self.guard.read(Path(baseline["patch"]["path"]).relative_to(self.root).as_posix(), max_bytes=16_777_216)
            if sha256(patch).hexdigest() != artifact_sha256:
                raise ForgePilotError("baseline_integrity", "Baseline patch hash changed")
            metadata = baseline["metadata"]
            metadata_bytes = self.guard.read(Path(metadata["path"]).relative_to(self.root).as_posix())
            if sha256(metadata_bytes).hexdigest() != metadata["sha256"]:
                raise ForgePilotError("baseline_integrity", "Baseline metadata hash changed")
            git = self._git(state)
            after = expected
            if patch:
                check = git.run(["apply", "--check", "--index", "--binary", "-"], data=patch, allowed=(0, 1))
                if check.returncode:
                    state["status"] = "conflict"
                    self._save(state)
                    raise ForgePilotError("workspace_conflict", "Baseline conflicts with selected base; waiting for user")
                result_files = dict(expected.file_hashes)
                for path, digest in baseline["file_hashes"].items():
                    if digest is None:
                        result_files.pop(path, None)
                    else:
                        result_files[path] = digest
                state["intent"] = {"operation": "baseline", "sha256": artifact_sha256,
                                   "head": expected.head_commit, "before_files": dict(expected.file_hashes),
                                   "result_files": result_files}
                self._save(state)
                guard = Guard(state["worktree_path"])
                for name in result_files:
                    parent = Path(name).parent.as_posix()
                    if parent != ".":
                        guard.mkdir(parent)
                with guard.protect_tree():
                    git.run(["apply", "--index", "--binary", "-"], data=patch)
                    after = self._snapshot(state)
                    if dict(after.file_hashes) != result_files:
                        raise ForgePilotError("baseline_integrity", "Applied patch bytes differ from approved baseline")
            self._set_expected(state, after)
            state["baseline"]["applied"] = True
            state.pop("intent", None)
            self._save(state)
            return self._snapshot(state)

    def freeze_source_baseline(self, task_id: str, artifact_sha256: str, *,
                               approved: bool, expected: WorkspaceSnapshot) -> WorkspaceSnapshot:
        """Commit confirmed source bytes before Graph creation, without plan approval."""
        if approved is not True:
            raise ForgePilotError("workspace_approval", "Source baseline confirmation is required")
        with self._operation(task_id) as state:
            self._assert_expected(state, expected)
            baseline = state.get("baseline")
            if (state["branch"] is not None or state.get("intent") or state.get("checkpoints")
                    or not baseline or not baseline["applied"] or baseline["patch"]["sha256"] != artifact_sha256):
                raise ForgePilotError("baseline_state", "Only a confirmed pre-analysis baseline can be frozen")
            git = self._git(state)
            tree = git.run(["write-tree"]).stdout.decode().strip()
            commit = self._commit(git, tree, expected.head_commit, "forgepilot(source): confirmed source baseline")
            self._verify_commit(state, commit, expected.file_hashes,
                                message="forgepilot(source): confirmed source baseline")
            state["intent"] = {"operation": "source_baseline", "before_head": expected.head_commit,
                               "commit": commit, "files": dict(expected.file_hashes)}
            self._save(state)
            git.run(["update-ref", "--no-deref", "HEAD", commit, expected.head_commit])
            after = self._snapshot(state)
            if after.head_commit != commit or dict(after.file_hashes) != dict(expected.file_hashes):
                raise ForgePilotError("baseline_integrity", "Source baseline changed while freezing")
            state["source_base_ref"] = state["base_ref"]
            state["base_ref"] = commit
            self._set_expected(state, after)
            state.pop("intent")
            self._save(state)
            return after

    @staticmethod
    def _message(kind, repair_round=None):
        if kind == "repair" and type(repair_round) is int and 1 <= repair_round <= 3:
            return f"forgepilot(checkpoint): repair round {repair_round}"
        if kind not in MESSAGES:
            raise ForgePilotError("checkpoint_policy", "Use a fixed checkpoint kind and repair round 1..3")
        return MESSAGES[kind]

    def _commit(self, git, tree, parent, message):
        self.policy.require(self.principal, Capability.COMMIT)
        return git.run(["commit-tree", tree, "-p", parent], data=(message + "\n").encode(),
                       env={"GIT_AUTHOR_NAME": AUTHOR, "GIT_AUTHOR_EMAIL": EMAIL,
                            "GIT_COMMITTER_NAME": AUTHOR, "GIT_COMMITTER_EMAIL": EMAIL}).stdout.decode().strip()

    def checkpoint_commit(self, task_id, *, kind, expected, repair_round=None, checkpoint_id=None):
        self.policy.require(self.principal, Capability.COMMIT)
        message = self._message(kind, repair_round)
        checkpoint_id = _id(checkpoint_id or "cp-" + uuid4().hex)
        with self._operation(task_id) as state:
            if checkpoint_id in state["checkpoints"]:
                saved = state["checkpoints"][checkpoint_id]
                current = self._snapshot(state)
                valid_input = (isinstance(expected, WorkspaceSnapshot) and expected.known
                               and expected.workspace_id == current.workspace_id and expected.task_id == current.task_id
                               and ((expected.head_commit == saved["head"] and dict(expected.file_hashes) == saved["files"])
                                    or (expected.head_commit == saved.get("input_head")
                                        and dict(expected.file_hashes) == saved.get("input_files"))))
                if not valid_input or saved["message"] != message or current.head_commit != saved["head"] or dict(current.file_hashes) != saved["files"]:
                    raise ForgePilotError("checkpoint_integrity", "Repeated checkpoint no longer matches the worktree")
                return current
            current = self._assert_expected(state, expected)
            if state.get("intent") or state["status"] != "active":
                raise ForgePilotError("workspace_recovery", "Pending operation requires explicit recovery")
            if state["branch"] is None:
                raise ForgePilotError("workspace_approval", "Plan approval is required before checkpoint commits")
            git = self._git(state)
            git.run(["add", "--all", "--", "."])
            if self._snapshot(state) != current:
                raise ForgePilotError("workspace_changed", "Worktree changed while preparing checkpoint")
            tree = git.run(["write-tree"]).stdout.decode().strip()
            commit = self._commit(git, tree, current.head_commit, message)
            self._verify_commit(state, commit, current.file_hashes, message=message)
            state["intent"] = {"operation": "checkpoint", "checkpoint_id": checkpoint_id,
                               "parent": current.head_commit, "head": commit, "files": dict(current.file_hashes),
                               "message": message, "index": self._index_hash(state)}
            self._save(state)  # Commit exists; compare-and-swap HEAD is the only remaining Git write.
            git.run(["update-ref", "refs/heads/" + state["branch"], commit, current.head_commit])
            after = self._snapshot(state)
            if dict(after.file_hashes) != dict(current.file_hashes):
                raise ForgePilotError("workspace_changed", "Checkpoint file manifest changed")
            self._publish_checkpoint(state)
            return after

    def _publish_checkpoint(self, state):
        intent = state["intent"]
        if self._index_hash(state) != intent["index"]:
            raise ForgePilotError("workspace_changed", "Git index changed during checkpoint publication")
        state["checkpoints"][intent["checkpoint_id"]] = {
            "head": intent["head"], "files": intent["files"], "message": intent["message"],
            "input_head": intent["parent"], "input_files": intent["files"]}
        state["expected_head"], state["expected_files"] = intent["head"], intent["files"]
        state["expected_index"] = intent["index"]
        state["validated_commit"], state["validated_files"] = None, None
        if getattr(self._local, "active", None) is None:
            state.pop("writer_before", None)
            state.pop("writer_observed", None)
        state.pop("intent")
        self._save(state)

    def mark_validated(self, task_id, *, expected):
        with self._operation(task_id) as state:
            current = self._assert_expected(state, expected)
            # Git status can hide edits behind assume-unchanged/skip-worktree.
            # A validator certifies exact committed bytes, independent of flags.
            self._verify_commit(state, current.head_commit, current.file_hashes)
            if self._git(state).run(["status", "--porcelain=v1", "-z", "--untracked-files=all"]).stdout:
                raise ForgePilotError("workspace_dirty", "Validation must refer to a committed clean worktree")
            state["validated_commit"], state["validated_files"] = current.head_commit, dict(current.file_hashes)
            self._save(state)
            return current

    def recover_task(self, task_id, *, approved, expected):
        if approved is not True:
            raise ForgePilotError("workspace_approval", "Recovery requires a new human confirmation")
        with self._operation(task_id, recover=True) as state:
            current = self._snapshot(state)
            if current != expected:
                raise ForgePilotError("workspace_changed", "Recovery snapshot changed")
            intent = state.get("intent")
            if intent and intent["operation"] == "checkpoint":
                if dict(current.file_hashes) != intent["files"] or current.head_commit not in {intent["parent"], intent["head"]}:
                    raise ForgePilotError("checkpoint_integrity", "Interrupted checkpoint does not match durable intent")
                if self._index_hash(state) != intent["index"]:
                    raise ForgePilotError("workspace_changed", "Interrupted checkpoint index changed")
                self._verify_commit(state, intent["head"], intent["files"], message=intent["message"])
                if current.head_commit == intent["parent"]:
                    self._git(state).run(["update-ref", "refs/heads/" + state["branch"], intent["head"], intent["parent"]])
                self._publish_checkpoint(state)
            elif intent and intent["operation"] == "baseline":
                if current.head_commit != intent["head"]:
                    raise ForgePilotError("baseline_integrity", "Baseline recovery HEAD changed")
                if dict(current.file_hashes) == intent["result_files"]:
                    tree = self._git(state).run(["write-tree"]).stdout.decode().strip()
                    self._verify_commit(state, tree, current.file_hashes)
                    self._set_expected(state, current)
                    state["baseline"]["applied"] = True
                elif dict(current.file_hashes) == intent["before_files"]:
                    if self._git(state).run(["status", "--porcelain=v1", "-z", "--untracked-files=all"]).stdout:
                        raise ForgePilotError("baseline_integrity", "Baseline index requires user inspection")
                else:
                    raise ForgePilotError("baseline_integrity", "Partial baseline or unknown files require user inspection")
                state.pop("intent", None)
            elif intent:
                raise ForgePilotError("workspace_recovery", "Interrupted baseline or delivery requires user inspection; never auto merge")
            elif state.get("writer_before"):
                raise ForgePilotError("workspace_recovery", "Interrupted writer must restore a verified checkpoint")
            elif current.head_commit != state["expected_head"] or dict(current.file_hashes) != state["expected_files"]:
                raise ForgePilotError("workspace_changed", "External changes cannot be adopted by recovery")
            state["status"] = "active"
            self._save(state)
            return self._snapshot(state)

    @staticmethod
    def _tree_bytes(git, commit):
        raw = git.run(["ls-tree", "-r", "-z", commit]).stdout
        entries = []
        for row in raw.split(b"\0"):
            if not row:
                continue
            header, path = row.split(b"\t", 1)
            mode, kind, oid = header.split()
            name = validate_relative(path.decode("utf-8", errors="strict"))
            if mode not in {b"100644", b"100755"} or kind != b"blob":
                raise ForgePilotError("checkpoint_integrity", "Checkpoint contains a link or submodule")
            entries.append((name, oid))
        if len(entries) > 2000:
            raise ForgePilotError("workspace_limit", "Commit contains too many files")
        response = git.run(["cat-file", "--batch"], data=b"".join(oid + b"\n" for _, oid in entries)).stdout if entries else b""
        result, offset = {}, 0
        for name, oid in entries:
            end = response.find(b"\n", offset)
            header = response[offset:end].split() if end >= 0 else []
            if len(header) != 3 or header[:2] != [oid, b"blob"] or not header[2].isdigit():
                raise ForgePilotError("checkpoint_integrity", "Git object batch is incomplete")
            size = int(header[2])
            offset = end + 1
            if size > 16_777_216 or response[offset + size:offset + size + 1] != b"\n":
                raise ForgePilotError("workspace_limit", "Commit file exceeds the safe byte limit")
            result[name] = response[offset:offset + size]
            offset += size + 1
        if offset != len(response):
            raise ForgePilotError("checkpoint_integrity", "Git object batch contains unexpected data")
        # Reuse the complete snapshot grammar for case and path collisions.
        from .orchestration.workspace import _manifest
        _manifest({name: sha256(data).hexdigest() for name, data in result.items()})
        return result

    def _verify_commit(self, state, commit, files, *, message=None):
        if not _HASH.fullmatch(commit):
            raise ForgePilotError("checkpoint_integrity", "Invalid checkpoint commit")
        git = self._git(state)
        actual = {name: sha256(data).hexdigest() for name, data in self._tree_bytes(git, commit).items()}
        if actual != {p: h for p, h in files.items() if h is not None}:
            raise ForgePilotError("checkpoint_integrity", "Checkpoint tree differs from the exact file bytes; check Git EOL/attributes")
        if message is not None:
            info = git.run(["show", "--no-patch", "--format=%an%x00%ae%x00%cn%x00%ce%x00%B", commit]).stdout.decode().rstrip("\n")
            if info != "\0".join((AUTHOR, EMAIL, AUTHOR, EMAIL, message)):
                raise ForgePilotError("checkpoint_integrity", "Checkpoint author or message changed")

    def checkpoint_boundary(self, record, checkpoint_id, expected, *, kind, repair_round=None, approved_paths=None):
        self._check_record(record)
        state = self._load(record["task_id"])
        if state.get("intent", {}).get("operation") == "checkpoint":
            if state["intent"]["checkpoint_id"] != checkpoint_id:
                raise ForgePilotError("checkpoint_integrity", "Pending checkpoint belongs to another node boundary")
            self.recover_task(record["task_id"], approved=True, expected=expected)
        elif state["status"] == "interrupted_writer" and kind in {"implementation", "repair"}:
            # Called by RecoveryManager only after frozen ChangeSet and complete
            # manifests have been verified and the human confirms recovery.
            with self._operation(record["task_id"], recover=True) as state:
                current = self._snapshot(state)
                observed = state.get("writer_observed")
                before = state.get("writer_before")
                if (current != expected or not before or not observed
                        or current.head_commit != before["head"]
                        or dict(current.file_hashes) != observed["files"]):
                    raise ForgePilotError("workspace_changed", "Interrupted writer output changed or cannot be proven")
                if self._index_hash(state) != state["expected_index"]:
                    raise ForgePilotError("workspace_changed", "Interrupted writer index changed")
                self._check_scope(state, WorkspaceSnapshot(current.workspace_id, current.task_id, before["head"], before["files"]), current)
                self._set_expected(state, current)
                state["status"] = "active"
                state.pop("writer_before", None)
                state.pop("writer_observed", None)
                self._save(state)
        if kind not in {"plan_approved", "implementation", "repair", "validated"}:
            with self._operation(record["task_id"]) as state:
                self._assert_expected(state, expected)
                saved = {"head": expected.head_commit, "files": dict(expected.file_hashes), "message": None}
                if checkpoint_id in state["checkpoints"] and state["checkpoints"][checkpoint_id] != saved:
                    raise ForgePilotError("checkpoint_integrity", "Checkpoint ID already belongs to a different safe point")
                state["checkpoints"][checkpoint_id] = saved
                self._save(state)
                return expected
        if kind == "plan_approved":
            if checkpoint_id not in self._load(record["task_id"])["checkpoints"]:
                self.approve_plan(record["task_id"], approved=True, expected=expected, approved_paths=approved_paths)
        if kind == "repair" and repair_round is None:
            state = self._load(record["task_id"])
            existing = (state["checkpoints"].get(checkpoint_id, {}).get("message") or "")
            repair_round = int(existing.rsplit(" ", 1)[1]) if existing.startswith("forgepilot(checkpoint): repair round ") else (
                1 + sum(1 for item in state["checkpoints"].values()
                        if (item["message"] or "").startswith("forgepilot(checkpoint): repair round ")))
        result = self.checkpoint_commit(record["task_id"], kind=kind, expected=expected,
                                        repair_round=repair_round, checkpoint_id=checkpoint_id)
        if kind == "validated":
            self.mark_validated(record["task_id"], expected=result)
        return result

    def checkpoint(self, record, checkpoint_id, expected):
        return self.checkpoint_boundary(record, checkpoint_id, expected, kind="implementation")

    def verify_checkpoint(self, record, checkpoint_id, head_commit, file_hashes):
        state = self._check_record(record)
        saved = state["checkpoints"].get(checkpoint_id)
        intent = state.get("intent")
        if saved is None and intent and intent["operation"] == "checkpoint" and intent["checkpoint_id"] == checkpoint_id:
            saved = intent
        if not saved or saved["head"] != head_commit or saved["files"] != dict(file_hashes):
            raise ForgePilotError("checkpoint_integrity", "Checkpoint does not match durable manifest")
        self._verify_commit(state, head_commit, file_hashes, message=saved["message"])

    def restore(self, record, checkpoint_id, *, expected):
        self._check_record(record)
        with self._operation(record["task_id"], recover=True) as state:
            current = self._snapshot(state)
            saved = state["checkpoints"].get(checkpoint_id)
            if not saved or current.head_commit != saved["head"]:
                raise ForgePilotError("checkpoint_integrity", "Restore requires the verified checkpoint HEAD")
            self._verify_commit(state, saved["head"], saved["files"], message=saved["message"])
            previous = state.get("restore_intent") or state.get("last_restore")
            valid_identity = (isinstance(expected, WorkspaceSnapshot) and expected.known
                              and expected.workspace_id == current.workspace_id and expected.task_id == current.task_id)
            if (previous and previous["checkpoint_id"] == checkpoint_id and valid_identity
                    and expected.head_commit == saved["head"]
                    and dict(expected.file_hashes) in (previous["input_files"], saved["files"])
                    and dict(current.file_hashes) == saved["files"]):
                if not state.get("restore_intent") and self._index_hash(state) != state["expected_index"]:
                    raise ForgePilotError("workspace_changed", "Git index changed after completed restore")
                tree = self._git(state).run(["write-tree"]).stdout.decode().strip()
                self._verify_commit(state, tree, saved["files"])
                self._set_expected(state, current)
                state["status"] = "active"
                state["last_restore"] = previous
                for key in ("restore_intent", "writer_before", "writer_observed"):
                    state.pop(key, None)
                self._save(state)
                return current
            if current != expected:
                raise ForgePilotError("workspace_changed", "Worktree changed before restore")
            # Only a registered interrupted writer's observed changes may be discarded.
            if not state.get("writer_before"):
                raise ForgePilotError("workspace_recovery", "Restore requires an interrupted owned writer")
            observed = state.get("writer_observed")
            if (not observed or current.head_commit != observed["head"]
                    or dict(current.file_hashes) != observed["files"]):
                raise ForgePilotError("workspace_changed", "Interrupted output changed or contains unknown user files")
            if self._index_hash(state) != state["expected_index"]:
                raise ForgePilotError("workspace_changed", "Interrupted writer Git index changed")
            extra = set(current.file_hashes) - set(saved["files"])
            state["restore_intent"] = {"checkpoint_id": checkpoint_id, "input_files": dict(current.file_hashes),
                                       "head": saved["head"], "result_files": saved["files"]}
            self._save(state)
            guard = Guard(state["worktree_path"])
            for name in saved["files"]:
                parent = Path(name).parent.as_posix()
                if parent != ".":
                    guard.mkdir(parent)
            with guard.protect_tree():
                self._git(state).run(["reset", "--hard", saved["head"]])
                for name in sorted(extra):
                    guard.unlink(name, current.file_hashes[name])
            result = self._snapshot(state)
            if dict(result.file_hashes) != saved["files"]:
                raise ForgePilotError("checkpoint_integrity", "Restored files do not match checkpoint")
            self._set_expected(state, result)
            state["status"] = "active"
            state.pop("writer_before", None)
            state.pop("writer_observed", None)
            state["last_restore"] = state.pop("restore_intent")
            self._save(state)
            return result

    def cleanup(self, task_id, *, approved, expected):
        if approved is not True:
            raise ForgePilotError("workspace_approval", "Discarding a worktree requires explicit approval")
        state = self._load(task_id)
        if state["status"] == "abandoned":
            if (expected.workspace_id != state["workspace_id"] or expected.task_id != task_id
                    or expected.head_commit != state["expected_head"] or dict(expected.file_hashes) != state["expected_files"]):
                raise ForgePilotError("workspace_changed", "Cleanup retry does not match the removed worktree")
            return {"status": "abandoned", "worktree_path": state["worktree_path"], "branch": state["branch"]}
        with self._operation(task_id, recover=True) as state:
            if state.get("intent", {}).get("operation") == "cleanup":
                intent = state["intent"]
                if (expected.workspace_id != state["workspace_id"] or expected.task_id != task_id
                        or expected.head_commit != intent["head"] or dict(expected.file_hashes) != intent["files"]):
                    raise ForgePilotError("workspace_changed", "Cleanup retry requires the original approved snapshot")
                return self._finish_cleanup(state)
            self._assert_expected(state, expected)
            if state.get("intent") or state.get("writer_before"):
                raise ForgePilotError("workspace_recovery", "Resolve pending operations before cleanup")
            git = self._git(state)
            untracked = git.run(["ls-files", "--others", "--exclude-standard", "-z"]).stdout
            ignored = git.run(["ls-files", "--others", "--ignored", "--exclude-standard", "-z"]).stdout
            others = {p.decode("utf-8", errors="strict") for p in (untracked + ignored).split(b"\0") if p}
            if others - set(state.get("owned_files", ())):
                raise ForgePilotError("workspace_dirty", "Cleanup preserves unknown user and ignored files")
            state["intent"] = {"operation": "cleanup", "head": expected.head_commit,
                               "files": dict(expected.file_hashes), "admin_path": str(git.git_dir),
                               "admin_identity": _identity(git.git_dir), "admin_files": manifest(git.git_dir)}
            owned_paths = set(state["initial_files"]) | set(state.get("owned_files", ())) | set(expected.file_hashes)
            state["intent"]["directories"] = sorted({"/".join(path.split("/")[:i])
                                                      for path in owned_paths for i in range(1, len(path.split("/")))})
            state["status"] = "pending_cleanup"
            self._save(state)
            return self._finish_cleanup(state)

    def _finish_cleanup(self, state):
        intent = state["intent"]
        # Never pass a vacated pathname to recursive Git removal. Windows DELETE
        # handles bind each deletion to the approved file/directory identity.
        self._git(state, main=True)
        remove_worktree_files(state["worktree_path"], intent["files"], state["marker_hash"],
                              expected_root_identity=state["worktree_identity"],
                              allowed_empty_directories=intent.get("directories", ()))
        remove_worktree_registration(intent["admin_path"], state["common_dir"], state["worktree_path"],
                                     intent["admin_files"], expected_root_identity=intent["admin_identity"])
        state["status"] = "abandoned"
        self._complete_delivery_operation(state)
        state.pop("intent", None)
        self._save(state)
        return {"status": "abandoned", "worktree_path": state["worktree_path"], "branch": state["branch"]}


__all__ = ["WorkspaceManager"]
