"""Resolve the small set of build-selection fields; CMake validates preset availability."""

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re

from .errors import ForgePilotError
from .repository import excluded, is_link


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
        if not resolved.is_relative_to(root) or resolved != candidate:
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
    def __init__(self, root: Path, env: dict[str, str]):
        self.root = root
        self.env = env
        self.files: dict[Path, str] = {}
        self.raw = {kind: {} for kind in ("configure", "build", "test")}
        self.present = (root / "CMakePresets.json").exists() or (root / "CMakeUserPresets.json").exists()
        for name in ("CMakePresets.json", "CMakeUserPresets.json"):
            if (root / name).exists():
                self._load(root / name, set())

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

    def _load(self, path: Path, visiting: set[Path]):
        try:
            relative = path.relative_to(self.root)
            current = self.root
            for part in relative.parts:
                if part == "..":
                    raise ValueError()
                current /= part
                if is_link(current.lstat()):
                    raise ValueError()
            path = path.resolve(strict=True)
            if path in visiting or len(self.files) >= 64:
                raise ValueError()
            if path in self.files:
                return
            data = path.read_bytes()
            if len(data) > 1_048_576:
                raise ValueError()
            document = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_object)
            if not isinstance(document, dict) or type(document.get("version")) is not int:
                raise ValueError()
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
                self._load(include_path if include_path.is_absolute() else path.parent / include_path, visiting | {path})
            for kind in self.raw:
                for preset in document.get(kind + "Presets", []):
                    name = preset["name"]
                    if not isinstance(name, str) or name in self.raw[kind]:
                        raise ValueError()
                    self.raw[kind][name] = {**preset, "_file": path}
            self.files[path] = hashlib.sha256(data).hexdigest()
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
        current_names = {self.root / name for name in ("CMakePresets.json", "CMakeUserPresets.json") if (self.root / name).exists()}
        original_names = {p for p in self.files if p.parent == self.root and p.name in {"CMakePresets.json", "CMakeUserPresets.json"}}
        try:
            if current_names != original_names:
                raise ValueError()
            for path, expected in self.files.items():
                current = self.root
                for part in path.relative_to(self.root).parts:
                    current /= part
                    if is_link(current.lstat()):
                        raise ValueError()
                if path.resolve() != path or path.stat().st_size > 1_048_576 or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
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
