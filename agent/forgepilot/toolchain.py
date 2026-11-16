"""Host-owned command templates and separately approved custom build commands.

No command string is evaluated by a shell. Repository/model text cannot add
placeholders, executables, arguments, or approvals to a built-in profile.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
from pathlib import Path, PureWindowsPath
import re
import stat
from string import Formatter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .presets import MissionBuildSelection

from .errors import ForgePilotError
from .path_identity import path_within, same_path


_INTERPRETERS = {"cmd", "command", "powershell", "pwsh", "sh", "bash", "dash", "zsh", "fish",
                 "wsl", "py", "python", "pythonw", "node", "perl", "ruby", "cscript", "wscript",
                 "mshta", "rundll32", "regsvr32", "git"}
_SCRIPT_SUFFIXES = {".bat", ".cmd", ".ps1", ".py", ".pyw", ".js", ".vbs", ".sh"}
_PROTECTED_COMPONENTS = {".git", ".oracle", "oracle", "protected-oracle", "protected_oracle",
                         "hidden-oracle", "hidden_oracle", ".hidden-tests", ".hidden_tests",
                         "hidden-tests", "hidden_tests", "__hidden_tests__", ".forgepilot-tests"}
_PLACEHOLDERS = frozenset({"source_dir", "build_dir", "configuration", "jobs", "generator", "architecture",
                           "make_program", "configure_preset", "build_preset", "test_preset", "report_path",
                           "workspace_dir", "target_build_dir"})


def validate_executable(value: str, *, expected: str | None = None) -> Path:
    try:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError()
        lexical = PureWindowsPath(value)
        if (value.startswith(("\\\\", "//")) or ":" in value[len(lexical.drive):]
                or any(part.casefold() in _PROTECTED_COMPONENTS
                       or PureWindowsPath(part).is_reserved() for part in lexical.parts)):
            raise ValueError()
        path = Path(value)
        info = path.lstat()
        name = path.stem.casefold()
        if (not path.is_absolute() or not same_path(path.resolve(strict=True), path) or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1 or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError()
        for parent in path.parents:
            if getattr(parent.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError()
        if (name in _INTERPRETERS or re.fullmatch(r"python\d+(?:\.\d+)?w?", name)
                or path.suffix.casefold() in _SCRIPT_SUFFIXES):
            raise ValueError()
        if expected and path.name.casefold() not in {expected, expected + ".exe"}:
            raise ValueError()
        return path
    except (OSError, ValueError, RuntimeError, TypeError):
        raise ForgePilotError("command_denied", "Build executable must be a trusted ordinary executable; shells, interpreters and Git are forbidden") from None


def _scalar(value: str) -> str:
    if not isinstance(value, str) or len(value) > 32_768 or any(ord(c) < 32 for c in value):
        raise ForgePilotError("command_denied", "Invalid command argument")
    return value


@dataclass(frozen=True)
class ResolvedCommand:
    executable: str
    args: tuple[str, ...]
    cwd: Path
    shell: bool = False

    def __post_init__(self):
        if self.shell is not False:
            raise ForgePilotError("command_denied", "Build commands always require shell=False")

    @property
    def argv(self) -> list[str]:
        return [self.executable, *self.args]

    def review(self) -> dict:
        return {"executable": self.executable, "args": list(self.args), "cwd": str(self.cwd), "shell": False}


def _expand(template: str, values: dict[str, str]) -> str:
    """Only plain, known host-provided fields; no attribute/index/format evaluation."""
    try:
        for _, field, format_spec, conversion in Formatter().parse(template):
            if field is not None and (field not in _PLACEHOLDERS or field not in values or format_spec or conversion):
                raise ValueError()
        return _scalar(template.format_map(values))
    except (KeyError, ValueError):
        raise ForgePilotError("toolchain_placeholder", "Only trusted ToolchainProfile placeholders are permitted") from None


@dataclass(frozen=True)
class ToolchainProfile:
    """Fixed CMake/CTest profile. Only the host supplies these absolute tools."""
    cmake: str
    ctest: str
    profile_id: str = "cmake-ctest-v1"

    def resolve(self, phase: str, values: dict[str, str], *, preset: bool = False,
                multi_config: bool = False, generator=False, architecture=False, make_program=False) -> ResolvedCommand:
        source = Path(_scalar(values["source_dir"]))
        if not source.is_absolute() or not same_path(source.resolve(strict=True), source) or source.is_symlink():
            raise ForgePilotError("command_denied", "Toolchain source directory must be the trusted real workspace")
        templates = {
            "preset_discovery": ("cmake", ("--list-presets=all",)),
            "ctest_inventory": ("ctest", ("--test-dir", "{build_dir}", "-C", "{configuration}", "--show-only=json-v1")),
            "cmake_configure": ("cmake", ("--preset", "{configure_preset}") if preset else ("-S", "{source_dir}", "-B", "{build_dir}")),
            "cmake_build": ("cmake", ("--build", "--preset", "{build_preset}") if preset else ("--build", "{build_dir}")),
            "run_tests": ("ctest", ("--preset", "{test_preset}") if preset else ("--test-dir", "{build_dir}")),
        }
        if phase not in templates:
            raise ForgePilotError("command_denied", "Phase is absent from the trusted ToolchainProfile")
        kind, args = templates[phase]
        if phase == "cmake_configure":
            if not preset:
                if generator:
                    args += ("-G", "{generator}")
                if architecture:
                    args += ("-A", "{architecture}")
                if make_program:
                    validate_executable(values["make_program"])
                    args += ("-DCMAKE_MAKE_PROGRAM={make_program}",)
            args += ("-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",)
            if not multi_config:
                args += ("-DCMAKE_BUILD_TYPE={configuration}",)
        elif phase == "cmake_build":
            args += ("--config", "{configuration}")
        elif phase == "run_tests":
            args += ("-C", "{configuration}", "--output-on-failure", "--no-tests=error", "--output-junit", "{report_path}")
        executable = validate_executable(self.cmake if kind == "cmake" else self.ctest, expected=kind)
        return ResolvedCommand(str(executable), tuple(_expand(arg, values) for arg in args), source)

    def resolve_mission(self, phase: str, values: dict[str, str], selection: MissionBuildSelection) -> ResolvedCommand:
        """Translate the reviewed safe preset subset, never invoke native --preset."""
        if selection.source == "default":
            selected = dict(values, generator=selection.generator or "", architecture=selection.architecture or "",
                            configuration=selection.configuration,
                            make_program=next((value for name, _, value in selection.cache_variables if name == "CMAKE_MAKE_PROGRAM"), ""))
            return self.resolve(phase, selected, generator=bool(selected["generator"]),
                architecture=bool(selected["architecture"]), make_program=bool(selected["make_program"]),
                multi_config=bool(selection.generator and ("Visual Studio" in selection.generator or "Multi-Config" in selection.generator or selection.generator == "Xcode")))
        source = Path(_scalar(values["source_dir"]))
        if not source.is_absolute() or not same_path(source.resolve(strict=True), source) or source.is_symlink():
            raise ForgePilotError("command_denied", "Toolchain source directory must be the trusted real workspace")
        build = _scalar(values["build_dir"])
        configuration = ("-C", selection.configuration) if selection.configuration else ()
        if phase == "cmake_configure":
            args = ("-S", str(source), "-B", build, "-G", selection.generator)
            if selection.architecture: args += ("-A", selection.architecture)
            if selection.toolset: args += ("-T", selection.toolset)
            args += tuple("-D" + name + (":" + kind if kind else "") + "=" + value for name, kind, value in selection.cache_variables)
        elif phase == "cmake_build":
            args = ("--build", build)
            if selection.configuration: args += ("--config", selection.configuration)
            if selection.build_jobs is not None: args += ("--parallel", str(selection.build_jobs))
            if selection.build_verbose: args += ("--verbose",)
            if selection.build_clean_first: args += ("--clean-first",)
            if selection.build_targets: args += ("--target", *selection.build_targets)
        elif phase == "ctest_inventory":
            args = ("--test-dir", build, *configuration, "--show-only=json-v1")
        elif phase == "run_tests":
            args = ("--test-dir", build, *configuration, "--no-tests=error", "--output-junit", values["report_path"])
            if selection.test_output_on_failure: args += ("--output-on-failure",)
            if selection.test_jobs is not None: args += ("--parallel", str(selection.test_jobs))
            if selection.test_timeout is not None: args += ("--timeout", str(selection.test_timeout))
        else:
            raise ForgePilotError("command_denied", "Phase is absent from the Mission ToolchainProfile")
        kind = "cmake" if phase.startswith("cmake_") else "ctest"
        executable = validate_executable(self.cmake if kind == "cmake" else self.ctest, expected=kind)
        return ResolvedCommand(str(executable), tuple(_scalar(arg) for arg in args), source)

    def resolve_oracle(self, phase: str, values: dict[str, str], **options) -> ResolvedCommand:
        """Host-only Oracle project; neither paths nor commands are model inputs.

        Oracle source and output remain outside the task Worktree. The trusted
        project selects production sources using these two fixed CMake fields.
        """
        command = self.resolve(phase, values, **options)
        args = command.args
        if phase == "cmake_configure":
            args += tuple(_expand(value, values) for value in (
                "-DFORGEPILOT_WORKTREE={workspace_dir}",
                "-DFORGEPILOT_TARGET_BUILD={target_build_dir}",
            ))
        return ResolvedCommand(command.executable, args, command.cwd)


def _custom_cwd(root: Path, relative: str) -> Path:
    try:
        _scalar(relative)
        raw = relative.replace("\\", "/")
        if not raw or raw.startswith("/") or PureWindowsPath(raw).is_absolute() or ":" in raw or ".." in raw.split("/"):
            raise ValueError()
        current = root
        for part in raw.split("/"):
            if part in {"", "."}:
                continue
            if (part.casefold() in _PROTECTED_COMPONENTS
                    or part.endswith((" ", ".")) or PureWindowsPath(part).is_reserved()):
                raise ValueError()
            current /= part
            info = current.lstat()
            if current.is_symlink() or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError()
        if not current.is_dir() or not same_path(current.resolve(strict=True), current) or not path_within(current, root):
            raise ValueError()
        return current
    except (OSError, ValueError, RuntimeError):
        raise ForgePilotError("command_denied", "Custom command cwd must be an ordinary directory inside the approved workspace") from None


@dataclass(frozen=True)
class CustomCommandApproval:
    digest: str


@dataclass(frozen=True)
class CustomCommand:
    """Reviewable exact command; plan approval does not approve this object."""
    root: Path
    executable: str
    args: tuple[str, ...]
    cwd: str = "."

    def _resolved(self) -> ResolvedCommand:
        root = self.root.resolve(strict=True)
        if not same_path(self.root, root) or self.root.is_symlink():
            raise ForgePilotError("command_denied", "Custom command workspace changed")
        executable = validate_executable(self.executable)
        if type(self.args) is not tuple or len(self.args) > 256:
            raise ForgePilotError("command_denied", "Custom command requires a bounded argument array")
        for arg in self.args:
            _scalar(arg)
            if any(char in arg for char in "|<>;&`\r\n") or "$(" in arg:
                raise ForgePilotError("command_denied", "Pipelines, redirection, command substitution and command chaining are forbidden")
            name = PureWindowsPath(arg).stem.casefold()
            if name in _INTERPRETERS or re.fullmatch(r"python\d+(?:\.\d+)?w?", name):
                raise ForgePilotError("command_denied", "Custom commands cannot dispatch a shell, interpreter or Git")
        modes = {"cmake": {"-E", "-P", "--workflow", "--open"},
                 "ctest": {"-S", "-SP", "--script", "--script-new-process", "--build-and-test"}}
        if any(arg == mode or (arg.startswith(mode) if len(mode) <= 3 else arg.startswith(mode + "="))
               for arg in self.args for mode in modes.get(executable.stem.casefold(), set())):
            raise ForgePilotError("command_denied", "Custom commands cannot use arbitrary script/dispatch modes")
        return ResolvedCommand(str(executable), self.args, _custom_cwd(root, self.cwd))

    def _manifest(self) -> tuple[ResolvedCommand, str]:
        result = self._resolved()
        # Bind approval to executable contents as well as argv/cwd. Replacing a
        # configured binary after human review must require another approval.
        path = Path(result.executable)
        info = path.stat()
        if info.st_size > 268_435_456:
            raise ForgePilotError("command_denied", "Custom executable exceeds the approval hashing limit")
        executable_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        cwd_info = result.cwd.stat()
        manifest = {**result.review(), "executable_sha256": executable_hash,
                    "executable_identity": [info.st_dev, info.st_ino],
                    "cwd_identity": [cwd_info.st_dev, cwd_info.st_ino],
                    "root_identity": [self.root.stat().st_dev, self.root.stat().st_ino]}
        digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        return result, digest

    def review(self) -> dict:
        result, digest = self._manifest()
        return {**result.review(), "approval_digest": digest, "requires_separate_approval": True}

    def approve(self, expected_digest: str, *, approved: bool = False) -> CustomCommandApproval:
        _, digest = self._manifest()
        if approved is not True or not isinstance(expected_digest, str) or not hmac.compare_digest(expected_digest, digest):
            raise ForgePilotError("command_approval", "Review and separately approve the exact executable, args and cwd")
        return CustomCommandApproval(digest)

    def resolve(self, approval: CustomCommandApproval | None = None) -> ResolvedCommand:
        result, digest = self._manifest()
        if not isinstance(approval, CustomCommandApproval) or not hmac.compare_digest(approval.digest, digest):
            raise ForgePilotError("command_approval", "Custom command is missing its unchanged, separate approval")
        return result
