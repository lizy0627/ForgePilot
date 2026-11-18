"""Run the four original Windows isolation/real-symlink checks offline."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import traceback
import xml.etree.ElementTree as ET

from offline_cmake_runtime import copy_cmake_runtime
from offline_symlink_probe import probe_symlinks
from offline_validation_tools import load_toolchain


ROOT = Path(__file__).resolve().parents[1]
TESTS = (
    "tests/test_oracle_isolation_attack.py::test_candidate_executable_domain_denies_external_file_and_loopback",
    "tests/test_oracle_isolation_attack.py::test_candidate_configure_and_build_in_restricted_domain",
    "tests/test_integrity.py::test_junction_test_replacement_never_followed",
    "tests/test_real_manifest.py::test_init_rejects_junction_parent_resolving_into_project",
)
LINK_SKIPS = {
    TESTS[2]: ("file", "Creating symlinks requires Windows developer mode or privilege"),
    TESTS[3]: ("directory", "Creating symlinks requires elevated Windows privileges"),
}


def save_json(path, value):
    with path.open("x", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write("\n")


def remove_scratch(root, identity):
    # Check the directory we created, then every descendant before recursive
    # deletion. An original symlink test can legitimately leave an in-tree link.
    if root.resolve(strict=True) != root or not os.path.samestat(root.stat(), identity):
        raise ValueError(f"Scratch directory identity changed: {root}")
    pending = [root]
    while pending:
        for child in pending.pop().iterdir():
            actual = child.resolve(strict=True)
            if not actual.is_relative_to(root):
                raise ValueError(f"Scratch entry resolves outside the run: {child}")
            metadata = child.lstat()
            if getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                if not child.is_symlink():
                    raise ValueError(f"Unexpected reparse point in scratch directory: {child}")
            elif stat.S_ISDIR(metadata.st_mode):
                pending.append(child)
    shutil.rmtree(root)


def check_results(junit, capabilities):
    """Require exactly the requested node IDs, including both original link cases."""
    actual = {}
    for case in ET.parse(junit).iter("testcase"):
        filename = case.attrib["classname"].replace(".", "/") + ".py"
        node = filename + "::" + case.attrib["name"]
        if node not in TESTS or node in actual:
            raise ValueError(f"Unexpected or repeated testcase: {node}")
        if case.find("error") is not None or case.find("failure") is not None:
            actual[node] = "failed"
            continue
        skip = case.find("skipped")
        expected_gap = False
        if node in LINK_SKIPS:
            kind, message = LINK_SKIPS[node]
            expected_gap = capabilities[kind]["status"] == "permission_denied"
            if expected_gap and skip is not None and skip.get("message") == message:
                actual[node] = "permission_gap"
                continue
        actual[node] = "passed" if skip is None and not expected_gap else "unexpected_result"
    if set(actual) != set(TESTS):
        raise ValueError(f"Missing testcases: {sorted(set(TESTS) - set(actual))}")
    return actual


def save_build_evidence(base, evidence):
    # Copy known evaluator reports only, after all test children have exited.
    if not base.exists():
        return
    if base.resolve(strict=True) != base:
        raise ValueError(f"Unexpected pytest scratch directory: {base}")
    reports = ["candidate/build/Testing/Temporary/LastTest.log",
               "graded-candidate/public-build/public.xml", "graded-candidate/oracle-build/oracle.xml",
               "graded-candidate/probe-commands.json", "graded-candidate/probe-challenge.json"]
    for phase in ("configure", "public_build", "public_test", "oracle_configure", "oracle_build",
                  "oracle_test", "probe-configure", "probe-build", "probe-execute"):
        reports.extend(f"graded-candidate/{phase}.{stream}" for stream in ("stdout", "stderr"))
    for directory in base.iterdir():
        # pytest may add a <testname>current alias alongside the real directory.
        if directory.resolve(strict=True) != directory or not directory.is_dir():
            continue
        for report in reports:
            path = directory / report
            if path != path.resolve(strict=False):
                raise ValueError(f"Unsafe report file: {path}")
            if not path.exists():
                continue
            if path.stat().st_nlink != 1 or not path.is_file():
                raise ValueError(f"Unsafe report file: {path}")
            target = evidence / "build" / path.relative_to(base)
            target.parent.mkdir(parents=True, exist_ok=True)
            with path.open("rb") as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)


def run(evidence, scratch, report):
    agent = ROOT / "agent"
    # Several source checkouts may share one installed venv. Require the
    # original tests in this checkout before preparing any native tools.
    files = sorted({node.split("::")[0] for node in TESTS})
    save_json(evidence / "test-sources.json", {
        name: hashlib.sha256((agent / name).read_bytes()).hexdigest() for name in files
    })
    env, tools = load_toolchain(ROOT)
    save_json(evidence / "tools.json", tools)
    runtime = copy_cmake_runtime(scratch, Path(tools["cmake"]["path"]), Path(tools["ctest"]["path"]))
    save_json(evidence / "cmake-runtime.json", runtime)
    private_bin = Path(runtime["bin"])
    if private_bin.parent.parent != scratch or private_bin.resolve(strict=True) != private_bin:
        raise ValueError("CMake runtime is outside the new scratch directory")
    for name in ("cmake", "ctest"):
        if runtime[name + "_sha256"] != tools[name]["sha256"]:
            raise ValueError(f"{name} changed after toolchain inspection")
    env["PATH"] = str(private_bin) + os.pathsep + env["PATH"]
    child_tmp = scratch / "temp"
    child_tmp.mkdir()
    env.update(TEMP=str(child_tmp), TMP=str(child_tmp), UV_OFFLINE="true",
               PYTHONPATH=os.pathsep.join(map(str, (agent, ROOT / "eval"))),
               PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
               MSBUILDDISABLENODEREUSE="1")
    for key in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONOPTIMIZE"):
        env.pop(key, None)
    python = env["FORGEPILOT_PYTHON"]
    # The originals call shutil.which from agent/, then grant the CMake root.
    # Check the same Windows search semantics in the exact child environment.
    lookup = "import json,shutil,sys; print(json.dumps([shutil.which(n) for n in sys.argv[1:]]))"
    names = ("cmake", "ctest", "cl", "nmake", "rc", "mt")
    selected = json.loads(subprocess.check_output([python, "-c", lookup, *names],
                                                  cwd=agent, env=env, text=True))
    for name, found in zip(names, selected, strict=True):
        expected = private_bin / (name + ".exe") if name in ("cmake", "ctest") else Path(tools[name]["path"])
        if not found or Path(found).resolve(strict=True) != expected:
            raise ValueError(f"Unexpected child executable for {name}: {found}")
    save_json(evidence / "child-tools.json", dict(zip(names, selected, strict=True)))
    capabilities = probe_symlinks(scratch)
    save_json(evidence / "symlink-capabilities.json", capabilities)
    base = scratch / "pytest"
    junit = evidence / "pytest.xml"
    command = [python, "-m", "pytest", "-p", "no:cacheprovider", "-vv", "-rs",
               "--basetemp", str(base), "--junitxml", str(junit), *TESTS]
    report["command"] = command
    with (evidence / "pytest.log").open("xb") as log:
        result = subprocess.run(command, cwd=agent, env=env, stdout=log, stderr=subprocess.STDOUT)
    report["pytest_exit"] = result.returncode
    save_build_evidence(base, evidence)
    outcomes = check_results(junit, capabilities)
    report["tests"] = outcomes
    if result.returncode or set(outcomes.values()) - {"passed", "permission_gap"}:
        return 1
    if "permission_gap" in outcomes.values():
        report["required_environment"] = (
            "Re-run on a Windows filesystem and token permitting real file and directory symbolic links "
            "(e.g. Developer Mode or SeCreateSymbolicLinkPrivilege)."
        )
        return 2
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "build-test/offline-isolation",
                        help="Evidence parent. Each invocation creates a new run directory.")
    args = parser.parse_args()
    parent = args.output.resolve()
    parent.mkdir(parents=True, exist_ok=True)
    evidence = Path(tempfile.mkdtemp(prefix="run-", dir=parent))
    print(f"Evidence: {evidence}", flush=True)
    scratch = Path(tempfile.mkdtemp(prefix="fp-iso-", dir=Path(tempfile.gettempdir()).resolve()))
    identity = scratch.stat()
    report = {"scratch": str(scratch), "scratch_removed": False}
    code = 1
    try:
        code = run(evidence, scratch, report)
        if code in (0, 2):
            remove_scratch(scratch, identity)
            report["scratch_removed"] = True
    except Exception:
        (evidence / "failure.log").write_text(traceback.format_exc(), encoding="utf-8")
        code = 1
    report["status"] = {0: "passed", 1: "failed", 2: "symlink_permission_gap"}[code]
    report["exit_code"] = code
    save_json(evidence / "result.json", report)
    print(report["status"], report.get("required_environment", ""), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
