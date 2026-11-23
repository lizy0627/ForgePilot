"""Revision-bound, local-only delivery controlled by WorkspaceManager.

No entry point accepts a Git command, a commit message, or an output path.
Approval tokens bind both complete task bytes and the selected target revision.
An interrupted merge is deliberately never retried or rolled back automatically.
"""
from __future__ import annotations

from hashlib import sha256
import difflib
from fnmatch import fnmatchcase
import hmac
import json
from pathlib import Path
import re
import secrets
from tempfile import TemporaryDirectory

from .artifacts.models import (ChangedFile, ChangeSet, DeliveryBundle, ExecutionPlanArtifact,
                               ReviewReport, TaskSpecArtifact, ValidationReport)
from .artifacts.validation import validate_artifact
from .errors import ForgePilotError
from .integrity import _assertions, is_test_path
from .path_identity import path_key
from .workspace_safety import Guard, manifest, validate_relative


_HASH = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$")
_MESSAGE = "forgepilot(delivery): squash validated result"
_MODES = frozenset({"squash", "keep_branch", "patch", "bundle", "abandon"})


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _fail(code, message):
    raise ForgePilotError(code, message)


class DeliveryMixin:
    def recover_observed_change_set(self, task_id, spec_artifact, plan_artifact, baseline):
        """Describe only a recorded interrupted writer's unambiguous production edits.

        This host-only operation returns observed data, never a model completion
        or permission grant. RecoveryManager still owns adoption/checkpointing,
        and deterministic validation plus independent review remain mandatory.
        Source bytes, workspace state, Artifacts and graph state are not written.
        """
        spec_artifact = TaskSpecArtifact.model_validate(validate_artifact(spec_artifact))
        plan_artifact = ExecutionPlanArtifact.model_validate(validate_artifact(plan_artifact))
        if (spec_artifact.task_id != task_id or plan_artifact.task_id != task_id
                or plan_artifact.content.task_spec_artifact_id != spec_artifact.artifact_id):
            _fail("workspace_recovery", "Observed recovery requires the same task and frozen plan")
        spec, plan = spec_artifact.content, plan_artifact.content
        criteria = {criterion.id for criterion in spec.acceptance_criteria}
        if (getattr(baseline, "task_id", None) != task_id
                or not getattr(baseline, "workspace_id", None)
                or not getattr(baseline, "head_commit", None)):
            _fail("workspace_recovery", "Observed recovery requires a complete approved baseline")
        baseline_files = dict(baseline.file_hashes)
        if any(value is None for value in baseline_files.values()):
            _fail("workspace_recovery", "Observed recovery requires a complete existing-file baseline")
        frozen = set(getattr(baseline, "input_artifacts", ())) | set(getattr(baseline, "output_artifacts", ()))
        if frozen and not {(spec_artifact.artifact_id, spec_artifact.content_sha256),
                           (plan_artifact.artifact_id, plan_artifact.content_sha256)} <= frozen:
            _fail("workspace_recovery", "Recovery Artifacts differ from the approved checkpoint")
        with self._operation(task_id, recover=True) as state:
            before, observed = state.get("writer_before"), state.get("writer_observed")
            if (state["status"] != "interrupted_writer" or state.get("intent") or not before or not observed
                    or state["workspace_id"] != baseline.workspace_id
                    or before.get("head") != baseline.head_commit or before.get("files") != baseline_files
                    or state["expected_head"] != baseline.head_commit
                    or state["expected_files"] != baseline_files):
                _fail("workspace_recovery", "Interrupted writer does not start at the approved baseline")
            current = self._snapshot(state)
            index_hash = self._index_hash(state)
            if (current.head_commit != baseline.head_commit or current.head_commit != observed.get("head")
                    or dict(current.file_hashes) != observed.get("files")
                    or index_hash != state["expected_index"]):
                _fail("workspace_changed", "Interrupted output, HEAD or index changed after observation")
            checkpoint_id = getattr(baseline, "checkpoint_id", None)
            if checkpoint_id:
                self.verify_checkpoint(self.record(task_id), checkpoint_id, baseline.head_commit, baseline_files)
            git = self._git(state)
            self._verify_commit(state, baseline.head_commit, baseline_files)
            original = self._tree_bytes(git, baseline.head_commit)
            if {name: sha256(data).hexdigest() for name, data in original.items()} != baseline_files:
                _fail("workspace_recovery", "Git baseline bytes differ from the frozen manifest")
            if set(current.file_hashes) != set(baseline_files):
                _fail("workspace_recovery", "Observed recovery does not support additions or deletions")
            changed = sorted(name for name in baseline_files if baseline_files[name] != current.file_hashes[name])
            if not changed or len(changed) > spec.allowed_scope.max_changed_files:
                _fail("workspace_recovery", "Observed changes are empty or exceed the frozen file budget")
            approved = {path_key(name) for name in (state.get("approved_paths") or ())}
            files = []
            guard = Guard(state["worktree_path"])
            for path in changed:
                validate_relative(path)
                key = path_key(path)
                allowed = any(fnmatchcase(key, path_key(pattern)) for pattern in spec.allowed_scope.path_patterns)
                forbidden = any(fnmatchcase(key, path_key(pattern)) for pattern in spec.forbidden_scope)
                if key not in approved or not allowed or forbidden or is_test_path(path):
                    _fail("workspace_scope", "Observed recovery is limited to approved production files")
                steps = [step for step in plan.steps if step.owner in {"implementer", "implementer.v1"}
                         and key in {path_key(name) for name in step.expected_files}]
                if (len(steps) != 1 or not steps[0].acceptance_criteria_ids
                        or not set(steps[0].acceptance_criteria_ids) <= criteria):
                    _fail("workspace_recovery", "Observed file has no unique approved plan and criterion mapping")
                old, new = original[path], guard.read(path)
                if (sha256(old).hexdigest() != baseline_files[path]
                        or sha256(new).hexdigest() != current.file_hashes[path]):
                    _fail("workspace_changed", "Observed file bytes changed during recovery inspection")
                try:
                    if b"\0" in old or b"\0" in new:
                        raise UnicodeError()
                    old_text, new_text = old.decode("utf-8"), new.decode("utf-8")
                except UnicodeError:
                    _fail("workspace_recovery", "Observed recovery requires ordinary UTF-8 production text")
                # Assertion-bearing files may mix test and production behavior;
                # this minimal recovery path does not infer test-edit authority.
                if _assertions(old_text) or _assertions(new_text) or path.lower().endswith(".cmake") or path_key(Path(path).name) == "cmakelists.txt":
                    _fail("workspace_recovery", "Test/assertion/build-registration changes require explicit recovery review")
                rows = difflib.unified_diff(old_text.splitlines(keepends=True),
                    new_text.splitlines(keepends=True), fromfile="a/" + path, tofile="b/" + path)
                diff = "".join(row if row.endswith("\n") else row + "\n\\ No newline at end of file\n" for row in rows)
                if not diff:
                    _fail("workspace_recovery", "Observed production change has no exact text diff")
                files.append(ChangedFile(path=path, change_type="modified", unified_diff=diff,
                    before_sha256=baseline_files[path], after_sha256=current.file_hashes[path],
                    plan_step_ids=(steps[0].step_id,), acceptance_criteria_ids=steps[0].criteria_for_path(path),
                    test_change="none"))
            if self._snapshot(state) != current or self._index_hash(state) != index_hash:
                _fail("workspace_changed", "Workspace changed before observed recovery returned")
            return ChangeSet(execution_plan_artifact_id=plan_artifact.artifact_id, files=tuple(files),
                requires_additional_approval=False,
                summary="Host recovered observed production changes after an Artifact-storage interruption; no model reply was replayed")

    @staticmethod
    def _complete_delivery_operation(state):
        operation_id = state.pop("pending_delivery_operation_id", None)
        if operation_id is not None:
            state["delivery_operation_id"] = operation_id
        else:
            state.pop("delivery_operation_id", None)

    def final_change_set(self, task_id, changes, *, plan_artifact_id=None):
        """Derive the cumulative base-to-checkpoint diff from Git and verified bytes.

        Incremental ChangeSets supply only plan/AC provenance, never final bytes.
        This keeps repairs that touch the same file from hiding earlier changes.
        """
        changes = tuple(ChangeSet.model_validate(item) for item in changes)
        if not changes:
            _fail("delivery_changes", "Expected at least one successful writer ChangeSet")
        final_plan_id = plan_artifact_id or changes[-1].execution_plan_artifact_id
        earlier_plans = {item.execution_plan_artifact_id for item in changes} - {final_plan_id}
        # Successful writer checkpoints bind each incremental diff to its grant.
        # Only their original plan or a same-task descendant can carry them forward.
        cursor = final_plan_id
        visited = set()
        while earlier_plans:
            if self.artifacts is None or cursor in visited:
                _fail("delivery_changes", "Final changes require one plan revision history")
            visited.add(cursor)
            plan = self.artifacts.get(cursor)
            if not isinstance(plan, ExecutionPlanArtifact) or plan.task_id != task_id:
                _fail("delivery_changes", "Final plan revision belongs to a different task")
            parents = [self.artifacts.get(parent_id) for parent_id in plan.parent_artifact_ids]
            prior = [item for item in parents if isinstance(item, ExecutionPlanArtifact) and item.task_id == task_id]
            if len(prior) != 1:
                _fail("delivery_changes", "Writer plan is outside the final plan revision history")
            cursor = prior[0].artifact_id
            earlier_plans.discard(cursor)
        with self._operation(task_id) as state:
            current = self._snapshot(state)
            self._assert_expected(state, current)
            git = self._git(state)
            self._verify_commit(state, current.head_commit, current.file_hashes)
            before = self._tree_bytes(git, state["base_ref"])
            files = []
            for path in self._changed_files(state, git, current):
                provenance = [file for change in changes for file in change.files if file.path == path]
                if not provenance:
                    _fail("delivery_changes", "Final diff has no approved writer provenance")
                original = before.get(path)
                final_hash = current.file_hashes.get(path)
                diff = git.run(["diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv",
                    "--no-color", "--no-renames", state["base_ref"], current.head_commit, "--", path]).stdout.decode("utf-8")
                files.append(ChangedFile(path=path,
                    change_type="added" if original is None else "deleted" if final_hash is None else "modified",
                    before_sha256=sha256(original).hexdigest() if original is not None else None,
                    after_sha256=final_hash, unified_diff=diff,
                    plan_step_ids=tuple(sorted({value for item in provenance for value in item.plan_step_ids})),
                    acceptance_criteria_ids=tuple(sorted({value for item in provenance for value in item.acceptance_criteria_ids})),
                    test_change=provenance[0].test_change))
            self._assert_expected(state, current)
            if not files:
                _fail("delivery_changes", "A delivery requires a nonempty final diff")
            return ChangeSet(execution_plan_artifact_id=final_plan_id,
                files=tuple(files), summary="Cumulative approved implementation and repair diff")

    def export_validated_patch(self, task_id):
        """Prepare a reviewable patch; no merge, branch publication or final approval."""
        with self._operation(task_id) as state:
            current = self._snapshot(state)
            self._assert_expected(state, current)
            git = self._delivery_validated(state, current)
            result = self._artifact(state, "delivery.patch", self._delivery_patch(state, git, current))
            self._assert_expected(state, current)
            return result

    def prepare_delivery_bundle(self, task_id, bundle):
        """Persist the verified delivery package while retaining final user control."""
        with self._operation(task_id) as state:
            current = self._snapshot(state)
            self._assert_expected(state, current)
            git = self._delivery_validated(state, current)
            result = self._bundle(state, bundle, current, git, self._delivery_patch(state, git, current))
            self._assert_expected(state, current)
            return result

    def _delivery_recovery_observation(self, state):
        intent = state.get("intent")
        if (state["status"] != "pending_delivery" or not intent
                or intent.get("operation") != "delivery" or intent.get("mode") != "squash"):
            _fail("delivery_recovery", "There is no interrupted squash delivery to inspect")
        current = self._snapshot(state)
        self._assert_expected(state, current)
        if current.head_commit != intent["task_commit"]:
            _fail("delivery_changed", "Task changed after interrupted delivery")
        target = self._delivery_target(state, intent["target_branch"])
        outcome = "manual_inspection"
        reason = None
        if target["checked_out_branch"] != target["branch"]:
            reason = "源仓库当前检出的分支与交付目标分支不同。"
        elif target["dirty"]:
            reason = "源仓库的索引或工作区仍有差异，请检查后重新核验。"
        elif target["commit"] == target["main_head"] == intent["head"]:
            outcome = "completed"
        elif (target["commit"] == target["main_head"] == intent["parent"]
              and target["files"] == intent["main_files"]):
            outcome = "restored"
        else:
            reason = "目标提交或文件内容与记载的交付结果和原始状态均不匹配。"
        return {"head": current.head_commit, "files": dict(current.file_hashes),
                "target": target, "outcome": outcome}, reason

    def delivery_recovery_preview(self, task_id, *, approved=False):
        """Observe an uncertain delivery; approval permits reclaiming a stale lease.

        This never changes a Git ref, index, or worktree. Incomplete publication
        requires the user to inspect/restore the main workspace themselves.
        """
        with self._operation(task_id, recover=approved is True) as state:
            observed, reason = self._delivery_recovery_observation(state)
            token = secrets.token_hex(32)
            state["delivery_recovery_preview"] = {
                **observed, "token_sha256": sha256(token.encode()).hexdigest()}
            self._save(state)
            intent = state["intent"]
            return {"token": token, "task_id": task_id, "outcome": observed["outcome"],
                    "task_commit": observed["head"], "target_branch": intent["target_branch"],
                    "reason": reason, "parent_commit": intent["parent"], "delivery_commit": intent["head"],
                    "current_target_commit": observed["target"]["commit"],
                    "target_clean": not observed["target"]["dirty"],
                    "file_hashes": observed["target"]["files"]}

    def resolve_delivery(self, task_id, *, approved, token, operation_id=None):
        """Confirm an already completed merge or a user-restored baseline only."""
        if approved is not True:
            _fail("delivery_approval", "Confirming a delivery outcome requires explicit user approval")
        with self._operation(task_id) as state:
            preview = state.get("delivery_recovery_preview")
            if (not isinstance(token, str) or len(token) != 64 or not preview
                    or not hmac.compare_digest(sha256(token.encode()).hexdigest(), preview["token_sha256"])):
                _fail("delivery_approval", "Inspect the current delivery recovery evidence first")
            observed, _ = self._delivery_recovery_observation(state)
            if observed != {key: value for key, value in preview.items() if key != "token_sha256"}:
                _fail("delivery_changed", "Delivery recovery evidence changed; inspect it again")
            if observed["outcome"] == "manual_inspection":
                _fail("delivery_recovery", "The main workspace requires manual user inspection or restoration")
            intent = state["intent"]
            state["status"] = "delivered" if observed["outcome"] == "completed" else "active"
            result = {"mode": "squash", "status": state["status"], "outcome": observed["outcome"],
                      "target_branch": intent["target_branch"],
                      "target_commit": observed["target"]["commit"]}
            if observed["outcome"] == "completed":
                result["commit"] = intent["head"]
            state["delivery_result"] = result
            if operation_id is not None:
                state["pending_delivery_operation_id"] = operation_id
            self._complete_delivery_operation(state)
            state.pop("intent")
            state.pop("delivery_recovery_preview", None)
            state.pop("delivery_preview", None)
            self._save(state)
            return result

    def _delivery_active(self, state):
        if state.get("intent") or state.get("writer_before") or state["status"] not in {"active", "delivered"}:
            _fail("delivery_recovery", "Pending or conflicted operations require user inspection before delivery")

    @staticmethod
    def _branch_name(git, branch):
        if (not isinstance(branch, str) or not _BRANCH.fullmatch(branch)
                or ".." in branch or "//" in branch or "/." in branch
                or branch.endswith(("/", ".", ".lock"))):
            _fail("delivery_target", "Select a local target branch")
        git.run(["check-ref-format", "refs/heads/" + branch])
        return branch

    def _checkout_manifest(self, git, tree):
        """Hash a clean target checkout, including the attributes in this tree."""
        # Validate the paths, modes and blob limits before letting Git materialize them.
        names = set(self._tree_bytes(git, tree))
        with self.guard.protect(), TemporaryDirectory(dir=self.root) as temporary:
            scratch = Guard(temporary)
            checkout = Guard(scratch.mkdir("checkout"))
            for parent in {Path(name).parent.as_posix() for name in names} - {"."}:
                checkout.mkdir(parent)
            git.run(["read-tree", "--reset", "-u", tree], checkout_eol=True, checkout_root=checkout.root,
                    env={"GIT_INDEX_FILE": str(scratch.root / "index")})
            files = manifest(checkout.root)
            if files.keys() != names:
                _fail("delivery_target", "Target checkout does not contain the complete Git tree")
            return files

    def _delivery_target(self, state, target_branch):
        """Capture complete main bytes even when the user plans an export."""
        git = self._git(state, main=True)
        main_head = self._head(git)
        if target_branch is None:
            branch, head = None, main_head
        else:
            branch = self._branch_name(git, target_branch)
            head = git.run(["rev-parse", "--verify", "--end-of-options", "refs/heads/" + branch + "^{commit}"]).stdout.decode().strip()
        checked_out = git.run(["symbolic-ref", "-q", "--short", "HEAD"], allowed=(0, 1)).stdout.decode().strip() or None
        files = manifest(Path(state["repository_path"]))
        dirty = bool(git.run(["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored"],
                             checkout_eol=True).stdout)
        # Index flags such as assume-unchanged/skip-worktree can hide real user
        # edits. Keep the actual bytes in the preview even when Git reports clean.
        dirty = dirty or files != self._checkout_manifest(git, main_head)
        if self._head(git) != main_head:
            _fail("delivery_changed", "Target HEAD moved while preparing delivery")
        if not _HASH.fullmatch(head):
            _fail("delivery_target", "Target branch must name a committed revision")
        return {"branch": branch, "commit": head, "checked_out_branch": checked_out,
                "main_head": main_head, "files": files, "dirty": dirty}

    def delivery_preview(self, task_id, target_branch=None):
        """Return reviewable evidence; issuing a token never changes Git refs."""
        with self._operation(task_id) as state:
            self._delivery_active(state)
            current = self._snapshot(state)
            self._assert_expected(state, current)
            target = self._delivery_target(state, target_branch)
            token = secrets.token_hex(32)
            preview = {"token_sha256": sha256(token.encode()).hexdigest(),
                       "head": current.head_commit, "files": dict(current.file_hashes),
                       "target": target}
            state["delivery_preview"] = preview
            state["target_branch"], state["target_commit"] = target["branch"], target["commit"]
            self._save(state)
            return {"token": token, "task_id": task_id, "head_commit": current.head_commit,
                    "file_hashes": dict(current.file_hashes), "target_branch": target["branch"],
                    "target_commit": target["commit"], "target_clean": not target["dirty"],
                    "target_checked_out": (target["branch"] is not None
                                           and target["checked_out_branch"] == target["branch"]),
                    "validated": (state.get("validated_commit") == current.head_commit
                                  and state.get("validated_files") == dict(current.file_hashes))}

    def _check_delivery_token(self, state, token):
        preview = state.get("delivery_preview")
        if (not isinstance(token, str) or len(token) != 64 or not preview
                or not hmac.compare_digest(sha256(token.encode()).hexdigest(), preview["token_sha256"])):
            _fail("delivery_approval", "Delivery requires the current revision-bound approval token")
        current = self._snapshot(state)
        self._assert_expected(state, current)
        if current.head_commit != preview["head"] or dict(current.file_hashes) != preview["files"]:
            _fail("delivery_changed", "Task changed after delivery preview; review it again")
        return preview, current

    def _delivery_validated(self, state, current):
        if (state.get("validated_commit") != current.head_commit
                or state.get("validated_files") != dict(current.file_hashes)):
            _fail("delivery_validation", "Delivery requires validation of the current commit and complete file manifest")
        if not state.get("branch"):
            _fail("delivery_branch", "The approved task branch is required for delivery")
        git = self._git(state)
        if git.run(["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored"]).stdout:
            _fail("delivery_dirty", "Checkpoint all task changes before delivery")
        committed_files = {name: sha256(data).hexdigest()
                           for name, data in self._tree_bytes(git, current.head_commit).items()}
        if committed_files != dict(current.file_hashes):
            _fail("delivery_dirty", "Task bytes differ from the validated commit despite Git index flags")
        return git

    def _delivery_patch(self, state, git, current):
        return git.run(["diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv",
                        "--no-color", "--no-renames", "--ignore-submodules=all",
                        state["base_ref"], current.head_commit, "--"]).stdout

    @staticmethod
    def _changed_files(state, git, current):
        data = git.run(["diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", "--no-textconv",
                        state["base_ref"], current.head_commit, "--"]).stdout
        return tuple(p.decode("utf-8", errors="strict") for p in data.split(b"\0") if p)

    def _bundle(self, state, bundle, current, git, patch):
        if not isinstance(bundle, DeliveryBundle):
            _fail("delivery_bundle", "Supply a validated DeliveryBundle")
        # Revalidate even instances constructed with Pydantic's unchecked helpers.
        try:
            bundle = DeliveryBundle.model_validate_json(bundle.model_dump_json())
        except ValueError:
            _fail("delivery_bundle", "DeliveryBundle does not match its schema")
        changed = set(self._changed_files(state, git, current))
        if bundle.commit != current.head_commit or set(bundle.changed_files) != changed:
            _fail("delivery_bundle", "DeliveryBundle commit and file list must match the final diff")
        lineage = False
        if self.artifacts is not None:
            declared = ((bundle.change_set_artifact_id, ChangeSet),
                        (bundle.validation_report_artifact_id, ValidationReport),
                        (bundle.review_report_artifact_id, ReviewReport))
            parents = []
            try:
                for artifact_id, model in declared:
                    artifact = validate_artifact(self.artifacts.get(artifact_id))
                    if (artifact.artifact_id != artifact_id or artifact.task_id != state["task_id"]
                            or not isinstance(artifact.content, model)):
                        raise ValueError()
                    parents.append(artifact)
                cache = {artifact.artifact_id: artifact for artifact in parents}
                def has_ancestor(artifact, expected_id):
                    pending = list(artifact.parent_artifact_ids)
                    visited = set()
                    while pending:
                        artifact_id = pending.pop()
                        if artifact_id == expected_id:
                            return True
                        if artifact_id in visited:
                            continue
                        visited.add(artifact_id)
                        if len(visited) > 256:
                            raise ValueError()
                        parent = cache.get(artifact_id)
                        if parent is None:
                            parent = validate_artifact(self.artifacts.get(artifact_id))
                            if parent.artifact_id != artifact_id or parent.task_id != state["task_id"]:
                                raise ValueError()
                            cache[artifact_id] = parent
                        pending.extend(parent.parent_artifact_ids)
                    return False
                if (not has_ancestor(parents[1], parents[0].artifact_id)
                        or not has_ancestor(parents[2], parents[1].artifact_id)):
                    raise ValueError()
            except (KeyError, ValueError):
                _fail("delivery_bundle", "DeliveryBundle lineage must connect same-task changes, validation, and review")
            changes, validation, review = (p.content for p in parents)
            if (validation.status != "passed" or review.verdict != "approve"
                    or not review.recommend_delivery or review.blocking_issues
                    or set(f.path for f in changes.files) != changed):
                _fail("delivery_bundle", "DeliveryBundle requires passing validation, approving review, and matching changes")
            base_blobs = {}
            for row in git.run(["ls-tree", "-r", "-z", state["base_ref"]]).stdout.split(b"\0"):
                if row:
                    header, path = row.split(b"\t", 1)
                    _mode, kind, oid = header.split()
                    if kind == b"blob":
                        base_blobs[path.decode("utf-8", errors="strict")] = oid.decode("ascii")
            for changed_file in changes.files:
                oid = base_blobs.get(changed_file.path)
                before = sha256(git.run(["cat-file", "blob", oid]).stdout).hexdigest() if oid else None
                if (changed_file.before_sha256 != before
                        or changed_file.after_sha256 != current.file_hashes.get(changed_file.path)):
                    _fail("delivery_bundle", "ChangeSet hashes do not match the selected base and delivered bytes")
            lineage = True
        patch_artifact = self._artifact(state, "delivery.patch", patch)
        payload = bundle.model_dump(mode="json")
        payload["patch_path"] = patch_artifact["path"]
        bundle = DeliveryBundle.model_validate(payload)
        artifact = self._artifact(state, "DeliveryBundle.json", _json(bundle.model_dump(mode="json")))
        evidence = {"commit": current.head_commit, "base_ref": state["base_ref"],
                    "lineage_verified": lineage, "patch": patch_artifact, "bundle": artifact}
        evidence_artifact = self._artifact(state, "DeliveryBundle.manifest.json", _json(evidence))
        return {**evidence, "manifest": evidence_artifact}

    def _squash(self, state, preview, current, git):
        if preview["target"]["branch"] is None:
            _fail("delivery_target", "Select a local target branch before squash delivery")
        target = self._delivery_target(state, preview["target"]["branch"])
        if target != preview["target"]:
            _fail("delivery_changed", "Target branch or main workspace changed after approval; review it again")
        if target["checked_out_branch"] != target["branch"] or target["main_head"] != target["commit"]:
            _fail("delivery_target", "The selected target branch must be checked out in the main workspace")
        if target["dirty"]:
            _fail("delivery_dirty", "Main workspace must be completely clean before merging")
        main = self._git(state, main=True)
        result = main.run(["merge-tree", "--write-tree", "--messages",
                           target["commit"], current.head_commit], allowed=(0, 1))
        if result.returncode:
            state["status"] = "conflict"
            state["delivery_conflict"] = {"target_branch": target["branch"],
                                          "target_commit": target["commit"], "task_commit": current.head_commit}
            state.pop("delivery_preview", None)
            self._save(state)
            _fail("delivery_conflict", "Merge conflicts detected; waiting for the user to resolve them")
        tree = result.stdout.splitlines()[0].decode("ascii") if result.stdout.splitlines() else ""
        if not _HASH.fullmatch(tree):
            _fail("delivery_merge", "Git did not return a verifiable merge tree")
        merged_files = self._checkout_manifest(main, tree)
        directories = set()
        for name in merged_files:
            name = validate_relative(name)
            destination = main.guard.path(name, missing=True)
            if destination.exists() and destination.is_dir():
                _fail("delivery_merge", "A directory-to-file conversion requires user handling or patch export")
            parts = name.split("/")[:-1]
            directories.update("/".join(parts[:n]) for n in range(1, len(parts) + 1))
        for directory in directories:
            path = main.guard.path(directory, missing=True)
            if path.exists() and not path.is_dir():
                _fail("delivery_merge", "A file-to-directory conversion requires user handling or patch export")
        # Recheck after preflight; merge-tree writes only objects, never user files.
        self._assert_expected(state, current)
        if self._delivery_target(state, target["branch"]) != target:
            _fail("delivery_changed", "Target changed during merge preflight; review it again")
        commit = self._commit(main, tree, target["commit"], _MESSAGE)
        state["intent"] = {"operation": "delivery", "mode": "squash", "target_branch": target["branch"],
                           "parent": target["commit"], "head": commit, "tree": tree,
                           "task_commit": current.head_commit, "main_files": target["files"],
                           "merged_files": merged_files}
        state["status"] = "pending_delivery"
        state.pop("delivery_preview", None)
        self._save(state)  # Recovery must require a fresh human decision from this point.
        # Precreate every merged directory so Git never traverses an unpinned new
        # parent. A file-to-directory conversion fails closed and can be exported
        # as a patch instead. Existing directory handles stay held through CAS.
        for directory in sorted(directories, key=lambda value: (value.count("/"), value)):
            main.guard.mkdir(directory)
        with main.guard.protect_tree():
            self._publish_squash(state, main, target, current, tree, commit, merged_files)
        state["status"] = "delivered"
        state["delivery_result"] = {"mode": "squash", "commit": commit, "target_branch": target["branch"]}
        self._complete_delivery_operation(state)
        state.pop("intent", None)
        self._save(state)
        return dict(state["delivery_result"])

    def _publish_squash(self, state, main, target, current, tree, commit, merged_files):
        # read-tree refuses to overwrite files changed since the clean preflight.
        # update-ref publishes only if the selected branch still has its old HEAD.
        main.run(["read-tree", "-u", "-m", target["commit"], tree], checkout_eol=True)
        checked_out = main.run(["symbolic-ref", "-q", "--short", "HEAD"], allowed=(0, 1)).stdout.decode().strip()
        if checked_out != target["branch"] or self._head(main) != target["commit"]:
            _fail("delivery_changed", "Target changed during delivery; user inspection is required")
        if main.run(["write-tree"]).stdout.decode().strip() != tree:
            _fail("delivery_changed", "Main index changed during delivery; user inspection is required")
        published_files = manifest(main.root)
        if published_files != merged_files:
            self._recheckout_unchanged_blobs(main, target, tree, published_files, merged_files)
            published_files = manifest(main.root)
            if published_files != merged_files or main.run(["write-tree"]).stdout.decode().strip() != tree:
                _fail("delivery_changed", "Main files or index changed during checkout; user inspection is required")
        # Detect non-index external file changes before publishing the commit.
        if main.run(["diff", "--no-ext-diff", "--no-textconv", "--quiet", "--"],
                    allowed=(0, 1), checkout_eol=True).returncode:
            _fail("delivery_changed", "Main files changed during delivery; user inspection is required")
        status = main.run(["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored"],
                          checkout_eol=True).stdout
        if any(row[:2] in {b"??", b"!!"} for row in status.split(b"\0") if row):
            _fail("delivery_changed", "Main workspace gained user files during delivery; user inspection is required")
        self._assert_expected(state, current)
        if manifest(Path(state["repository_path"])) != published_files:
            _fail("delivery_changed", "Main files changed before publication; user inspection is required")
        main.run(["update-ref", "refs/heads/" + target["branch"], commit, target["commit"]])
        if (self._head(main) != commit
                or main.run(["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored"],
                            checkout_eol=True).stdout):
            _fail("delivery_changed", "Delivery outcome requires user inspection")

    @staticmethod
    def _recheckout_unchanged_blobs(main, target, tree, files, merged_files):
        # A two-tree checkout skips unchanged blobs, even when new attributes
        # require different working bytes. Only refresh their exact previewed bytes.
        if files.keys() != merged_files.keys():
            _fail("delivery_changed", "Main file inventory changed during delivery")
        paths = [name for name in files if files[name] != merged_files[name]]
        changed = set(main.run(["diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", "--no-textconv",
                                target["commit"], tree, "--"]).stdout.split(b"\0"))
        if any(files[name] != target["files"].get(name) or name.encode() in changed for name in paths):
            _fail("delivery_changed", "Main bytes changed during delivery; user inspection is required")
        for name in paths:
            if not main.guard.unlink(name, target["files"][name]):
                _fail("delivery_changed", "Main file disappeared before checkout")
            # Without --force Git also preserves a file recreated after the checked unlink.
            main.run(["checkout-index", "--index", "--ignore-skip-worktree-bits", "-z", "--stdin"],
                     data=name.encode() + b"\0", checkout_eol=True)

    def deliver(self, task_id, mode, *, approved, token, bundle=None, operation_id=None):
        from .policy import Capability
        self.policy.require(self.principal, Capability.DELIVER)
        if approved is not True:
            _fail("delivery_approval", "Final delivery requires explicit user approval")
        if not isinstance(mode, str) or mode not in _MODES:
            _fail("delivery_mode", "Select squash, keep_branch, patch, bundle, or abandon")
        if operation_id is not None and (not isinstance(operation_id, str)
                or re.fullmatch(r"[0-9a-f]{32}", operation_id) is None):
            _fail("delivery_mode", "Invalid delivery operation identity")
        abandon = None
        with self._operation(task_id) as state:
            self._delivery_active(state)
            preview, current = self._check_delivery_token(state, token)
            if operation_id is not None:
                state["pending_delivery_operation_id"] = operation_id
            else:
                state.pop("pending_delivery_operation_id", None)
            if mode == "abandon":
                # cleanup obtains the same repository lease and rechecks this exact
                # snapshot. It never receives an arbitrary path or recursive flag.
                state.pop("delivery_preview", None)
                self._save(state)
                abandon = current
            else:
                git = self._delivery_validated(state, current)
                if mode == "squash":
                    return self._squash(state, preview, current, git)
                result = {"mode": mode, "commit": current.head_commit, "branch": state["branch"]}
                if mode == "patch":
                    result["patch"] = self._artifact(state, "delivery.patch", self._delivery_patch(state, git, current))
                elif mode == "bundle":
                    result.update(self._bundle(state, bundle, current, git, self._delivery_patch(state, git, current)))
                self._assert_expected(state, current)
                state.pop("delivery_preview", None)
                state["status"] = "delivered"
                state["delivery_result"] = result
                self._complete_delivery_operation(state)
                self._save(state)
                return result
        return self.cleanup(task_id, approved=True, expected=abandon)
