"""Resolve the small set of build-selection fields; CMake validates preset availability."""

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re

from .errors import ForgePilotError
from .path_identity import path_key, path_within, same_path
from .repository import excluded, is_link
from .toolchain import validate_executable


@dataclass(frozen=True)
class BuildConfig:
    cmake: str
    ctest: str
    configure_preset: str | None = None
    build_preset: str | None = None
    test_preset: str | None = None
    generator: str | None = None
    architecture: str | None = None
    make_program: str | None = None
    configuration: str | None = None
    configure_timeout: float = 120
    build_timeout: float = 300
    test_timeout: float = 120

    def __post_init__(self):
        for path in (self.cmake, self.ctest, self.make_program):
            if path is not None and (not Path(path).is_absolute() or not Path(path).is_file()):
                raise ForgePilotError("build_config", "Build executables must be configured absolute file paths")
        validate_executable(self.cmake, expected="cmake")
        validate_executable(self.ctest, expected="ctest")
        if self.make_program:
            validate_executable(self.make_program)
        for timeout in (self.configure_timeout, self.build_timeout, self.test_timeout):
            if isinstance(timeout, bool) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
                raise ForgePilotError("build_config", "Build timeouts must be finite and in (0, 3600] seconds")
        for name in (self.configure_preset, self.build_preset, self.test_preset, self.configuration,
                     self.generator, self.architecture):
            if name is not None and (not name or len(name) > 256 or any(ord(c) < 32 for c in name)):
                raise ForgePilotError("build_config", "Invalid build selection string")


def safe_build_path(root: Path, value: str) -> Path:
    """Artifacts must stay in a non-source directory in this repository; links are denied."""
    raw = value.replace("\\", "/")
    if not raw or ".." in raw.split("/") or raw.startswith("//"):
        raise ForgePilotError("build_path", "Build directory cannot traverse parents or use UNC paths")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        if (not same_path(root.resolve(strict=True), root) or is_link(root.lstat())
                or not path_within(candidate, root)):
            raise ValueError()
        relative = candidate.relative_to(root)
        if not relative.parts:
            raise ValueError()
        # Generated build directories are allowed; secret/configuration locations are not.
        generated = lambda p: p.casefold() in {".forgepilot", "build", "out", "cmakefiles", "bin", "obj"} or p.casefold().startswith(("build-", "cmake-build-"))
        current = root
        for part in relative.parts:
            if (part.endswith((".", " ")) or PureWindowsPath(part).is_reserved() or ":" in part
                    or any(ord(c) < 32 or c in '<>"|?*' for c in part)
                    or (excluded(part) and not generated(part))):
                raise ValueError()
            current /= part
            if current.exists() or current.is_symlink():
                if is_link(current.lstat()) or not current.is_dir():
                    raise ValueError()
        resolved = candidate.resolve()
        if not path_within(resolved, root) or not same_path(resolved, candidate):
            raise ValueError()
        # Refuse an obvious source tree used as a build destination.
        if (candidate / "CMakeLists.txt").exists():
            raise ValueError()
        return resolved
    except (OSError, ValueError, RuntimeError):
        raise ForgePilotError("build_path", "Build directory must be inside the repository, outside protected/source paths, without links") from None


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


class Presets:
    def __init__(self, root: Path, env: dict[str, str], *, overrides: dict[str, bytes | None] | None = None):
        self.root = root
        self.env = env
        self.overrides = dict(overrides or {})
        self.documents = {}
        self.files: dict[str, tuple[Path, str]] = {}
        self.raw = {kind: {} for kind in ("configure", "build", "test")}
        self.present = self._input_exists(root / "CMakePresets.json") or self._input_exists(root / "CMakeUserPresets.json")
        for name in ("CMakePresets.json", "CMakeUserPresets.json"):
            if self._input_exists(root / name):
                self._load(root / name, set())

    def _input_exists(self, path: Path) -> bool:
        relative = path.relative_to(self.root).as_posix()
        return self.overrides[relative] is not None if relative in self.overrides else path.exists()

    def _read_input(self, path: Path) -> bytes:
        relative = path.relative_to(self.root).as_posix()
        if relative in self.overrides:
            data = self.overrides[relative]
            if not isinstance(data, bytes):
                raise ValueError("Preset file is deleted from the selected baseline")
            return data
        return path.read_bytes()

    def file_identity(self) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
        return (tuple(sorted((path.relative_to(self.root).as_posix(), digest) for path, digest in self.files.values())),
                tuple(name for name in ("CMakePresets.json", "CMakeUserPresets.json") if self._input_exists(self.root / name)))

    def resolve_mission(self, config: BuildConfig) -> "MissionBuildSelection":
        return _resolve_mission(self, config)

    def _expand(self, value: str, preset: dict, stack=()) -> str:
        if not isinstance(value, str):
            raise ForgePilotError("preset_fields", "Expected a string preset field")
        values = {"sourceDir": self.root.as_posix(), "sourceParentDir": self.root.parent.as_posix(),
                  "sourceDirName": self.root.name, "presetName": preset.get("name", ""),
                  "configurePresetName": preset.get("configurePreset", preset.get("name", "")),
                  "generator": preset.get("generator", ""), "hostSystemName": "Windows",
                  "fileDir": str(preset.get("_file", self.root / "CMakePresets.json").parent).replace("\\", "/"),
                  "dollar": "$", "pathListSep": os.pathsep}
        def substitute(match):
            namespace, key = match.group(1), match.group(2)
            token = (namespace, key)
            if token in stack or len(stack) >= 32:
                raise ForgePilotError("preset_macro", "Cyclic preset macro/environment expansion")
            def parent_environment(name):
                # Windows process environment lookup ignores case, but CMake's preset
                # environment map is case-sensitive (including on Windows).
                return next((v for k, v in self.env.items() if k.casefold() == name.casefold()), "")
            if namespace == "penv":
                return parent_environment(key)
            if namespace == "env":
                value = preset.get("environment", {}).get(key)
                return parent_environment(key) if value is None else self._expand(value, preset, (*stack, token))
            if namespace or key not in values:
                raise ForgePilotError("preset_macro", "Unsupported preset macro; choose a supported explicit preset")
            value = values[key]
            return self._expand(value, preset, (*stack, token)) if "$" in value else value
        return re.sub(r"\$(env|penv|vendor)?\{([^}]+)\}", substitute, value)

    def _load(self, path: Path, visiting: set[str]):
        try:
            if (not same_path(self.root.resolve(strict=True), self.root)
                    or not path_within(path, self.root)):
                raise ValueError()
            relative = path.relative_to(self.root)
            current = self.root
            for part in relative.parts:
                if part == "..":
                    raise ValueError()
                current /= part
                if (current.exists() or current.is_symlink()) and is_link(current.lstat()):
                    raise ValueError()
            resolved = path.resolve(strict=not bool(self.overrides))
            if not same_path(resolved, path):
                raise ValueError()
            path = resolved
            key = path_key(str(path))
            if key in visiting or len(self.files) >= 64:
                raise ValueError()
            if key in self.files:
                return
            data = self._read_input(path)
            if len(data) > 1_048_576:
                raise ValueError()
            document = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_object,
                                  parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON")))
            if not isinstance(document, dict) or type(document.get("version")) is not int:
                raise ValueError()
            self.documents[key] = document
            if document["version"] >= 12:
                # Preset v12 changed fileDir to the lexical file containing each macro.
                def lexical(item):
                    if isinstance(item, str):
                        return item.replace("${fileDir}", path.parent.as_posix())
                    if isinstance(item, dict):
                        return {k: lexical(v) for k, v in item.items()}
                    if isinstance(item, list):
                        return [lexical(v) for v in item]
                    return item
                document = lexical(document)
            for include in document.get("include", []):
                include_path = Path(self._expand(include, {"_file": path}))
                self._load(include_path if include_path.is_absolute() else path.parent / include_path, visiting | {key})
            for kind in self.raw:
                for preset in document.get(kind + "Presets", []):
                    name = preset["name"]
                    if not isinstance(name, str) or name in self.raw[kind]:
                        raise ValueError()
                    self.raw[kind][name] = {**preset, "_file": path}
            self.files[key] = (path, hashlib.sha256(data).hexdigest())
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            raise ForgePilotError("preset_invalid", "Preset JSON/include graph is invalid, too large, outside the repository, or contains links") from None

    def effective(self, kind: str, name: str, visiting=()) -> dict:
        if name in visiting or len(visiting) >= 64 or name not in self.raw[kind]:
            raise ForgePilotError("preset_inheritance", "Invalid preset inheritance")
        own = self.raw[kind][name]
        parents = own.get("inherits", [])
        parents = [parents] if isinstance(parents, str) else parents
        result = {}
        for parent in reversed(parents):
            inherited = self.effective(kind, parent, (*visiting, name))
            inherited = {k: v for k, v in inherited.items() if k not in {"name", "hidden", "inherits", "description", "displayName", "_file"}}
            if inherited.get("condition") is None:
                inherited.pop("condition", None)
            result = self._merge(result, inherited)
        return self._merge(result, own)

    @staticmethod
    def _merge(base, new):
        merged = {**base, **new}
        for field in ("environment", "cacheVariables"):
            if field in base or field in new:
                merged[field] = {**base.get(field, {}), **new.get(field, {})}
        return merged

    def unchanged(self):
        current_names = {path_key(str(self.root / name)) for name in ("CMakePresets.json", "CMakeUserPresets.json") if (self.root / name).exists()}
        original_names = {path_key(str(path)) for path, _ in self.files.values()
                          if same_path(path.parent, self.root) and path.name in {"CMakePresets.json", "CMakeUserPresets.json"}}
        try:
            if current_names != original_names or not same_path(self.root.resolve(strict=True), self.root):
                raise ValueError()
            for path, expected in self.files.values():
                current = self.root
                for part in path.relative_to(self.root).parts:
                    current /= part
                    if is_link(current.lstat()):
                        raise ValueError()
                if not same_path(path.resolve(), path) or path.stat().st_size > 1_048_576 or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                    raise ValueError()
        except (OSError, ValueError):
            raise ForgePilotError("build_selection_changed", "Preset files changed after review; start a task with the new explicit build selection") from None

    def choose(self, available: dict[str, set[str]], config: BuildConfig) -> dict:
        requested = (config.configure_preset, config.build_preset, config.test_preset)
        candidates = []
        for c in sorted(available["configure"]):
            if requested[0] is not None and c != requested[0]:
                continue
            for b in sorted(available["build"]):
                build = self.effective("build", b)
                if build.get("configurePreset") != c or (requested[1] is not None and b != requested[1]):
                    continue
                for t in sorted(available["test"]):
                    test = self.effective("test", t)
                    if test.get("configurePreset") != c or (requested[2] is not None and t != requested[2]):
                        continue
                    configure = self.effective("configure", c)
                    build_env = configure.get("environment", {}) if build.get("inheritConfigureEnvironment", True) else {}
                    test_env = configure.get("environment", {}) if test.get("inheritConfigureEnvironment", True) else {}
                    build = {**build, "generator": configure.get("generator", ""), "environment": {**build_env, **build.get("environment", {})}}
                    test = {**test, "generator": configure.get("generator", ""), "environment": {**test_env, **test.get("environment", {})}}
                    bc = self._expand(build.get("configuration", ""), build)
                    tc = self._expand(test.get("configuration", ""), test)
                    cache_type = configure.get("cacheVariables", {}).get("CMAKE_BUILD_TYPE", "")
                    cache_type = cache_type.get("value", "") if isinstance(cache_type, dict) else cache_type
                    cache_type = self._expand(cache_type or "", configure)
                    generator = self._expand(configure.get("generator", ""), configure)
                    multi = "Multi-Config" in generator or generator.startswith("Visual Studio") or generator == "Xcode"
                    configurations = {v for v in (bc, tc, config.configuration, None if multi else cache_type) if v}
                    if len(configurations) > 1:
                        continue
                    candidates.append({"source": "preset", "configure_preset": c, "build_preset": b, "test_preset": t,
                        "binary_dir": self._expand(configure.get("binaryDir", ""), configure),
                        "configuration": next(iter(configurations), "Debug"), "generator": generator or None,
                        "multi_config": multi, "note": "Matched configure/build/test presets"})
        if len(candidates) > 1:
            names = [f"{p['configure_preset']}/{p['build_preset']}/{p['test_preset']}" for p in candidates[:12]]
            raise ForgePilotError("preset_ambiguous", "Choose configure/build/test presets explicitly: " + ", ".join(names))
        if not candidates:
            if any(requested):
                raise ForgePilotError("preset_no_match", "Requested presets are unavailable or their configure/configuration fields do not match")
            return {"source": "default", "configure_preset": None, "build_preset": None, "test_preset": None,
                    "binary_dir": str(self.root / ".forgepilot/build"), "configuration": config.configuration or "Debug",
                    "generator": config.generator, "multi_config": None,
                    "note": "No applicable complete preset trio; use the default build directory"}
        selected = candidates[0]
        if config.generator or config.architecture or config.make_program:
            raise ForgePilotError("preset_override", "Generator/architecture/make-program flags are for default mode; configure the selected preset instead")
        return selected


def available_presets(output: str) -> dict[str, set[str]]:
    found = {"configure": set(), "build": set(), "test": set()}
    kind = None
    for line in output.splitlines():
        header = re.match(r"Available (\w+) presets:", line)
        if header:
            kind = header[1]
        name = re.match(r'^  "([^"\r\n]+)"', line)
        if kind in found and name:
            found[kind].add(name[1])
    return found


@dataclass(frozen=True)
class MissionBuildSelection:
    """Resolved preset semantics, independent of a repository-controlled build directory."""
    source: str
    configure_preset: str | None = None
    build_preset: str | None = None
    test_preset: str | None = None
    generator: str | None = None
    architecture: str | None = None
    toolset: str | None = None
    configuration: str = ""
    binary_dir: str | None = None
    cache_variables: tuple[tuple[str, str | None, str], ...] = ()
    build_targets: tuple[str, ...] = ()
    build_jobs: int | None = None
    build_verbose: bool = False
    build_clean_first: bool = False
    test_jobs: int | None = None
    test_timeout: float | None = None
    test_output_on_failure: bool = False
    preset_files: tuple[tuple[str, str], ...] = ()
    root_files: tuple[str, ...] = ()
    version: int = 1
    trusted_config_sha256: str = ""
    tool_files: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "MissionBuildSelection":
        # This is a durable recovery boundary, not a second preset parser.
        try:
            if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
                raise ValueError()
            item = dict(value)
            for name in ("cache_variables", "preset_files", "tool_files"):
                item[name] = tuple(tuple(row) for row in item[name])
            for name in ("root_files", "build_targets"):
                item[name] = tuple(item[name])
            selection = cls(**item)
            if type(selection.version) is not int or selection.version != 1 or selection.source not in {"preset", "default"}:
                raise ValueError()
            for name in ("configure_preset", "build_preset", "test_preset", "generator", "architecture", "toolset", "binary_dir"):
                field = getattr(selection, name)
                if field is not None and (not isinstance(field, str) or not field or len(field) > 32768 or any(ord(c) < 32 for c in field)):
                    raise ValueError()
            if not isinstance(selection.configuration, str) or any(ord(c) < 32 for c in selection.configuration):
                raise ValueError()
            for field in (selection.build_jobs, selection.test_jobs):
                if field is not None and (type(field) is not int or not 1 <= field <= 1024):
                    raise ValueError()
            if selection.test_timeout is not None and (type(selection.test_timeout) not in {int, float}
                    or not math.isfinite(selection.test_timeout) or not 0 < selection.test_timeout <= 3600):
                raise ValueError()
            if any(type(getattr(selection, field)) is not bool for field in ("build_verbose", "build_clean_first", "test_output_on_failure")):
                raise ValueError()
            for name, kind, field in selection.cache_variables:
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or kind not in {None, "BOOL", "STRING", "PATH", "FILEPATH", "INTERNAL"}:
                    raise ValueError()
                if not isinstance(field, str) or any(ord(c) < 32 for c in field):
                    raise ValueError()
            for path, digest in selection.preset_files:
                if not isinstance(path, str) or path.startswith(("/", "\\")) or ".." in path.replace("\\", "/").split("/") or ":" in path:
                    raise ValueError()
                if not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ValueError()
            if any(name not in {"CMakePresets.json", "CMakeUserPresets.json"} for name in selection.root_files):
                raise ValueError()
            if any(not isinstance(target, str) or not target or target.startswith("-") or any(ord(c) < 32 for c in target) for target in selection.build_targets):
                raise ValueError()
            if not re.fullmatch(r"[0-9a-f]{64}", selection.trusted_config_sha256):
                raise ValueError()
            for path, digest in selection.tool_files:
                if not isinstance(path, str) or not Path(path).is_absolute() or not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ValueError()
            return selection
        except (ValueError, TypeError, KeyError):
            raise ForgePilotError("build_selection_changed", "Frozen Mission build selection is invalid or has an unsupported version") from None

    def matches_config(self, config: BuildConfig) -> bool:
        return self.trusted_config_sha256 == _config_hash(config)

    def verify_tools(self, cmake: str, ctest: str) -> None:
        if len(self.tool_files) < 2 or not same_path(self.tool_files[0][0], cmake) or not same_path(self.tool_files[1][0], ctest):
            raise ForgePilotError("build_selection_changed", "Trusted CMake/CTest selection changed since task confirmation")
        for path, expected in self.tool_files:
            try:
                validate_executable(path)
                with open(path, "rb") as stream:
                    current = hashlib.file_digest(stream, "sha256").hexdigest()
            except (OSError, ForgePilotError):
                raise ForgePilotError("build_selection_changed", "A frozen build executable is unavailable or changed") from None
            if current != expected:
                raise ForgePilotError("build_selection_changed", "A frozen build executable changed since task confirmation")

    def verify(self, root: Path) -> None:
        if self.source != "preset":
            return
        try:
            current = Presets(root, {}).file_identity()
        except ForgePilotError:
            raise ForgePilotError("build_selection_changed", "Frozen preset files are unavailable, changed, or contain links") from None
        if current != (self.preset_files, self.root_files):
            raise ForgePilotError("build_selection_changed", "Preset files changed after baseline confirmation; create and confirm a new task")


def _config_hash(config: BuildConfig) -> str:
    return hashlib.sha256(json.dumps(asdict(config), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _freeze_tools(selection: MissionBuildSelection, config: BuildConfig) -> MissionBuildSelection:
    paths = [config.cmake, config.ctest]
    paths.extend(value for name, _, value in selection.cache_variables if name == "CMAKE_MAKE_PROGRAM")
    identities = []
    for path in paths:
        validate_executable(path)
        with open(path, "rb") as stream:
            identities.append((path, hashlib.file_digest(stream, "sha256").hexdigest()))
    return replace(selection, trusted_config_sha256=_config_hash(config), tool_files=tuple(identities))


def _preset_error(kind: str, name: str, field: str, reason: str):
    raise ForgePilotError("preset_unsupported", f"{kind} preset '{name}' field '{field}': {reason}")


def _mission_text(value, kind, name, field):
    if not isinstance(value, str) or not value or len(value) > 32768 or any(ord(c) < 32 for c in value):
        _preset_error(kind, name, field, "expected a bounded nonempty string without control characters")
    return value


def _mission_expand(presets: Presets, value, kind, preset, field):
    value = _mission_text(value, kind, preset["name"], field)
    for namespace, macro in re.findall(r"\$(env|penv|vendor)?\{([^}]+)\}", value):
        if namespace:
            _preset_error(kind, preset["name"], field, f"${namespace}{{{macro}}} depends on an external or mutable environment; use an explicit cache value")
        if macro not in {"sourceDir", "sourceParentDir", "sourceDirName", "presetName", "generator", "hostSystemName", "fileDir", "dollar", "pathListSep"}:
            _preset_error(kind, preset["name"], field, f"macro ${{{macro}}} cannot be preserved with the controlled build directory")
    return presets._expand(value, preset)


def _mission_effective(presets: Presets, kind, name, visiting=()):
    allowed = {"name", "hidden", "inherits", "description", "displayName", "condition", "vendor", "$comment", "_file"}
    allowed |= {"configure": {"generator", "binaryDir", "architecture", "toolset", "cacheVariables"},
                "build": {"configurePreset", "configuration", "targets", "jobs", "verbose", "cleanFirst", "inheritConfigureEnvironment"},
                "test": {"configurePreset", "configuration", "output", "execution", "inheritConfigureEnvironment"}}[kind]
    if name in visiting or name not in presets.raw[kind] or len(visiting) >= 64:
        raise ForgePilotError("preset_inheritance", f"Invalid {kind} preset inheritance at '{name}'")
    own = presets.raw[kind][name]
    for field in own.keys() - allowed:
        _preset_error(kind, name, field, "not in the supported Mission subset; it will not be silently ignored")
    if "condition" in own and own["condition"] is not None and type(own["condition"]) is not bool:
        _preset_error(kind, name, "condition", "only boolean conditions are supported; complex conditions need native CMake evaluation")
    if "cacheVariables" in own and not isinstance(own["cacheVariables"], dict):
        _preset_error(kind, name, "cacheVariables", "expected an object")
    if "hidden" in own and type(own["hidden"]) is not bool:
        _preset_error(kind, name, "hidden", "expected a boolean")
    parents = own.get("inherits", [])
    parents = [parents] if isinstance(parents, str) else parents
    if not isinstance(parents, list) or any(not isinstance(parent, str) for parent in parents):
        _preset_error(kind, name, "inherits", "expected a preset name or array of preset names")
    result = {}
    reachable = {path_key(str(own["_file"]))}
    pending = list(reachable)
    while pending:
        current = pending.pop()
        path, _ = presets.files[current]
        includes = list(presets.documents[current].get("include", []))
        if path.name == "CMakeUserPresets.json" and path.parent == presets.root and (presets.root / "CMakePresets.json").as_posix() in {p.as_posix() for p, _ in presets.files.values()}:
            includes.append("CMakePresets.json")
        for included in includes:
            key = path_key(str((path.parent / included).resolve()))
            if key not in reachable:
                reachable.add(key); pending.append(key)
    for parent in reversed(parents):
        if parent not in presets.raw[kind] or path_key(str(presets.raw[kind][parent]["_file"])) not in reachable:
            _preset_error(kind, name, "inherits", f"parent '{parent}' is not reachable through this file's include graph")
        parent_file = presets.raw[kind][parent]["_file"]
        if parent_file == presets.root / "CMakeUserPresets.json" and own["_file"] != parent_file:
            _preset_error(kind, name, "inherits", "project presets cannot inherit CMakeUserPresets.json presets")
        inherited = _mission_effective(presets, kind, parent, (*visiting, name))
        inherited = {k: v for k, v in inherited.items() if k not in {"name", "hidden", "inherits", "description", "displayName", "_file"}}
        if inherited.get("condition") is None:
            inherited.pop("condition", None)
        for field in ("output", "execution"):
            if field in result and field in inherited:
                _preset_error(kind, name, field, "combining nested options from multiple parents is outside the deterministic subset")
        result = presets._merge(result, inherited)
    for field in ("output", "execution"):
        if field in result and field in own:
            _preset_error(kind, name, field, "overriding inherited nested options is outside the deterministic subset")
    return presets._merge(result, own)


def _resolve_mission(presets: Presets, config: BuildConfig) -> MissionBuildSelection:
    requested = (config.configure_preset, config.build_preset, config.test_preset)
    if not any(requested):
        cache = (("CMAKE_MAKE_PROGRAM", None, config.make_program),) if config.make_program else ()
        return _freeze_tools(MissionBuildSelection("default", generator=config.generator, architecture=config.architecture,
            configuration=config.configuration or "Debug", cache_variables=cache), config)
    if config.generator or config.architecture or config.make_program:
        raise ForgePilotError("preset_override", "Mission preset selection cannot also override generator, architecture or make program")
    for key, document in presets.documents.items():
        path = presets.files[key][0]
        if not 2 <= document["version"] <= 10:
            _preset_error("file", path.name, "version", "Mission supports preset schema versions 2 through 10")
        if "cmakeMinimumRequired" in document:
            _preset_error("file", path.name, "cmakeMinimumRequired", "preset-level version requirements cannot be proven without running CMake; use CMakeLists.txt cmake_minimum_required")
        if document.get("include") and document["version"] < 4:
            _preset_error("file", path.name, "include", "includes require preset schema version 4 or later")
        if any(not isinstance(item, str) or "$" in item or Path(item).is_absolute() for item in document.get("include", [])):
            _preset_error("file", path.name, "include", "only literal repository-relative include paths are supported")
        unknown = document.keys() - {"version", "$schema", "$comment", "include", "configurePresets", "buildPresets", "testPresets", "packagePresets", "workflowPresets", "vendor"}
        if unknown:
            _preset_error("file", path.name, sorted(unknown)[0], "unsupported preset document field")
    candidates = []
    for c in presets.raw["configure"]:
        if requested[0] is not None and c != requested[0]: continue
        for b in presets.raw["build"]:
            if requested[1] is not None and b != requested[1]: continue
            for t in presets.raw["test"]:
                if requested[2] is not None and t != requested[2]: continue
                configure, build, test = (_mission_effective(presets, kind, name) for kind, name in (("configure", c), ("build", b), ("test", t)))
                if any(item.get("hidden", False) or item.get("condition") is False for item in (configure, build, test)): continue
                if build.get("configurePreset") == c and test.get("configurePreset") == c:
                    candidates.append((configure, build, test))
    if len(candidates) != 1:
        reason = "Choose one complete enabled configure/build/test combination" if candidates else "Requested configure/build/test presets are unavailable, hidden, disabled or do not reference the same configure preset"
        raise ForgePilotError("preset_ambiguous" if candidates else "preset_no_match", reason)
    configure, build, test = candidates[0]
    c, b, t = configure["name"], build["name"], test["name"]
    generator = _mission_expand(presets, configure.get("generator"), "configure", configure, "generator")
    if generator not in {"Ninja", "Ninja Multi-Config", "Unix Makefiles", "NMake Makefiles", "MinGW Makefiles", "Visual Studio 16 2019", "Visual Studio 17 2022", "Xcode"}:
        _preset_error("configure", c, "generator", "generator is not in the explicitly supported Mission set")
    multi = generator.startswith("Visual Studio") or generator in {"Ninja Multi-Config", "Xcode"}
    def platform_value(field):
        value = configure.get(field)
        if value is None: return None
        if isinstance(value, dict):
            if value.keys() - {"value", "strategy"} or value.get("strategy", "set") != "set":
                _preset_error("configure", c, field, "only CMake 'set' strategy is supported; IDE-managed external environment is unavailable")
            value = value.get("value")
        return _mission_expand(presets, value, "configure", configure, field)
    cache = []
    values = configure.get("cacheVariables", {})
    if not isinstance(values, dict): _preset_error("configure", c, "cacheVariables", "expected an object")
    for name, value in sorted(values.items()):
        if value is None: continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name): _preset_error("configure", c, "cacheVariables", "unsupported cache variable name")
        if (name in {"CMAKE_TOOLCHAIN_FILE", "CMAKE_HOME_DIRECTORY", "CMAKE_CACHEFILE_DIR", "CMAKE_GENERATOR", "CMAKE_GENERATOR_PLATFORM", "CMAKE_GENERATOR_TOOLSET", "CMAKE_PROJECT_TOP_LEVEL_INCLUDES"}
                or re.fullmatch(r"CMAKE_PROJECT(?:_[A-Za-z0-9_]+)?_INCLUDE(?:_BEFORE)?", name)
                or re.fullmatch(r"CMAKE_USER_MAKE_RULES_OVERRIDE(?:_[A-Za-z0-9_]+)?", name)
                or name in {"CMAKE_AR", "CMAKE_RANLIB", "CMAKE_LINKER", "CMAKE_LINKER_TYPE", "CMAKE_NM", "CMAKE_OBJCOPY", "CMAKE_OBJDUMP", "CMAKE_STRIP", "CMAKE_DLLTOOL", "CMAKE_ADDR2LINE", "CMAKE_READELF", "CMAKE_MT", "CMAKE_TEST_LAUNCHER", "CMAKE_CROSSCOMPILING_EMULATOR"}
                or re.fullmatch(r"CMAKE_[A-Za-z0-9_]+_(?:COMPILER(?:_[A-Za-z0-9_]+)?|LINKER_LAUNCHER|AR|RANLIB|COMPILE_OBJECT|CREATE_SHARED_LIBRARY|CREATE_SHARED_MODULE|LINK_EXECUTABLE|ARCHIVE_CREATE|ARCHIVE_APPEND|ARCHIVE_FINISH)", name)):
            _preset_error("configure", c, "cacheVariables." + name,
                "variable changes toolchain/source/build identity or loads configure scripts outside the frozen preset subset")
        kind = None
        if isinstance(value, dict):
            if value.keys() - {"type", "value"}: _preset_error("configure", c, "cacheVariables." + name, "expected type/value only")
            kind, value = value.get("type"), value.get("value")
        if not (kind is None or isinstance(kind, str) and kind in {"BOOL", "STRING", "PATH", "FILEPATH", "INTERNAL"}): _preset_error("configure", c, "cacheVariables." + name, "unsupported cache type")
        if type(value) is bool:
            kind = kind or "BOOL"
            value = "TRUE" if value else "FALSE"
        value = _mission_expand(presets, value, "configure", configure, "cacheVariables." + name) if value != "" else ""
        if name == "CMAKE_MAKE_PROGRAM": validate_executable(value)
        cache.append((name, kind, value))
    configurations = {_mission_expand(presets, item["configuration"], kind, item, "configuration")
                      for kind, item in (("build", build), ("test", test)) if item.get("configuration")}
    if not multi:
        configurations.update(value for name, _, value in cache if name == "CMAKE_BUILD_TYPE" and value)
    if len(configurations) > 1: raise ForgePilotError("preset_no_match", "Configure, build and test configurations do not agree")
    configuration = next(iter(configurations), "")
    if config.configuration is not None and config.configuration != configuration:
        raise ForgePilotError("preset_override", "Host configuration does not match the selected preset configuration")
    targets = build.get("targets", [])
    targets = [targets] if isinstance(targets, str) else targets
    if not isinstance(targets, list): _preset_error("build", b, "targets", "expected a string or array")
    targets = tuple(_mission_expand(presets, value, "build", build, "targets") for value in targets)
    if any(value.startswith("-") for value in targets): _preset_error("build", b, "targets", "target names cannot begin with an option prefix")
    def jobs(value, kind, name, field):
        if value is not None and (type(value) is not int or not 1 <= value <= 1024): _preset_error(kind, name, field, "expected an integer between 1 and 1024")
        return value
    for name in ("verbose", "cleanFirst", "inheritConfigureEnvironment"):
        if name in build and type(build[name]) is not bool: _preset_error("build", b, name, "expected a boolean")
    if "inheritConfigureEnvironment" in test and type(test["inheritConfigureEnvironment"]) is not bool: _preset_error("test", t, "inheritConfigureEnvironment", "expected a boolean")
    output, execution = test.get("output", {}), test.get("execution", {})
    if not isinstance(output, dict) or output.keys() - {"outputOnFailure"}: _preset_error("test", t, "output", "only outputOnFailure is supported; report paths remain host-owned")
    if "outputOnFailure" in output and type(output["outputOnFailure"]) is not bool: _preset_error("test", t, "output.outputOnFailure", "expected a boolean")
    if not isinstance(execution, dict) or execution.keys() - {"jobs", "timeout", "noTestsAction", "stopOnFailure"}: _preset_error("test", t, "execution", "only jobs, timeout, noTestsAction=error and stopOnFailure=false are supported")
    if execution.get("noTestsAction", "error") != "error": _preset_error("test", t, "execution.noTestsAction", "all registered tests are required; only error is supported")
    if execution.get("stopOnFailure", False) is not False: _preset_error("test", t, "execution.stopOnFailure", "stopping early can omit registered tests")
    timeout = execution.get("timeout")
    if timeout is not None and (type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 3600): _preset_error("test", t, "execution.timeout", "expected seconds in (0, 3600]")
    binary_dir = (_mission_expand(presets, configure["binaryDir"], "configure", configure, "binaryDir")
                  if "binaryDir" in configure else None)
    if binary_dir:
        original = Path(binary_dir)
        if not original.is_absolute(): original = presets.root / original
        original_key = path_key(str(original).replace("\\", "/")).rstrip("/")
        for field, value in [("cacheVariables." + name, value) for name, _, value in cache] + [("targets", value) for value in targets]:
            if any(key in path_key(value.replace("\\", "/")) for key in {original_key, path_key(binary_dir.replace("\\", "/")).rstrip("/")} if key):
                _preset_error("configure" if field.startswith("cache") else "build", c if field.startswith("cache") else b, field,
                    "value refers to the original binaryDir; replacing it with the controlled task directory would change preset semantics")
    files, roots = presets.file_identity()
    selection = MissionBuildSelection("preset", c, b, t, generator, platform_value("architecture"), platform_value("toolset"),
        configuration, binary_dir,
        tuple(cache), targets, jobs(build.get("jobs"), "build", b, "jobs"), build.get("verbose", False), build.get("cleanFirst", False),
        jobs(execution.get("jobs"), "test", t, "execution.jobs"), timeout, output.get("outputOnFailure", False), files, roots)
    return MissionBuildSelection.from_dict(_freeze_tools(selection, config).to_dict())
