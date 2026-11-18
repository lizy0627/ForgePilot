"""Approval-bound test inventories and conservative anti-weakening checks.

Repository text is evidence only. Neither a patch, a test result nor a model's
TaskPlan can grant permission to edit an existing test.
"""

from collections import Counter
from dataclasses import dataclass
import hashlib
import os
from pathlib import PurePosixPath, PureWindowsPath
import re
import stat

from .errors import ForgePilotError
from .path_identity import path_key
from .file_access import pinned_file
from .repository import MAX_FILE_BYTES, MAX_FILES, MAX_SCAN_ENTRIES, excluded, is_link

MAX_INVENTORY_BYTES = 8_388_608
_TEXT_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx",
                  ".py", ".js", ".ts", ".cmake", ".json", ".txt", ".pro", ".pri"}
_ASSERTION = re.compile(
    r"\b(?:assert|static_assert|Q_ASSERT(?:_X)?|QVERIFY\w*|QCOMPARE\w*|QFAIL|"
    r"ASSERT_\w+|EXPECT_\w+|expect|REQUIRE\w*|CHECK\w*|TEST(?:_F|_P|_CASE|_SUITE)?|"
    r"assert\w+|pytest\s*\.\s*raises)\s*\(")
_SUPPRESSION = re.compile(
    r"\b(?:NDEBUG|QT_NO_DEBUG|DOCTEST_CONFIG_DISABLE|CATCH_CONFIG_DISABLE|"
    r"GTEST_SKIP|QSKIP|WILL_FAIL|DISABLED|PASS_REGULAR_EXPRESSION|"
    r"SKIP_RETURN_CODE|SKIP_REGULAR_EXPRESSION|TEST_INCLUDE_FILES|TEST_INCLUDE_FILE|"
    r"CTEST_CUSTOM_TESTS_IGNORE|CTEST_TEST_EXCLUDE|CTEST_TEST_ARGS)\b|"
    r"\bDISABLED_\w+|\bpytest\s*\.\s*(?:mark\s*\.\s*)?(?:skip\w*|xfail)|\bunittest\s*\.\s*(?:skip\w*|SkipTest)|"
    r"\b(?:skip|xfail|skipif)\s*\(|#\s*if\s+0\b|"
    r"#\s*(?:define|undef)\s+(?:assert|expect|ASSERT_\w+|EXPECT_\w+|CHECK\w*|REQUIRE\w*|QVERIFY\w*|QCOMPARE\w*)\b|"
    r"#\s*pragma\s+warning\s*\(\s*disable|"
    r"-Wno-[\w-]+|(?<!\w)-w\b|/W0\b|"
    r"\b(?:BUILD_TESTING|ENABLE_TESTS|BUILD_TESTS|ENABLE_TESTING)\s*(?::\w+)?\s*[= \t]\s*(?:OFF|FALSE|NO|0)\b|"
    r"\b(?:set|option)\s*\(\s*(?:BUILD_TESTING|ENABLE_TESTS|BUILD_TESTS|ENABLE_TESTING)\b[^)]*\b(?:OFF|FALSE|NO|0)\s*\)",
    re.I)
_BYPASS_CONTROL = re.compile(
    r"\b(?:return|goto|break|continue|throw|catch|try|except|finally)\b|"
    r"\b(?:exit|_Exit|quick_exit|longjmp|setjmp|abort)\s*\(|"
    r"\b(?:if|while|for)\s*\(|\b(?:if|while|for)\s+[^\n]+:", re.I)
_CMAKE_PROTECTED = {
    "add_test", "enable_testing", "include", "set_tests_properties", "set_property",
    "set", "option", "target_compile_options", "target_compile_definitions",
    "add_compile_options", "add_compile_definitions", "if", "elseif", "else", "endif",
    "return", "message", "add_subdirectory", "gtest_discover_tests", "gtest_add_tests",
    "catch_discover_tests", "add_custom_target", "add_custom_command",
    "foreach", "endforeach", "while", "endwhile", "function", "endfunction",
    "macro", "endmacro", "block", "endblock", "cmake_language",
}
_CMAKE_FLOW = {"if", "elseif", "else", "endif", "return", "foreach", "endforeach",
               "while", "endwhile", "function", "endfunction", "macro", "endmacro",
               "block", "endblock", "cmake_language"}


def _fail(message):
    raise ForgePilotError("validation_integrity", message)


def _comments(text, *, cmake=False):
    """Remove comments while preserving quoted literals and line locations."""
    pattern = (r'"(?:\\.|[^"\\])*"|\'[^\'\n]*\'|#\[=*\[[\s\S]*?\]=*\]|#[^\n]*'
               if cmake else r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*[\s\S]*?\*/|//[^\n]*|#[^\n]*')
    def replace(match):
        value = match.group()
        # C/C++ preprocessor directives are executable policy input, not comments.
        if value.startswith("#") and not cmake and re.match(r"#\s*(?:if|ifdef|ifndef|elif|else|endif|define|undef|pragma|include)\b", value):
            return value
        if value.startswith(('"', "'")):
            return value
        return " " + "\n" * value.count("\n")
    return re.sub(pattern, replace, text)


def _canonical(text):
    # Preserve literal contents; ignoring whitespace inside strings could weaken an assertion.
    return "\x1f".join(re.findall(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|\w+|[^\s]', text))


def _body_end(text, start):
    depth, quote, escaped = 1, None, False
    for index in range(start, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index + 1
    _fail("Unbalanced validation command cannot be verified")


def _assertions(text):
    text = _comments(text)
    calls = [_canonical(text[match.start():_body_end(text, match.end())])
             for match in _ASSERTION.finditer(text)]
    calls.extend(_canonical(match.group()) for match in re.finditer(r"(?m)^\s*assert\s+[^\n]+", text))
    return Counter(calls)


def _control_flow(text):
    text = _comments(text)
    result = []
    for match in _BYPASS_CONTROL.finditer(text):
        if match.group().endswith("("):
            result.append(_canonical(text[match.start():_body_end(text, match.end())]))
        else:
            end = re.search(r"[;\n]", text[match.start():])
            result.append(_canonical(text[match.start():match.start() + end.start()] if end else text[match.start():]))
    result.extend(_canonical(match.group()) for match in re.finditer(
        r"(?m)^\s*#\s*(?:if|ifdef|ifndef|elif|else)\b[^\n]*", text))
    return Counter(result)


def _cmake_commands(text):
    text = _comments(text, cmake=True)
    result = []
    # Advance past each command body: command-looking strings are never registrations.
    position = 0
    command = re.compile(r"\b([A-Za-z_][\w]*)\s*\(")
    while match := command.search(text, position):
        end = _body_end(text, match.end())
        name = match.group(1).casefold()
        body = text[match.end():end - 1]
        result.append((name, body, text.count("\n", 0, match.start()) + 1))
        position = end
    return result


def is_test_path(path):
    path = PurePosixPath(path.casefold())
    return (any(part in {"test", "tests", "testing"} for part in path.parts)
            or bool(re.search(r"(?:^test[_.-]|[_.-]tests?\.|^tst_|test\.cpp$)", path.name)))


def _is_cmake(path):
    path = PurePosixPath(path.casefold())
    return path.name == "cmakelists.txt" or path.suffix == ".cmake"


def _text(data):
    try:
        return data.decode("utf-8-sig") if b"\0" not in data else None
    except UnicodeError:
        return None


@dataclass(frozen=True, slots=True)
class AssertionUpdate:
    """One trusted, base-bound assertion correction in an existing public test."""
    path: str
    baseline_file_sha256: str
    old_assertion_sha256: str
    new_assertion_sha256: str


@dataclass(frozen=True, slots=True)
class ControlFlowAddition:
    """One known new loop/control signature in a pinned public test file."""
    path: str
    baseline_file_sha256: str
    kind: str
    signature_sha256: str


def _control_flow_preserved(old, new, *, path, baseline_bytes, additions, kind):
    if kind == "cmake":
        before = Counter((name, _canonical(body)) for name, body, _ in _cmake_commands(old)
                         if name in _CMAKE_FLOW)
        after = Counter((name, _canonical(body)) for name, body, _ in _cmake_commands(new)
                        if name in _CMAKE_FLOW)
        def digest(value):
            return hashlib.sha256((value[0] + "\0" + value[1]).encode()).hexdigest()
    else:
        before, after = _control_flow(old), _control_flow(new)
        def digest(value):
            return hashlib.sha256(value.encode()).hexdigest()
    if before - after:
        return False
    added = after - before
    if not added:
        return True
    baseline_hash = hashlib.sha256(baseline_bytes).hexdigest()
    for addition in additions:
        if (path_key(addition.path) != path_key(path) or addition.kind != kind
                or addition.baseline_file_sha256 != baseline_hash):
            continue
        matches = [signature for signature in added
                   if digest(signature) == addition.signature_sha256]
        if len(matches) == 1:
            added[matches[0]] -= 1
            if added[matches[0]] == 0:
                del added[matches[0]]
    return not added


def _assertions_preserved(old, new, *, path, baseline_bytes, updates):
    original = _assertions(old)
    current = _assertions(new)
    removed = original - current
    if not removed:
        return True
    added = current - original
    baseline_hash = hashlib.sha256(baseline_bytes).hexdigest()
    for update in updates:
        if (path_key(update.path) != path_key(path)
                or update.baseline_file_sha256 != baseline_hash):
            continue
        old_matches = [assertion for assertion in removed
                       if hashlib.sha256(assertion.encode()).hexdigest() == update.old_assertion_sha256]
        new_matches = [assertion for assertion in added
                       if hashlib.sha256(assertion.encode()).hexdigest() == update.new_assertion_sha256]
        if len(old_matches) == 1 and len(new_matches) == 1:
            removed[old_matches[0]] -= 1
            if removed[old_matches[0]] == 0:
                del removed[old_matches[0]]
    return not removed


@dataclass(frozen=True, slots=True)
class IntegrityTaskSpec:
    """Trusted adapter-owned exact permission request, never parsed from model plans."""
    task_type: str = "bug_fix"
    test_files_to_modify: tuple[str, ...] = ()
    assertion_updates: tuple[AssertionUpdate, ...] = ()
    control_flow_additions: tuple[ControlFlowAddition, ...] = ()

    def __post_init__(self):
        if self.task_type not in {"bug_fix", "feature", "maintenance"}:
            raise ValueError("Unknown task type")
        if type(self.test_files_to_modify) is not tuple or len(self.test_files_to_modify) > 100:
            raise ValueError("Test modification scope must be an immutable exact file list")
        if type(self.assertion_updates) is not tuple or len(self.assertion_updates) > 8:
            raise ValueError("Assertion updates must be a bounded immutable tuple")
        if type(self.control_flow_additions) is not tuple or len(self.control_flow_additions) > 8:
            raise ValueError("Control-flow additions must be a bounded immutable tuple")
        seen = set()
        for path in self.test_files_to_modify:
            if (not isinstance(path, str) or not path or len(path) > 1024
                    or path.startswith(("/", "\\")) or PureWindowsPath(path).drive
                    or any(ord(char) < 32 or char in ':*?[]<>"|' for char in path)
                    or ".." in path.replace("\\", "/").split("/")):
                raise ValueError("TaskSpec must list exact relative test files without wildcards")
            key = path_key(path)
            if key in seen:
                raise ValueError("Duplicate test file permission")
            seen.add(key)
        update_paths = set()
        for update in self.assertion_updates:
            if not isinstance(update, AssertionUpdate):
                raise ValueError("Assertion update must be a trusted structured rule")
            key = path_key(update.path)
            if key not in seen or key in update_paths:
                raise ValueError("Assertion update requires one exact approved test path")
            update_paths.add(key)
            if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in (
                    update.baseline_file_sha256, update.old_assertion_sha256,
                    update.new_assertion_sha256)):
                raise ValueError("Assertion update requires SHA-256 digests")
        flow_keys = set()
        for addition in self.control_flow_additions:
            if not isinstance(addition, ControlFlowAddition):
                raise ValueError("Control-flow addition must be a trusted structured rule")
            key = path_key(addition.path)
            flow_key = (key, addition.kind, addition.signature_sha256)
            if key not in seen or flow_key in flow_keys or addition.kind not in {"cpp", "cmake"}:
                raise ValueError("Control-flow addition requires one exact approved test path")
            flow_keys.add(flow_key)
            if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in (
                    addition.baseline_file_sha256, addition.signature_sha256)):
                raise ValueError("Control-flow addition requires SHA-256 digests")

    @classmethod
    def from_task_spec(cls, task_spec):
        # Older artifact schemas name this field 'patterns'; globs are deliberately rejected.
        return cls(task_spec.task_type, tuple(task_spec.test_policy.modifiable_existing_test_patterns))


@dataclass(frozen=True, slots=True)
class Registration:
    path: str
    line: int
    command: str
    signature: str
    name: str | None = None


@dataclass(frozen=True, slots=True)
class IntegritySnapshot:
    plan_version: int
    test_hashes: tuple[tuple[str, str], ...]
    ctest_names: tuple[str, ...]
    registrations: tuple[Registration, ...]
    test_count: int
    ctest_inventory_source: str


class IntegrityMonitor:
    def __init__(self, repository, task_spec=None):
        self.repository = repository
        self.task_spec = task_spec or IntegrityTaskSpec()
        if not isinstance(self.task_spec, IntegrityTaskSpec):
            self.task_spec = IntegrityTaskSpec.from_task_spec(self.task_spec)
        self.snapshot = None
        self._baseline = {}
        self._tests = set()
        self._approved = frozenset()
        self._ctest_bound = False

    def _scan(self):
        pending, files, scanned, total = ["."], {}, 0, 0
        while pending:
            directory = self.repository._path(pending.pop())
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        scanned += 1
                        if scanned > MAX_SCAN_ENTRIES:
                            _fail("Test inventory exceeds the directory-entry limit")
                        if excluded(entry.name):
                            continue
                        relative = (directory / entry.name).relative_to(self.repository.root).as_posix()
                        info = entry.stat(follow_symlinks=False)
                        if is_link(info):
                            # Never follow links; a substituted baseline file will be missing below.
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            pending.append(relative)
                        elif stat.S_ISREG(info.st_mode):
                            if len(files) >= MAX_FILES:
                                _fail("Test inventory exceeds the file-count limit")
                            protected_path = is_test_path(relative) or _is_cmake(relative)
                            if info.st_size > (MAX_FILE_BYTES if protected_path else MAX_INVENTORY_BYTES):
                                _fail("Complete integrity inventory requires files within the size limit")
                            with pinned_file(self.repository, relative) as (stream, _):
                                if stream is None:
                                    _fail("File disappeared while recording test inventory")
                                maximum = MAX_FILE_BYTES if protected_path else MAX_INVENTORY_BYTES
                                data = stream.read(min(maximum, MAX_INVENTORY_BYTES - total) + 1)
                                if len(data) > maximum:
                                    _fail("File grew beyond the integrity size limit")
                            if len(data) > MAX_FILE_BYTES and (text := _text(data)) is not None and _assertions(text):
                                _fail("Assertion-bearing files exceed the protected test size limit")
                            total += len(data)
                            if total > MAX_INVENTORY_BYTES:
                                _fail("Complete integrity inventory exceeds the total-byte limit")
                            files[relative] = data
            except OSError:
                _fail("Cannot record a complete safe test inventory")
        return files

    @staticmethod
    def _registrations(files):
        registrations = []
        for path, data in sorted(files.items()):
            text = _text(data)
            if not _is_cmake(path) or text is None:
                continue
            for command, body, line in _cmake_commands(text):
                if command not in _CMAKE_PROTECTED:
                    continue
                name = None
                if command == "add_test":
                    tokens = re.findall(r'"(?:\\.|[^"\\])*"|[^\s]+', body)
                    if tokens:
                        token = tokens[1] if tokens[0].casefold() == "name" and len(tokens) > 1 else tokens[0]
                        name = token.strip('"')
                        if "$" in name or ";" in name:
                            name = None  # Dynamic names require a trusted CTest inventory.
                registrations.append(Registration(path, line, command, _canonical(body), name))
        return tuple(registrations)

    def capture(self, plan_version, *, approved_test_files=(), ctest_names=None):
        if type(plan_version) is not int or plan_version < 1:
            raise ValueError("Integrity approval requires a plan version")
        if type(approved_test_files) not in {tuple, list, set, frozenset}:
            raise ValueError("Extra test approval requires an exact file list")
        approved = frozenset(path_key(self.repository.normalize_plan_path(path)) for path in approved_test_files)
        requested = frozenset(path_key(self.repository.normalize_plan_path(path))
                              for path in self.task_spec.test_files_to_modify)
        if approved - requested:
            _fail("Extra test approval must be restricted to exact TaskSpec files")
        if self.snapshot is not None:
            self.verify()
            if plan_version < self.snapshot.plan_version:
                _fail("Test permission cannot bind an older plan")
            self._approved = approved
            self.snapshot = IntegritySnapshot(plan_version, self.snapshot.test_hashes,
                self.snapshot.ctest_names, self.snapshot.registrations, self.snapshot.test_count,
                self.snapshot.ctest_inventory_source)
            return self.report()
        files = self._scan()
        tests = {path for path, data in files.items() if is_test_path(path) or (
            PurePosixPath(path).suffix.casefold() in _TEXT_SUFFIXES and
            (text := _text(data)) is not None and bool(_assertions(text)))}
        if approved - {path_key(path) for path in tests}:
            _fail("Extra test approval must name an existing inventoried test")
        registrations = self._registrations(files)
        names = self._names(ctest_names) if ctest_names is not None else tuple(
            sorted(item.name for item in registrations if item.name is not None))
        self._ctest_bound = ctest_names is not None
        self._baseline, self._tests, self._approved = files, tests, approved
        self.snapshot = IntegritySnapshot(plan_version, tuple((path, hashlib.sha256(files[path]).hexdigest())
            for path in sorted(tests)), names, registrations, len(names),
            "ctest" if self._ctest_bound else "source_registrations")
        return self.report()

    @staticmethod
    def _names(names):
        if (not isinstance(names, (tuple, list)) or len(names) > MAX_FILES
                or any(not isinstance(name, str) or not name or len(name) > 1024 for name in names)):
            _fail("Invalid or incomplete trusted CTest inventory")
        if len(set(names)) != len(names):
            _fail("CTest inventory contains duplicate test names")
        return tuple(sorted(names))

    def bind_ctest(self, names):
        """Bind a trusted *pre-modification* CTest enumeration, never a model result."""
        if self.snapshot is None or self._ctest_bound:
            _fail("CTest baseline must bind exactly once after approval capture")
        self.verify()  # Cannot bless a modified tree as the original test inventory.
        if self._scan() != self._baseline:
            _fail("CTest baseline must be recorded before repository changes")
        names = self._names(names)
        self.snapshot = IntegritySnapshot(self.snapshot.plan_version, self.snapshot.test_hashes,
            names, self.snapshot.registrations, len(names), "ctest")
        self._ctest_bound = True

    def guard(self, path, current, content):
        if self.snapshot is None:
            _fail("Test inventory must be recorded at approval before writes")
        old = _text(current or b"")
        if old is None:
            _fail("Binary validation inputs are immutable")
        new = content.removeprefix("\ufeff")
        if old == new:
            return
        key = path_key(path)
        protected = key in {path_key(test) for test in self._tests}
        existed_at_approval = key in {path_key(item) for item in self._baseline}
        if current is not None and existed_at_approval and (protected or is_test_path(path) or _assertions(old)):
            if key not in self._approved:
                _fail("Existing tests are read-only; exact TaskSpec and separate test approval are required")
            if not _assertions_preserved(old, new, path=path,
                                         baseline_bytes=self._baseline.get(path, b""),
                                         updates=self.task_spec.assertion_updates):
                _fail("Removing or weakening an existing assertion is forbidden")
            if not _is_cmake(path) and not _control_flow_preserved(
                    old, new, path=path, baseline_bytes=self._baseline.get(path, b""),
                    additions=self.task_spec.control_flow_additions, kind="cpp"):
                _fail("Changed control flow or early exits in existing tests cannot be verified safely")
        cmake = _is_cmake(path)
        if Counter(match.group().casefold() for match in _SUPPRESSION.finditer(_comments(new, cmake=cmake))) - Counter(
                match.group().casefold() for match in _SUPPRESSION.finditer(_comments(old, cmake=cmake))):
            _fail("Adding skip, failure suppression or compiler/test bypass is forbidden")
        if cmake:
            old_commands = Counter((name, _canonical(body)) for name, body, _ in _cmake_commands(old)
                                   if name in _CMAKE_PROTECTED)
            new_commands = Counter((name, _canonical(body)) for name, body, _ in _cmake_commands(new)
                                   if name in _CMAKE_PROTECTED)
            if old_commands - new_commands:
                _fail("Existing CTest registrations, control flow and compilation options must be preserved")
            if not _control_flow_preserved(
                    old, new, path=path, baseline_bytes=self._baseline.get(path, b""),
                    additions=self.task_spec.control_flow_additions, kind="cmake"):
                _fail("New CMake control flow may bypass existing test registration")

    def verify(self, *, ctest_names=None):
        if self.snapshot is None:
            _fail("Approval test inventory is missing")
        current = self._scan()
        for path in self._tests:
            if path not in current:
                _fail("Existing test was deleted, renamed, linked or made inaccessible")
            if current[path] != self._baseline[path] and path_key(path) not in self._approved:
                _fail("Existing test hash changed without separate exact approval")
        for path, data in current.items():
            old = self._baseline.get(path)
            if old == data:
                continue
            content = _text(data)
            if content is not None:
                self.guard(path, old, content)
            elif path in self._tests:
                _fail("Existing tests cannot be replaced with binary content")
        # A deleted CMake file must not silently remove a registration or compiler check.
        before = Counter((item.path, item.command, item.signature) for item in self.snapshot.registrations)
        after = Counter((item.path, item.command, item.signature) for item in self._registrations(current))
        if before - after:
            _fail("CTest registration or compilation controls disappeared or moved")
        if ctest_names is not None:
            names = self._names(ctest_names)
            if Counter(self.snapshot.ctest_names) - Counter(names) or len(names) < self.snapshot.test_count:
                _fail("CTest tests were deleted, renamed, filtered or reduced")
        return self.report() | {"status": "passed"}

    def report(self):
        if self.snapshot is None:
            return {"status": "not_recorded"}
        snapshot = self.snapshot
        return {"status": "recorded", "plan_version": snapshot.plan_version,
            "test_files": [{"path": path, "sha256": digest} for path, digest in snapshot.test_hashes],
            "ctest_names": list(snapshot.ctest_names), "test_count": snapshot.test_count,
            "ctest_inventory_source": snapshot.ctest_inventory_source,
            "registrations": [{"path": item.path, "line": item.line, "command": item.command,
                               "signature": item.signature, "name": item.name} for item in snapshot.registrations],
            "approved_test_files": sorted(self._approved)}
