"""Test the offline runner's pass/skip accounting and cleanup boundaries."""

import os
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    import run_offline_isolation

    return run_offline_isolation


def test_only_matching_skip_and_denied_capability_are_permission_gaps(runner, tmp_path):
    suite = ET.Element("testsuite")
    cases = []
    for node in runner.TESTS:
        file, name = node.split("::")
        cases.append(ET.SubElement(suite, "testcase", classname=file[:-3].replace("/", "."), name=name))
    junit = tmp_path / "report.xml"
    capabilities = {"file": {"status": "permission_denied"}, "directory": {"status": "available"}}
    left = ET.SubElement(cases[2], "skipped", message=runner.LINK_SKIPS[runner.TESTS[2]][1])
    right = ET.SubElement(cases[3], "skipped", message=runner.LINK_SKIPS[runner.TESTS[3]][1])
    ET.ElementTree(suite).write(junit)
    result = runner.check_results(junit, capabilities)
    assert list(result.values()) == ["passed", "passed", "permission_gap", "unexpected_result"]
    cases[3].remove(right)
    left.set("message", "fixture skipped unexpectedly")
    ET.SubElement(cases[1], "failure")
    ET.ElementTree(suite).write(junit)
    assert list(runner.check_results(junit, capabilities).values()) == [
        "passed", "failed", "unexpected_result", "passed"
    ]
    suite.remove(cases[3])
    ET.ElementTree(suite).write(junit)
    with pytest.raises(ValueError, match="Missing testcases"):
        runner.check_results(junit, capabilities)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction cleanup boundary")
def test_cleanup_checks_descendants_before_removing_anything(runner, tmp_path):
    from _winapi import CreateJunction

    root = tmp_path / "scratch"
    root.mkdir()
    saved = root.stat()
    ordinary = root / "keep"
    ordinary.touch()
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = root / "alias"
    CreateJunction(str(outside), str(alias))
    try:
        with pytest.raises(ValueError, match="outside the run"):
            runner.remove_scratch(root, saved)
        assert ordinary.exists() and outside.exists() and alias.is_junction()
    finally:
        # Remove the junction entry itself, without traversing it.
        assert alias.parent.resolve(strict=True) == root.resolve(strict=True) and alias.is_junction()
        assert alias.resolve(strict=True) == outside.resolve(strict=True)
        alias.rmdir()
    with pytest.raises(ValueError, match="identity"):
        runner.remove_scratch(root, outside.stat())
    runner.remove_scratch(root, saved)
    assert not root.exists() and outside.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Windows pytest temporary directory aliases")
def test_evidence_visits_each_real_test_directory_once(runner, tmp_path):
    from _winapi import CreateJunction

    base = tmp_path / "pytest"
    directory = base / "example0"
    report = directory / "graded-candidate/probe-commands.json"
    report.parent.mkdir(parents=True)
    report.write_text("[]", encoding="utf-8")
    alias = base / "examplecurrent"
    CreateJunction(str(directory), str(alias))
    evidence = tmp_path / "evidence"
    try:
        runner.save_build_evidence(base, evidence)
        assert [p.read_bytes() for p in evidence.rglob("*.json")] == [b"[]"]
    finally:
        assert alias.parent.resolve(strict=True) == base.resolve(strict=True) and alias.is_junction()
        assert alias.resolve(strict=True) == directory.resolve(strict=True)
        alias.rmdir()
