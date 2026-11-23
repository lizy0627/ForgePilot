"""Publish a cancelled Mission's lesson in Main.qml, restart, and inspect two analysis runs."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
from uuid import uuid4
import xml.etree.ElementTree as ET

import httpx

from run_mission_tools_integration import ROOT, serve_app, wait_plan
from closed_loop_support import make_loop_case
from forgepilot.agents.prompts import get_prompt
from forgepilot.process import executable
from test_mission_scope_expansion import scope_app
from test_mission_skill_workflow import RecordingAnalyst


QT_CASE = "missionSkillsFromMain"
PHASES = ("publish", "selected", "zero")


def run_qt(phase, output, url, repo, *, source="", digest="", artifact=None):
    env = dict(os.environ, FORGEPILOT_SKILLS_URL=url, FORGEPILOT_SKILLS_PHASE=phase,
               FORGEPILOT_SKILLS_REPO=str(repo), FORGEPILOT_SKILLS_SOURCE_ID=source,
               FORGEPILOT_SKILLS_HASH=digest)
    # Qt splits platform options on ':' including a Windows drive letter.
    env.setdefault("QT_QPA_PLATFORM", "offscreen:configfile=client/tests/desktop-screen.json")
    if artifact is not None:
        env["FORGEPILOT_SKILLS_SOURCE_ARTIFACT"] = artifact["artifact_id"]
        env["FORGEPILOT_SKILLS_SOURCE_VALUE"] = artifact["content"]["objective"]
    kit = Path(env.get("FORGEPILOT_QT_KIT", "D:/Qt/6.8.3/msvc2022_64"))
    env["PATH"] = str(kit / "bin") + os.pathsep + env.get("PATH", "")
    report = output / (phase + ".xml")
    receipt = output / (phase + "-task.txt")
    env["FORGEPILOT_SKILLS_RECEIPT"] = str(receipt)
    binary = ROOT / "client/build/Release/ForgePilotWorkflowQmlTests.exe"
    process = subprocess.run([str(binary), QT_CASE, "-o", str(report) + ",xml", "-o", "-,txt"],
                             cwd=ROOT, env=env, capture_output=True, timeout=120)
    diagnostics = (process.stdout + process.stderr).decode("utf-8", errors="replace").strip()
    if diagnostics:
        print(diagnostics, flush=True)
    if process.returncode and report.exists():
        print(report.read_text(encoding="utf-8", errors="replace"), flush=True)
    process.check_returncode()
    xml = ET.parse(report)
    functions = [(f.get("name"), [event.get("type") for event in f.findall("Incident")])
                 for f in xml.findall("TestFunction")]
    if functions != [(name, ["pass"]) for name in ("initTestCase", QT_CASE, "cleanupTestCase")]:
        raise RuntimeError(f"{phase}: expected exactly one executed Qt scenario, got {functions}")
    executed = sum(incidents.count("pass") for name, incidents in functions if name == QT_CASE)
    print(f"{phase}: {QT_CASE} {executed} passed (process exit {process.returncode})", flush=True)
    return receipt.read_text(encoding="ascii")


def json_ok(response):
    response.raise_for_status()
    return response.json()


async def scenario(root, output):
    git = executable(os.environ.get("FORGEPILOT_GIT"), "git")
    case = await make_loop_case(root, git)
    analyst = RecordingAnalyst(case)
    evidence = {}
    try:
        # No skill-authoring gateway. The developer can still save a private
        # note and review reusable guidance from a real terminal graph.
        async with scope_app(case, root, git, analyst_provider=analyst) as app:
            with serve_app(app) as url, httpx.Client(base_url=url + "/api/v2/", trust_env=False) as api:
                created = json_ok(api.post("missions", json={"repository_path": str(case.root),
                                  "request": "Inspect negative inputs", "task_type": "bug_fix"}))
                source = created["task_id"]
                waiting = wait_plan(url, source)
                cancelled = json_ok(api.post(f"missions/{source}/cancel", json={
                    "expected_revision": waiting["current_revision"]}))
                assert cancelled["status"] == "cancelled", cancelled
                artifacts = json_ok(api.get(f"missions/{source}/artifacts"))["items"]
                spec, = [a for a in artifacts if a["artifact_type"] == "TaskSpec"]
                published_source = run_qt("publish", output, url, case.root, source=source, artifact=spec)
                assert published_source == source
                draft, = json_ok(api.get("skill-drafts", params={"source_mission_id": source}))["drafts"]
                assert draft["result_version"] == 1 and draft["data"]["generation_status"] == "manual", draft
                assert draft["data"]["developer_note"] == "Checked at cancellation."
                assert draft["data"]["evidence_refs"] and draft["revision"] > 1
                version_path = f"skills/{draft['result_skill_id']}/versions/1"
                version = json_ok(api.get(version_path))
                identity = {key: version[key] for key in ("skill_id", "version", "content_sha256")}
                assert version["content"]["body"] == "Check the value before clamping."
                evidence["publish"] = {"source_mission": source, "draft": draft["draft_id"], "reviewed": identity}

        # Reopen SQLite with a new app, workspace manager and model gateway.
        # Each phase launches a fresh Qt process at the new loopback address.
        async with scope_app(case, root, git, restart=True, analyst_provider=analyst) as app:
            with serve_app(app) as url, httpx.Client(base_url=url + "/api/v2/", trust_env=False) as api:
                assert json_ok(api.get(version_path)) == version
                for phase in PHASES[1:]:
                    analyst.calls.clear()
                    task = run_qt(phase, output, url, case.root, digest=identity["content_sha256"])
                    path = f"missions/{task}/"
                    usage = json_ok(api.get(path + "skills"))
                    selected = phase == "selected"
                    assert usage["selected_skills"] == ([identity] if selected else []), usage
                    assert usage["injections"] == ([dict(identity, node_id="root_cause_analysis", attempt=1)]
                                                   if selected else []), usage
                    graph = json_ok(api.get(path + "graph"))["definition"]
                    analyst_node, = [n for n in graph["nodes"] if n["node_id"] == "root_cause_analysis"]
                    target_prompt = get_prompt(analyst_node["agent_id"], analyst_node["node_id"])
                    assert len(analyst.calls) == 4 and sum(p == target_prompt for _, p, _ in analyst.calls) == 1
                    for payload, prompt, _ in analyst.calls:
                        if selected and prompt == target_prompt:
                            received, = payload["skill_references"]
                            assert {k: received[k] for k in identity} == identity, received
                            assert received["body"] == version["content"]["body"]
                        else:
                            assert "skill_references" not in payload and "skill_reference_usage" not in payload
                    evidence[phase] = {"task": task, "usage": usage,
                        "model_inputs": [payload for payload, _, _ in analyst.calls]}
        if set(evidence) != set(PHASES) or len({source, evidence["selected"]["task"], evidence["zero"]["task"]}) != 3:
            raise RuntimeError("Expected three distinct Mission scenarios")
        (output / "model-evidence.json").write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")
    finally:
        await case.gateway.aclose()


def main():
    if not __debug__:
        raise RuntimeError("Enable Python assertions to verify model inputs and frozen versions")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=ROOT / "artifacts/qt-integration")
    args = parser.parse_args()
    output = args.output_root.resolve() / ("mission-skills-" + uuid4().hex[:12])
    output.mkdir(parents=True)
    print(f"Main.qml + SQLite Mission skill evidence: {output}", flush=True)
    with tempfile.TemporaryDirectory(prefix="fp-skills-") as temporary:
        root = Path(temporary) / "case"
        root.mkdir()
        asyncio.run(scenario(root, output))
    print("Mission skills integration: 3 scenarios passed, 0 skipped", flush=True)


if __name__ == "__main__":
    main()
