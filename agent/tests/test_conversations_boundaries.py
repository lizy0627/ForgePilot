"""Lightweight import and compatibility checks for the conversations module."""

import ast
from importlib import import_module
from importlib.util import resolve_name
import json
from pathlib import Path
import pkgutil
import subprocess
import sys

import pytest

from conftest import ScriptedLLM, reply
from forgepilot import conversations


ROOT = Path(__file__).resolve().parents[2]
AGENT = ROOT / "agent"
LEGACY = {
    "conversation_host": "application",
    "chat_service": "service",
    "conversation_stream": "stream",
    "conversation_store": "store",
    "memory_store": "memory_store",
    "memory_context": "context",
}


def imported_modules(path):
    """Resolve relative imports so alternate spellings cannot hide a dependency."""
    relative = path.relative_to(AGENT if path.is_relative_to(AGENT) else ROOT).with_suffix("")
    package = ".".join(relative.parts[:-1])
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name, None
        elif isinstance(node, ast.ImportFrom):
            name = "." * node.level + (node.module or "")
            module = resolve_name(name, package) if node.level else name
            yield node.lineno, module, [alias.name for alias in node.names]


def test_production_callers_use_only_the_public_conversations_boundary():
    errors = []
    legacy_modules = {"forgepilot." + name for name in LEGACY}
    owner_implementations = {
        "forgepilot.api", "forgepilot.server", "forgepilot.task_host",
        "forgepilot.sqlite_store", "forgepilot.mission_host", "forgepilot.mission_store",
        "forgepilot.orchestration", "forgepilot.agents",
    }
    sources = list((AGENT / "forgepilot").rglob("*.py"))
    sources.append(ROOT / "scripts" / "benchmark_chat.py")
    for path in sources:
        internal = path.is_relative_to(AGENT / "forgepilot" / "conversations")
        for line, module, names in imported_modules(path):
            candidates = {module}
            if names:
                candidates.update(module + "." + name for name in names)
            if candidates & legacy_modules:
                errors.append(f"{path.relative_to(ROOT)}:{line}: legacy import {module}")
            if internal and any(module == owner or module.startswith(owner + ".")
                                for owner in owner_implementations):
                errors.append(f"{path.relative_to(ROOT)}:{line}: reverse implementation import {module}")
            if not internal and module.startswith("forgepilot.conversations."):
                errors.append(f"{path.relative_to(ROOT)}:{line}: internal import {module}")
            if not internal and module == "forgepilot.conversations" and names:
                unknown = set(names) - set(conversations.__all__)
                if unknown:
                    errors.append(f"{path.relative_to(ROOT)}:{line}: nonpublic names {sorted(unknown)}")
    assert not errors, "\n".join(errors)

    # The shared SQLite owner integrates through public hooks, even though the
    # extensions retain private hooks for existing rollback/fault-injection tests.
    private_hooks = {
        "_conversation_schema", "_memory_schema", "_recover_conversations",
        "_sync_conversation_task", "_conversation",
    }
    owner = ast.parse((AGENT / "forgepilot" / "sqlite_store.py").read_text(encoding="utf-8-sig"))
    assert not {node.attr for node in ast.walk(owner)
                if isinstance(node, ast.Attribute) and node.attr in private_hooks}


@pytest.mark.parametrize("old,new", LEGACY.items())
def test_legacy_import_is_the_same_module_and_implementation(old, new):
    legacy = import_module("forgepilot." + old)
    implementation = import_module("forgepilot.conversations." + new)
    assert legacy is implementation
    assert legacy.__file__ == implementation.__file__
    shim = ast.parse((AGENT / "forgepilot" / (old + ".py")).read_text(encoding="utf-8-sig"))
    assert not any(isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                   for node in ast.walk(shim))
    if old == "chat_service":
        assert legacy.ChatAnswerEvent is conversations.ChatAnswerEvent


@pytest.mark.asyncio
async def test_legacy_monkeypatch_reaches_the_public_chat_service(monkeypatch):
    legacy = import_module("forgepilot.chat_service")
    original = legacy.chat_tool_route
    routed = []

    def route(mode, prompt, has_repository):
        routed.append((mode, prompt, has_repository))
        return original(mode, prompt, has_repository)

    monkeypatch.setattr("forgepilot.chat_service.chat_tool_route", route)
    model = ScriptedLLM([reply("offline answer")])
    service = conversations.ChatService(model)
    try:
        assert await service.answer([], "offline question", tool_mode="fast") == "offline answer"
        assert routed == [("fast", "offline question", False)]
    finally:
        await service.close()


def run_import_check(code, *arguments):
    result = subprocess.run(
        [sys.executable, "-c", code, *arguments], cwd=AGENT,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("legacy_first", [True, False])
def test_import_order_has_no_cycle_and_all_public_exports_resolve(legacy_first):
    modules = ["forgepilot." + name for name in LEGACY]
    modules += ["forgepilot.conversations." + name for name in LEGACY.values()]
    if not legacy_first:
        modules.reverse()
    run_import_check(
        """
import importlib, json, sys
for name in json.loads(sys.argv[1]):
    importlib.import_module(name)
from forgepilot import conversations
for name in conversations.__all__:
    assert getattr(conversations, name) is not None, name
for name in ('api', 'server', 'sqlite_store', 'runtime', 'cli'):
    importlib.import_module('forgepilot.' + name)
""", json.dumps(modules),
    )


def test_public_package_and_event_contract_import_without_runtime_resources():
    run_import_check(
        """
import sqlite3, sys, threading
def unexpected(*args, **kwargs):
    raise AssertionError('import opened a runtime resource')
sqlite3.connect = unexpected
threading.Thread.start = unexpected
from forgepilot import conversations
assert 'forgepilot.conversations.service' not in sys.modules
assert 'forgepilot.conversations.application' not in sys.modules
event = conversations.ChatAnswerEvent('content', 'segment-1', text='offline')
assert event.text == 'offline'
assert 'forgepilot.conversations.service' not in sys.modules
assert 'forgepilot.conversations.application' not in sys.modules
assert 'forgepilot.llm' not in sys.modules
""",
    )


def test_public_sqlite_hooks_preserve_legacy_fault_injection(monkeypatch):
    from forgepilot.sqlite_store import SqliteEventStore

    calls = []
    for private in ("_conversation_schema", "_memory_schema", "_recover_conversations"):
        original = getattr(SqliteEventStore, private)

        def observed(self, _original=original, _private=private):
            calls.append(_private)
            return _original(self)

        monkeypatch.setattr(SqliteEventStore, private, observed)
    store = SqliteEventStore()
    try:
        assert calls == ["_conversation_schema", "_memory_schema", "_recover_conversations"]
        cid = store.create_conversation()["conversation_id"]
        assert store.require_conversation(cid)["conversation_id"] == cid
        snapshots = []
        monkeypatch.setattr(store, "_sync_conversation_task", snapshots.append)
        snapshot = {"task_id": "offline-task", "state": "completed"}
        store.sync_conversation_task(snapshot)
        assert snapshots == [snapshot]
        assert conversations.ConversationStore in type(store).__mro__
        assert conversations.MemoryStore in type(store).__mro__
    finally:
        store.close()


def test_module_discovery_matches_release_collection():
    discovered = {item.name for item in pkgutil.iter_modules(conversations.__path__)}
    assert set(LEGACY.values()) | {"events"} <= discovered
    package_script = (ROOT / "scripts" / "Package-Windows.ps1").read_text(encoding="utf-8-sig")
    if "PyInstaller" in package_script:
        assert "'--collect-submodules', 'forgepilot'" in package_script
    else:
        # The committed source distribution and the user's pending frozen
        # distribution have distinct collection mechanisms; both include packages.
        assert "Copy-PythonTree (Join-Path $root 'agent\\forgepilot') 'agent\\forgepilot'" in package_script
        assert "Get-ChildItem -LiteralPath $Source -Recurse -File -Filter '*.py'" in package_script
        assert "Copy-File $_.FullName (Join-Path $RelativeDestination $relative)" in package_script
