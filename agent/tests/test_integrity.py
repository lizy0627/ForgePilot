from dataclasses import FrozenInstanceError
from contextlib import contextmanager
import hashlib
from io import BytesIO
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import forgepilot.integrity as integrity
from forgepilot.errors import ForgePilotError
from forgepilot.integrity import IntegrityMonitor, IntegrityTaskSpec
from forgepilot.repository import Repository


@pytest.fixture
def inventory(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/check.cpp").write_bytes(b'void check(){ EXPECT_EQ(answer(), 42); }\r\n')
    (tmp_path / "main.cpp").write_bytes(b'int answer(){ return 42; }\n')
    (tmp_path / "CMakeLists.txt").write_text(
        'enable_testing()\nadd_executable(check tests/check.cpp)\n'
        'add_test(\n NAME check\n COMMAND check\n)\n', encoding="utf-8")
    repo = Repository(tmp_path.resolve(), "rg")
    monitor = IntegrityMonitor(repo)
    return tmp_path, repo, monitor


def test_capture_hashes_names_registration_and_counts(inventory):
    root, _, monitor = inventory
    result = monitor.capture(1)
    assert result["ctest_names"] == ["check"]
    assert result["test_count"] == 1
    assert result["test_files"] == [{"path": "tests/check.cpp", "sha256": hashlib.sha256(
        (root / "tests/check.cpp").read_bytes()).hexdigest()}]
    add_test = next(item for item in result["registrations"] if item["command"] == "add_test")
    assert add_test["path"] == "CMakeLists.txt" and add_test["line"] == 3
    assert monitor.verify()["status"] == "passed"
    with pytest.raises(FrozenInstanceError):
        monitor.snapshot.plan_version = 99


def test_custom_expect_assertion_cannot_be_weakened_with_test_approval(tmp_path):
    (tmp_path / "tests").mkdir()
    test = tmp_path / "tests/check.cpp"
    test.write_text("void check() { expect(answer() == 42); }\n", encoding="utf-8")
    (tmp_path / "CMakeLists.txt").write_text(
        "enable_testing()\nadd_test(NAME check COMMAND check)\n", encoding="utf-8")
    monitor = IntegrityMonitor(Repository(tmp_path.resolve(), "rg"),
                               IntegrityTaskSpec("bug_fix", ("tests/check.cpp",)))
    monitor.capture(1, approved_test_files=("tests/check.cpp",))
    test.write_text("void check() { expect(true); }\n", encoding="utf-8")
    with pytest.raises(ForgePilotError, match="assertion"):
        monitor.verify()


@pytest.mark.parametrize("task_type", ["bug_fix", "feature", "maintenance"])
def test_existing_tests_require_separate_approval_even_with_spec(inventory, task_type):
    _, repo, _ = inventory
    monitor = IntegrityMonitor(repo, IntegrityTaskSpec(task_type, ("tests/check.cpp",)))
    monitor.capture(1)
    with pytest.raises(ForgePilotError, match="read-only"):
        monitor.guard("tests/check.cpp", b"EXPECT_EQ(x,42);", "EXPECT_EQ(x,42);\nEXPECT_EQ(y,7);")


@pytest.mark.parametrize("path", ["tests/*.cpp", "tests/[ab].cpp", "../test.cpp", "D:/test.cpp",
                                 "\\\\server\\test.cpp", "/test.cpp", "a\0.cpp", "x:stream", "tests/**"])
def test_task_spec_only_accepts_exact_relative_file_paths(path):
    with pytest.raises(ValueError):
        IntegrityTaskSpec(test_files_to_modify=(path,))


def test_exact_approval_and_safe_strengthening(inventory):
    root, repo, _ = inventory
    spec = IntegrityTaskSpec("feature", ("tests/check.cpp",))
    monitor = IntegrityMonitor(repo, spec)
    with pytest.raises(ForgePilotError, match="exact TaskSpec"):
        monitor.capture(1, approved_test_files=("main.cpp",))
    monitor.capture(1, approved_test_files=("tests/check.cpp",))
    old = (root / "tests/check.cpp").read_bytes()
    new = old.decode() + "EXPECT_NE(answer(), 0);\n"
    monitor.guard("tests/check.cpp", old, new)
    (root / "tests/check.cpp").write_text(new, encoding="utf-8")
    assert monitor.verify()["status"] == "passed"
    # Reapproval records a new binding, never a refreshed baseline hash.
    hashes = monitor.report()["test_files"]
    monitor.capture(2, approved_test_files=("tests/check.cpp",))
    assert monitor.report()["test_files"] == hashes
    with pytest.raises(ForgePilotError, match="older plan"):
        monitor.capture(1)


@pytest.mark.parametrize("old,new", [
    ("EXPECT_EQ(value, 42);", "EXPECT_TRUE(true);"),
    ("assert(x == 42);", "assert(true);"),
    ("assert(value == 'a b');", "assert(value == 'ab');"),
    ("assert value == 42\n", "assert True\n"),
    ("self.assertEqual(value,42)", "self.assertEqual(value,value)"),
    ("EXPECT_EQ(x,42);", "return; EXPECT_EQ(x,42);"),
    ("EXPECT_EQ(x,42);", "if (false) { EXPECT_EQ(x,42); }"),
    ("if (ready) { EXPECT_EQ(x,42); }", "if (false) { EXPECT_EQ(x,42); }"),
    ("EXPECT_EQ(x,42);", "#ifdef NEVER\nEXPECT_EQ(x,42);\n#endif"),
    ("EXPECT_EQ(x,42);", "#define EXPECT_EQ(x,y)\nEXPECT_EQ(x,42);"),
    ("EXPECT_EQ(x,42);", "try { EXPECT_EQ(x,42); } catch (...) {}"),
])
def test_explicit_approval_still_cannot_weaken_assertions_or_control(inventory, old, new):
    _, repo, _ = inventory
    monitor = IntegrityMonitor(repo, IntegrityTaskSpec(test_files_to_modify=("tests/check.cpp",)))
    monitor.capture(1, approved_test_files=("tests/check.cpp",))
    with pytest.raises(ForgePilotError, match="validation_integrity"):
        monitor.guard("tests/check.cpp", old.encode(), new)


@pytest.mark.parametrize("path,addition", [
    ("tests/new.cpp", "GTEST_SKIP();"), ("tests/new.cpp", "QSKIP(\"flaky\");"),
    ("tests/new.py", "@pytest.mark.skip()\ndef test_x(): pass"),
    ("tests/new.py", "@pytest.mark.skip\ndef test_x(): pass"),
    ("tests/new.py", "pytest.xfail('expected')"),
    ("tests/new.py", "@unittest.skipIf(True, 'skip')\ndef test_x(): pass"),
    ("main.cpp", "#define NDEBUG\n"), ("main.cpp", "#pragma warning(disable:123)\n"),
    ("CMakeLists.txt", "set(BUILD_TESTING OFF)"),
    ("CMakeLists.txt", 'option(ENABLE_TESTS "tests" FALSE)'),
    ("CMakeLists.txt", "add_compile_definitions(NDEBUG)"),
    ("CMakeLists.txt", "set_tests_properties(check PROPERTIES DISABLED TRUE)"),
    ("CMakeLists.txt", "set_tests_properties(check PROPERTIES WILL_FAIL TRUE)"),
    ("CMakeLists.txt", "set_tests_properties(check PROPERTIES SKIP_RETURN_CODE 0)"),
    ("CMakeLists.txt", "add_compile_options(-w)"),
    ("CMakeLists.txt", "add_compile_options(/W0)"),
    ("tests/new.cpp", "TEST(Foo, DISABLED_Bar) {}"),
    ("CMakeLists.txt", "set(CTEST_CUSTOM_TESTS_IGNORE check)"),
])
def test_new_skip_suppression_and_compile_bypass_rejected(inventory, path, addition):
    _, _, monitor = inventory
    monitor.capture(1)
    with pytest.raises(ForgePilotError, match="validation_integrity"):
        monitor.guard(path, None, addition)


@pytest.mark.parametrize("operation", ["delete", "rename", "modify", "binary"])
def test_final_hash_detects_out_of_band_test_changes(inventory, operation):
    root, _, monitor = inventory
    monitor.capture(1)
    target = root / "tests/check.cpp"
    if operation == "delete":
        target.unlink()
    elif operation == "rename":
        target.rename(root / "tests/renamed.cpp")
    elif operation == "modify":
        target.write_bytes(b"// removed assertions")
    else:
        target.write_bytes(b"\x00")
    with pytest.raises(ForgePilotError, match="validation_integrity"):
        monitor.verify()


@pytest.mark.parametrize("replacement", ["", "# add_test(NAME check COMMAND check)",
    'message("add_test(NAME check COMMAND check)")', "add_test(NAME check COMMAND always_success)",
    "if(FALSE)\nadd_test(NAME check COMMAND check)\nendif()"])
def test_registration_deletion_comment_forgery_or_replacement_rejected(inventory, replacement):
    root, _, monitor = inventory
    monitor.capture(1)
    path = root / "CMakeLists.txt"
    path.write_text(replacement, encoding="utf-8")
    with pytest.raises(ForgePilotError, match="validation_integrity"):
        monitor.verify()


def test_registration_addition_and_ordinary_source_change_allowed(inventory):
    root, _, monitor = inventory
    monitor.capture(1)
    with (root / "CMakeLists.txt").open("a", encoding="utf-8") as stream:
        stream.write("\nadd_test(NAME regression COMMAND check --regression)\n")
    (root / "main.cpp").write_text("int answer(){ return 43; }\n", encoding="utf-8")
    (root / "tests/new.cpp").write_text("EXPECT_EQ(answer(), 43);\n", encoding="utf-8")
    assert monitor.verify(ctest_names=["check", "regression"])["status"] == "passed"


def test_task_created_regression_test_can_be_refined(inventory):
    _, _, monitor = inventory
    monitor.capture(1)
    monitor.guard("tests/regression.cpp", None, "EXPECT_EQ(answer(), 0);")
    monitor.guard("tests/regression.cpp", b"EXPECT_EQ(answer(), 0);", "EXPECT_EQ(answer(), 42);")
    with pytest.raises(ForgePilotError, match="suppression"):
        monitor.guard("tests/regression.cpp", b"EXPECT_EQ(answer(), 0);", "GTEST_SKIP();")


def test_cmake_wrapper_cannot_disable_preserved_registration(inventory):
    root, _, monitor = inventory
    monitor.capture(1)
    path = root / "CMakeLists.txt"
    original = path.read_text(encoding="utf-8")
    path.write_text("if(FALSE)\n" + original + "\nendif()", encoding="utf-8")
    with pytest.raises(ForgePilotError, match="CMake control flow"):
        monitor.verify()


@pytest.mark.parametrize("names", [[], ["renamed"], ["check"]])
def test_trusted_ctest_inventory_cannot_be_reduced(inventory, names):
    _, _, monitor = inventory
    monitor.capture(1, ctest_names=["check", "generated_parameter_case"])
    with pytest.raises(ForgePilotError, match="deleted, renamed, filtered or reduced"):
        monitor.verify(ctest_names=names)


def test_bind_ctest_before_changes_only(inventory):
    root, _, monitor = inventory
    monitor.capture(1)
    monitor.bind_ctest(["check", "generated"])
    assert monitor.report()["ctest_inventory_source"] == "ctest"
    with pytest.raises(ForgePilotError, match="exactly once"):
        monitor.bind_ctest(["check"])
    second = IntegrityMonitor(monitor.repository)
    second.capture(1)
    (root / "main.cpp").write_text("changed", encoding="utf-8")
    with pytest.raises(ForgePilotError, match="before repository changes"):
        second.bind_ctest(["check"])


def test_assertion_file_outside_tests_is_protected(inventory):
    root, repo, _ = inventory
    (root / "expectation.cpp").write_text("assert(x == 42);", encoding="utf-8")
    monitor = IntegrityMonitor(repo)
    assert len(monitor.capture(1)["test_files"]) == 2
    with pytest.raises(ForgePilotError, match="read-only"):
        monitor.guard("expectation.cpp", b"assert(x == 42);", "assert(true);")


def test_binary_fixtures_are_hashed_and_immutable(inventory):
    root, _, monitor = inventory
    (root / "tests/input.bin").write_bytes(b"\0\xff\x01")
    assert len(monitor.capture(1)["test_files"]) == 2
    (root / "tests/input.bin").write_bytes(b"\0\xff\x02")
    with pytest.raises(ForgePilotError, match="hash changed"):
        monitor.verify()


def test_junction_test_replacement_never_followed(inventory, tmp_path):
    root, _, monitor = inventory
    monitor.capture(1)
    (root / "tests/check.cpp").unlink()
    outside = root / "outside.cpp"
    outside.write_text("EXPECT_EQ(answer(), 42);", encoding="utf-8")
    try:
        (root / "tests/check.cpp").symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks requires Windows developer mode or privilege")
    with pytest.raises(ForgePilotError, match="deleted, renamed, linked"):
        monitor.verify()


@pytest.mark.parametrize("limit", ["MAX_SCAN_ENTRIES", "MAX_FILES", "MAX_INVENTORY_BYTES", "MAX_FILE_BYTES"])
def test_inventory_limits_fail_closed(inventory, monkeypatch, limit):
    _, _, monitor = inventory
    monkeypatch.setattr(integrity, limit, 0)
    with pytest.raises(ForgePilotError, match="validation_integrity"):
        monitor.capture(1)


def test_invalid_grants_and_missing_inventory(inventory):
    _, _, monitor = inventory
    for call in [lambda: monitor.guard("main.cpp", b"x", "y"), lambda: monitor.verify(),
                 lambda: monitor.bind_ctest(["check"])]:
        with pytest.raises(ForgePilotError, match="validation_integrity"):
            call()
    with pytest.raises(ValueError):
        monitor.capture(0)
    with pytest.raises(ValueError):
        monitor.capture(1, approved_test_files="tests/check.cpp")
    with pytest.raises(ForgePilotError, match="duplicate test names"):
        monitor.capture(1, ctest_names=["check", "check"])
    with pytest.raises(ForgePilotError, match="Invalid or incomplete"):
        monitor.capture(1, ctest_names=[""])


def test_comments_and_unchanged_utf8_bom_do_not_grant_or_revoke_access(inventory):
    root, _, monitor = inventory
    monitor.capture(1)
    data = (root / "tests/check.cpp").read_bytes()
    monitor.guard("tests/check.cpp", b"\xef\xbb\xbf" + data, "\ufeff" + data.decode())
    monitor.guard("main.cpp", b"int x;", 'int x; // GTEST_SKIP(); NDEBUG\n')
    monitor.guard("CMakeLists.txt", b"", '# set(BUILD_TESTING OFF)\n')
    assert monitor.report()["approved_test_files"] == []


def test_no_reapproval_can_bless_changed_unapproved_test(inventory):
    root, _, monitor = inventory
    monitor.capture(1)
    (root / "tests/check.cpp").write_text("assert(true);", encoding="utf-8")
    with pytest.raises(ForgePilotError, match="hash changed"):
        monitor.capture(2)


def test_task_spec_is_immutable_and_allows_windows_spaces():
    spec = IntegrityTaskSpec("feature", ("tests/带 空格.cpp",))
    with pytest.raises(FrozenInstanceError):
        spec.task_type = "maintenance"
    for kwargs in [{"task_type": "unknown"}, {"test_files_to_modify": ["tests/a.cpp"]},
                   {"test_files_to_modify": ("tests/a.cpp", "TESTS/A.cpp")}]:
        with pytest.raises(ValueError):
            IntegrityTaskSpec(**kwargs)


def test_artifact_task_spec_adapter_rejects_globs(inventory):
    _, repo, _ = inventory
    artifact = SimpleNamespace(task_type="feature", test_policy=SimpleNamespace(
        modifiable_existing_test_patterns=("tests/check.cpp",)))
    monitor = IntegrityMonitor(repo, artifact)
    assert monitor.task_spec.test_files_to_modify == ("tests/check.cpp",)
    artifact.test_policy.modifiable_existing_test_patterns = ("tests/*.cpp",)
    with pytest.raises(ValueError, match="exact relative"):
        IntegrityMonitor(repo, artifact)


def test_unbalanced_and_escaped_assertion_cannot_weaken(inventory):
    _, repo, _ = inventory
    monitor = IntegrityMonitor(repo, IntegrityTaskSpec(test_files_to_modify=("tests/check.cpp",)))
    monitor.capture(1, approved_test_files=("tests/check.cpp",))
    old = 'EXPECT_EQ(value, "quoted\\\" (value)");'
    with pytest.raises(ForgePilotError, match="weakening"):
        monitor.guard("tests/check.cpp", old.encode(), 'EXPECT_EQ(value,"other");')
    with pytest.raises(ForgePilotError, match="Unbalanced"):
        monitor.guard("tests/check.cpp", old.encode(), 'EXPECT_EQ(value,"other";')


def test_unknown_binary_source_is_inventoried_without_decoding(inventory):
    root, _, monitor = inventory
    (root / "blob.bin").write_bytes(b"\xff")
    assert monitor.capture(1)["test_count"] == 1
    (root / "blob.bin").write_bytes(b"\xfe")
    assert monitor.verify()["status"] == "passed"
    with pytest.raises(ForgePilotError, match="Binary validation inputs"):
        monitor.guard("blob.bin", b"\xff", "text")


def test_excluded_paths_and_reparse_points_omitted(inventory, monkeypatch):
    root, _, monitor = inventory
    (root / ".git").mkdir()
    (root / ".git/secret").write_bytes(b"private")
    (root / "protected-oracle").mkdir()
    (root / "ordinary.txt").write_text("link substitution", encoding="utf-8")
    target_size = (root / "ordinary.txt").stat().st_size
    original = integrity.is_link
    monkeypatch.setattr(integrity, "is_link", lambda info: info.st_size == target_size or original(info))
    monitor.capture(1)
    assert "ordinary.txt" not in monitor._baseline
    assert not any(path.startswith(".git/") for path in monitor._baseline)


def test_large_ordinary_file_allowed_but_assertion_source_fails(inventory):
    root, repo, monitor = inventory
    (root / "huge.cpp").write_bytes(b" " * (integrity.MAX_FILE_BYTES + 1))
    assert monitor.capture(1)["test_count"] == 1
    (root / "huge.cpp").write_bytes(b"assert(x);" + b" " * integrity.MAX_FILE_BYTES)
    with pytest.raises(ForgePilotError, match="Assertion-bearing"):
        IntegrityMonitor(repo).capture(1)


@pytest.mark.parametrize("mode", ["missing", "grew"])
def test_inventory_toctou_cannot_bless_disappeared_or_growing_file(inventory, monkeypatch, mode):
    _, _, monitor = inventory
    original = integrity.pinned_file
    @contextmanager
    def racing(repository, relative):
        if relative == "tests/check.cpp":
            yield (None if mode == "missing" else BytesIO(b"x" * (integrity.MAX_FILE_BYTES + 1))), False
        else:
            with original(repository, relative) as result:
                yield result
    monkeypatch.setattr(integrity, "pinned_file", racing)
    with pytest.raises(ForgePilotError, match="disappeared|grew"):
        monitor.capture(1)
    assert monitor.report() == {"status": "not_recorded"}


def test_scan_io_error_is_fail_closed(inventory, monkeypatch):
    _, _, monitor = inventory
    monkeypatch.setattr(integrity.os, "scandir", MagicMock(side_effect=OSError("race")))
    with pytest.raises(ForgePilotError, match="complete safe test inventory"):
        monitor.capture(1)


def test_unusual_directory_entry_is_not_opened(inventory, monkeypatch):
    _, _, monitor = inventory
    entry = SimpleNamespace(name="socket", stat=lambda **_: SimpleNamespace(st_mode=0, st_file_attributes=0))
    iterator = MagicMock()
    iterator.__enter__.return_value = [entry]
    monkeypatch.setattr(integrity.os, "scandir", lambda _: iterator)
    assert monitor.capture(1)["test_count"] == 0


def test_dynamic_and_empty_add_test_recorded_without_forged_names(inventory):
    root, _, monitor = inventory
    with (root / "CMakeLists.txt").open("a", encoding="utf-8") as stream:
        stream.write('add_test(NAME "${dynamic}" COMMAND check)\nadd_test()\n')
    report = monitor.capture(1)
    assert report["ctest_names"] == ["check"]
    assert len([item for item in report["registrations"] if item["command"] == "add_test"]) == 3


def test_exact_grant_cannot_target_ordinary_file(inventory):
    _, repo, _ = inventory
    monitor = IntegrityMonitor(repo, IntegrityTaskSpec(test_files_to_modify=("main.cpp",)))
    with pytest.raises(ForgePilotError, match="existing inventoried test"):
        monitor.capture(1, approved_test_files=("main.cpp",))


def test_even_approved_test_cannot_become_binary(inventory):
    root, repo, _ = inventory
    monitor = IntegrityMonitor(repo, IntegrityTaskSpec(test_files_to_modify=("tests/check.cpp",)))
    monitor.capture(1, approved_test_files=("tests/check.cpp",))
    (root / "tests/check.cpp").write_bytes(b"\0")
    with pytest.raises(ForgePilotError, match="binary content"):
        monitor.verify()


def test_deleting_whole_registration_file_detected(inventory):
    root, _, monitor = inventory
    monitor.capture(1)
    (root / "CMakeLists.txt").unlink()
    with pytest.raises(ForgePilotError, match="disappeared or moved"):
        monitor.verify()


@pytest.mark.parametrize("flow", ["foreach(x IN ITEMS ignored)", "while(FALSE)", "function(hold_tests)",
                                  "macro(hold_tests)", 'cmake_language(EVAL CODE "")'])
def test_new_cmake_loop_function_or_eval_cannot_hide_registration(inventory, flow):
    root, _, monitor = inventory
    monitor.capture(1)
    old = (root / "CMakeLists.txt").read_bytes()
    with pytest.raises(ForgePilotError, match="CMake control flow"):
        monitor.guard("CMakeLists.txt", old, flow + "\n" + old.decode())
