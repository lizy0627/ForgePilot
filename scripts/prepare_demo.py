"""Create a retained, independent Qt demo. Never reset or modify an existing repository."""
import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agent"), str(ROOT / "eval")]
from forgepilot.build_process import build_environment, run_build_process
from forgepilot.code_intelligence import CodeIntelligence, CodeIntelligenceConfig
from forgepilot.process import executable, run_process
from forgepilot.repository import Repository
from forgepilot.security import Redactor
from forgepilot_eval.grader import read_junit
from forgepilot_eval.manifest import inventory
from forgepilot_eval.runner import save

FIXTURE = ROOT / "eval/demo/qt-button-status"
SOURCE = "src/StatusController.cpp"
FIXED = '#include "StatusController.h"\n\nvoid StatusController::activate()\n{\n    if (active_)\n        return;\n    active_ = true;\n    emit statusChanged(status());\n}\n'


async def copy_demo(args, *, reference=False):
    source_hashes = inventory(FIXTURE)
    parent = Path(tempfile.mkdtemp(prefix="ForgePilot-v1-reference-" if reference else "ForgePilot-v1-demo-"))
    repo = parent / "repository"
    shutil.copytree(FIXTURE, repo)
    if reference: (repo / SOURCE).write_text(FIXED, encoding="utf-8", newline="\n")
    (repo / ".gitignore").write_text(".forgepilot/\n", encoding="utf-8")
    presets = {"version": 6, "configurePresets": [{"name": "demo", "generator": "Ninja",
        "binaryDir": "${sourceDir}/.forgepilot/build", "cacheVariables": {"CMAKE_BUILD_TYPE": "Debug",
        "CMAKE_MAKE_PROGRAM": args.ninja, "CMAKE_EXPORT_COMPILE_COMMANDS": "ON",
        "Qt6_DIR": str(Path(args.qt_kit) / "lib/cmake/Qt6")}}],
        "buildPresets": [{"name": "demo-build", "configurePreset": "demo", "configuration": "Debug"}],
        "testPresets": [{"name": "demo-test", "configurePreset": "demo", "configuration": "Debug"}]}
    (repo / "CMakeUserPresets.json").write_text(json.dumps(presets, indent=2), encoding="utf-8")
    for argv in ([args.git, "init", "-q", str(repo)], [args.git, "-C", str(repo), "add", "--", "."]):
        result = await run_process(argv, parent)
        if result.returncode: raise ValueError("Cannot prepare isolated demo Git baseline")
    assert inventory(FIXTURE) == source_hashes
    return repo


async def verify(args, repo, output):
    output.mkdir(parents=True, exist_ok=False)
    commands = []
    for phase, argv in [
        ("configure", [args.cmake, "--preset", "demo"]),
        ("build", [args.cmake, "--build", "--preset", "demo-build"]),
        ("test", [args.ctest, "--preset", "demo-test", "--output-on-failure", "--no-tests=error",
                   "--timeout", "15", "--output-junit", str(output / "tests.xml")])]:
        result = await run_build_process(argv, repo, timeout=180, env=build_environment())
        commands.append({"phase": phase, **asdict(result)})
        if phase != "test" and result.status != "succeeded": break
    tests = read_junit(output / "tests.xml", "button_updates_status", commands[-1]["phase"] == "test" and commands[-1]["returncode"] == 0)
    evidence = {"repository": str(repo), "commands": commands, "tests": tests}
    save(output / "commands.json", evidence, args.redactor)
    return evidence


async def components(args, repo, output):
    from forgepilot.mcp_config import MCPConfig, TOOL_NAME
    from forgepilot.tools import ToolRegistry
    from forgepilot.tools.mcp_tools import MCPToolAdapter
    repository = await Repository.open(str(repo), rg_executable=args.rg, git_executable=args.git)
    search = json.loads(await repository.search_code("statusChanged"))
    text = (repo / "src/MainWindow.cpp").read_text(encoding="utf-8")
    row = next(i for i, line in enumerate(text.splitlines(), 1) if "&StatusController::activate" in line)
    column = text.splitlines()[row-1].index("activate);") + 1
    intelligence = CodeIntelligence(repository, CodeIntelligenceConfig(args.clangd, ".forgepilot/build"))
    definition = json.loads(await intelligence.query("find_symbol", path="src/MainWindow.cpp", line=row, column=column))
    references = json.loads(await intelligence.query("find_references", path="src/MainWindow.cpp", line=row, column=column))
    registry = ToolRegistry()
    adapter = MCPToolAdapter(MCPConfig(command=sys.executable, args=("-I", "-u", str(ROOT / "agent/examples/mcp_project_notes.py")), cwd=str(output)))
    try:
        await adapter.register(registry)
        mcp = await registry.execute(TOOL_NAME, {"topic": "architecture"})
    finally: await adapter.close()
    evidence = {"mode": "real_local_components_no_llm", "rg": search, "definition": definition,
                "references": references, "mcp": mcp.model_dump(mode="json")}
    save(output / "components.json", evidence, args.redactor)
    assert definition["source"] == references["source"] == "clangd", evidence
    assert any(p["path"] == SOURCE for p in definition["locations"]), definition
    assert any(p["path"] == "src/MainWindow.cpp" for p in references["locations"]), references
    assert mcp.success and "HTTP and SSE" in mcp.output
    return evidence


async def repair_limit(args, output):
    from forgepilot.llm import ChatReply
    from forgepilot.presets import BuildConfig
    from forgepilot.recording import TaskJournal
    from forgepilot.sqlite_store import SqliteEventStore
    from forgepilot.task_service import TaskService
    repo = await copy_demo(args)
    baseline = inventory(repo, ignore_generated=True)
    initial = (repo / SOURCE).read_text(encoding="utf-8")
    plan = {"summary": "CONTROLLED FAILURE: preserve the signal bug for Repair limit verification",
        "root_cause": "The status notification is missing", "files_to_modify": [{"path": SOURCE, "reason": "Controlled source-only repair attempt"}],
        "implementation_steps": ["Change only the source comment; intentionally leave the missing notification"],
        "validation_steps": ["Real Qt Configure, Build, CTest"], "risks": ["Expected four real failing test rounds; not real model evaluation"]}
    def reply(content=None, content_to_write=None, identity="write"):
        msg = {"role": "assistant", "content": content}
        if content_to_write is not None:
            msg["tool_calls"] = [{"id": identity, "type": "function", "function": {"name": "write_file",
                "arguments": json.dumps({"path": SOURCE, "content": content_to_write})}}]
        return ChatReply.model_validate({"message": msg, "usage": None})
    script = [reply("CONTROLLED FAILURE fixture analysis"), reply(json.dumps(plan)),
              reply(content_to_write=initial+"\n// controlled initial change\n"), reply("Run the real validation")]
    for n in range(1, 4):
        script += [reply(json.dumps({"category": "code", "plan": plan})),
                   reply(content_to_write=initial+f"\n// controlled repair {n}\n", identity=f"repair-{n}"), reply("Run the real validation")]
    class FixedFailureModel:
        count = 0
        async def chat(self, messages, tools=None):
            self.count += 1
            assert self.count <= len(script), "Fourth Repair / unexpected model call"
            return script[self.count-1]
    model = FixedFailureModel()
    store = SqliteEventStore(output / "controlled-repair.sqlite3", redactor=args.redactor)
    events = []
    journal = TaskJournal(store, lambda: service.snapshot(persist_diff=True), args.redactor, on_event=events.append)
    service = TaskService(model, rg_executable=args.rg, git_executable=args.git, trace=journal, redactor=args.redactor,
        build_config=BuildConfig(args.cmake, args.ctest, configure_preset="demo", build_preset="demo-build", test_preset="demo-test"))
    try:
        pending = await service.start(str(repo), "CONTROLLED FAILURE: verify maximum three repair rounds; intentionally keep failing Qt test")
        assert pending.state == "waiting_approval" and inventory(repo, ignore_generated=True) == baseline
        save(output / "controlled-approval.json", {"actor": "automated regression, not human MVP approval",
            "task_id": pending.task_id, "plan_version": pending.plan_version, "plan": pending.plan}, args.redactor)
        service.approve(pending.task_id, pending.plan_version)
        result = await service.execute(pending.task_id)
        journal.flush()
        snapshot = service.snapshot(persist_diff=True)
        save(output / "controlled-repair-result.json", asdict(snapshot), args.redactor)
        save(output / "controlled-repair-trace.json", [asdict(e) for e in events], args.redactor)
        assert result.state == "failed" and result.outcome == "repair_exhausted", result.error
        assert result.repair_round == 3 and model.count == 13 and len(result.validation_history) == 4
        assert all(h["tests"]["failed"] == 1 and h["stages"]["cmake_build"] == "succeeded" for h in result.validation_history)
        assert (repo / SOURCE).read_text(encoding="utf-8").endswith("// controlled repair 3\n")
        after = inventory(repo, ignore_generated=True)
        assert {p for p in set(after)|set(baseline) if after.get(p)!=baseline.get(p)} == {SOURCE}
        assert snapshot.task_diff["files"] and store._db.execute("PRAGMA foreign_key_check").fetchall() == []
        task_id = result.task_id
    finally:
        service.close(); journal.flush(); store.close()
    with_reopen = SqliteEventStore(output / "controlled-repair.sqlite3")
    try:
        stored = with_reopen.get(task_id)
        assert stored["read_only"] and stored["state"] == "failed" and stored["repair_round"] == 3
        save(output / "controlled-repair-reopened.json", stored, args.redactor)
    finally: with_reopen.close()
    return {"repository": str(repo), "task_id": task_id, "repair_rounds": 3,
            "validation_rounds": 4, "model": "fixed controlled failure, no real LLM", "retained": True}


async def run(args):
    args.redactor = Redactor((os.environ.get("FORGEPILOT_API_KEY", ""),))
    for name in ("cmake", "ninja", "git", "rg"):
        setattr(args, name, executable(getattr(args, name), name))
    args.ctest = str(Path(args.cmake).with_name("ctest.exe"))
    if not args.qt_kit or not Path(args.qt_kit).is_absolute() or not (Path(args.qt_kit)/"lib/cmake/Qt6/Qt6Config.cmake").is_file():
        raise ValueError("Configure the existing absolute Qt Kit")
    output = Path(args.output_root).resolve() / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    evidence = {"mode": "fixture preparation and local checks; not real LLM MVP acceptance", "started_at": datetime.now(timezone.utc).isoformat(),
        "output": str(output), "fixture_hashes": inventory(FIXTURE), "model_configured": bool(
            os.environ.get("FORGEPILOT_API_KEY") and os.environ.get("FORGEPILOT_BASE_URL") and
            (os.environ.get("FORGEPILOT_TASK_MODEL") or os.environ.get("FORGEPILOT_MODEL")))}
    try:
        repo = await copy_demo(args)
        evidence.update(repository=str(repo), demo_executable=str(repo/".forgepilot/build/ButtonStatusDemo.exe"))
        baseline = await verify(args, repo, output/"broken")
        assert len(baseline["commands"]) == 3 and all(c["status"] == "succeeded" for c in baseline["commands"][:2]), "Demo must configure and build"
        assert baseline["commands"][-1]["returncode"] == 8 and baseline["tests"]["count"] == 1 and baseline["tests"]["status"] == "failed", "Expected actual UI assertion failure"
        diagnostic = baseline["commands"][-1]["stdout"] + baseline["commands"][-1]["stderr"]
        assert all(word in diagnostic for word in ("FAIL!", "Actual", "Expected", "Ready", "Active")), "Runtime needs actual Qt assertion diagnostics"
        evidence["broken_test_reproduced"] = True
        if args.check_reference:
            reference = await copy_demo(args, reference=True)
            verified = await verify(args, reference, output/"reference")
            assert verified["tests"]["status"] == "passed", verified
            evidence.update(reference_repository=str(reference), reference_test_passed=True,
                            reference_notice="Developer reference on another copy, not Agent success")
        if args.check_components:
            await components(args, repo, output); evidence["local_components_passed"] = True
        if args.check_repair_limit:
            evidence["controlled_repair"] = await repair_limit(args, output)
        evidence["status"] = "passed_local_checks"
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
        return 0
    finally:
        evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
        save(output/"demo.json", evidence, args.redactor)
        print("Retained demo evidence: " + str(output/"demo.json"), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("cmake", "ninja", "git", "rg", "qt-kit", "clangd"):
        parser.add_argument("--"+name, default=os.environ.get("FORGEPILOT_"+name.upper().replace("-", "_")))
    parser.add_argument("--output-root", default=str(ROOT/"logs/demo"))
    for flag in ("check-reference", "check-components", "check-repair-limit"):
        parser.add_argument("--"+flag, action="store_true")
    args = parser.parse_args()
    try: return asyncio.run(run(args))
    except KeyboardInterrupt: return 130
    except Exception as exc:
        print(Redactor((os.environ.get("FORGEPILOT_API_KEY", ""),)).text(str(exc)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
