"""Offline architecture gates for the migrated backend slice.

These are source checks, including lazy and TYPE_CHECKING imports. They describe
the current modular monolith, not an assertion that every legacy module is pure.
Historical dependencies are identified by caller, member and bounded occurrence;
removing one never obliges a new implementation to keep it alive.
"""
import ast
from collections import Counter
from pathlib import Path

import pytest

from test_conversations_boundaries import imported_modules


ROOT = Path(__file__).resolve().parents[2] / "agent" / "forgepilot"

# Pure rules and contracts: add an explicit module when migrating more rules.
# Local edges are exact, so importing a new infrastructure module fails even
# when it is hidden behind an innocuous name or only used inside a function.
RULE_IMPORTS = {
    "artifacts/models.py": {"artifacts.canonical", "path_identity"},
    "artifacts/canonical.py": set(),
    "artifacts/validation.py": {"artifacts.canonical", "artifacts.models"},
    "artifacts/store.py": {"artifacts.models", "artifacts.validation"},
    "orchestration/models.py": set(),
    "orchestration/graph.py": {"orchestration.models", "agents.registry"},
    "orchestration/policies.py": {"orchestration.models"},
    "orchestration/execution.py": {"errors"},
    "orchestration/workspace.py": {"errors", "path_identity", "orchestration.models"},
    "conversations/events.py": set(),
    "agents/definitions.py": {"artifacts.canonical", "artifacts.models", "policy"},
    "policy.py": {"errors"},
    "models/contracts.py": {"stream_metrics"},
    "models/errors.py": {"errors"},
    "task_capacity.py": {"errors"},
}
PURE_LIBRARIES = {
    "__future__", "collections", "dataclasses", "datetime", "enum", "functools",
    "hashlib", "json", "pydantic", "re", "threading", "types", "typing", "uuid", "weakref",
    "contextlib",
}
APPLICATIONS = (
    "mission_host.py", "agents/analysis.py", "agents/implementation.py", "agents/delivery.py",
    "orchestration/runner.py", "orchestration/recovery.py", "mission_observability.py",
    "conversations/application.py", "conversations/service.py",
)
TRANSPORTS = {"fastapi", "starlette", "uvicorn", "httpx", "requests", "aiohttp", "sqlite3", "urllib3", "socket"}
SQLITE_IMPLEMENTATIONS = {"forgepilot.sqlite_store", "forgepilot.artifacts.sqlite_store"}
NETWORK_IMPLEMENTATIONS = {"forgepilot.models.openai", "forgepilot.mcp", "forgepilot.mcp_http"}
DATABASE_PRIVATE = {"_db", "_lock", "_transaction", "_graph_execution_lock", "_graph_active_tasks"}

# Only exception classification; no connection or SQL access is permitted.
APPLICATION_IMPORT_EXCEPTIONS = {
    ("conversations/application.py", "sqlite3"): "Legacy persistence-failure translation via sqlite3.Error",
}

# Expressions are exact and counts are upper bounds, not wildcard exclusions.
# Keys stay useful when user worktree functionality is absent in a clean checkout.
PRIVATE_ACCESS = {
    ("mission_host.py", "workflow._analysis_graph"): (2, "Build the stored delivery graph"),
    ("mission_host.py", "workflow._stored_graph"): (4, "Verify frozen graph before control"),
    ("mission_host.py", "workflow._check_review"): (1, "Verify the approval package"),
    ("mission_host.py", "workflow._run_worker"): (1, "Bridge synchronous graph approval"),
    ("mission_host.py", "workflow.runner._assert_definition"): (1, "Retry verifies frozen definition"),
    ("mission_host.py", "workspace._load"): (1, "Read durable delivery reconciliation state"),
    ("agents/analysis.py", "self.repository._path"): (1, "Validate evidence under the repository guard"),
    ("agents/implementation.py", "repository._path"): (1, "Validate frozen evidence paths"),
    ("agents/implementation.py", "analysis._loop"): (1, "Bind composed analysis to the same model loop"),
    ("agents/implementation.py", "analysis._drain"): (1, "Join composed analysis before resource close"),
    ("agents/implementation.py", "self._analysis_for(context.task_id)._agent_handler"): (1, "Delegate analysis node"),
    ("agents/implementation.py", "self._analysis_for(context.task_id)._locked_task_spec"): (1, "Delegate locked specification"),
    ("agents/implementation.py", "self._analysis_for(context.task_id)._join"): (1, "Delegate evidence join"),
    ("agents/delivery.py", "self.runner._exclusive_control"): (1, "Control shares graph execution slot"),
    ("agents/delivery.py", "self.runner.recovery._verify_pairs"): (1, "Verify restored baseline artifact pairs"),
    ("agents/delivery.py", "super()._package"): (1, "Implementation/Delivery inheritance within the agents module"),
    ("agents/delivery.py", "super()._agent"): (1, "Implementation/Delivery inheritance within the agents module"),
}

# Cross-file private imports anywhere in backend production. Counts also stop
# an existing exception from becoming a general authorization for more callers.
PRIVATE_IMPORTS = {
    ("git.py", "workspace_safety", "_identity"): 3,
    ("git.py", "workspace_safety", "_same_path"): 2,
    ("git.py", "workspace_safety", "_open_nofollow"): 1,
    ("git.py", "workspace_safety", "_windows_directory_handles"): 1,
    ("repair.py", "planning", "_unique_object"): 1,
    ("validation.py", "workspace_safety", "_exists"): 1,
    ("workspace_delivery.py", "integrity", "_assertions"): 1,
    ("workspace_manager.py", "workspace_safety", "_exists"): 1,
    ("workspace_manager.py", "orchestration.workspace", "_manifest"): 1,
    ("workspace_safety.py", "orchestration.workspace", "_manifest"): 3,
    ("agents/analysis.py", "orchestration.models", "_plain_json"): 1,
    ("agents/delivery.py", "orchestration.models", "_plain_json"): 1,
    ("agents/implementation.py", "orchestration.models", "_plain_json"): 3,
    ("agents/implementation_tools.py", "integrity", "_assertions"): 1,
    ("agents/implementation_tools.py", "tools.retrieval_tools", "_structured_output"): 1,
    ("agents/registry.py", "models.profiles", "_unique_json_pairs"): 1,
    ("orchestration/analysis_review.py", "orchestration.store", "_now"): 1,
    ("orchestration/analysis_review.py", "mission_store", "_identifier"): 1,
    ("orchestration/checkpoint.py", "orchestration.models", "_canonical"): 1,
    ("orchestration/checkpoint.py", "orchestration.models", "_identifier"): 1,
    ("rag/integration.py", "rag.indexer", "_RepositoryIndexState"): 1,
}

# Exact edges in existing lazy import cycles. All other cycle edges fail.
# MCP transport factory, model configuration validation, guarded file access,
# SQLite repository collaboration, and delivery/reviewer success predicate.
LAZY_CYCLE_EDGES = {
    ("mcp", "mcp_config"), ("mcp_config", "mcp"), ("mcp_config", "mcp_http"), ("mcp_http", "mcp"),
    ("models.profiles", "models.routing"), ("models.routing", "models.profiles"),
    ("file_access", "repository"), ("repository", "file_access"),
    ("mission_store", "orchestration.analysis_review"), ("mission_store", "orchestration.store"),
    ("orchestration.analysis_review", "mission_store"), ("orchestration.analysis_review", "orchestration.store"),
    ("orchestration.store", "orchestration.analysis_review"),
    ("agents.delivery", "agents.review"), ("agents.review", "agents.delivery"),
}


def tree(path):
    return ast.parse(path.read_text(encoding="utf-8-sig"))


def eager_import_lines(parsed):
    """Imports executed by the module body, excluding type-only declarations."""
    lines = set()

    class Imports(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            pass

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_If(self, node):
            if ast.unparse(node.test) in {"TYPE_CHECKING", "typing.TYPE_CHECKING"}:
                for statement in node.orelse:
                    self.visit(statement)
            else:
                self.generic_visit(node)

        def visit_Import(self, node):
            lines.add(node.lineno)

        visit_ImportFrom = visit_Import

    Imports().visit(parsed)
    return lines


def module_sources():
    sources = {}
    for path in ROOT.rglob("*.py"):
        parts = path.relative_to(ROOT).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        sources[".".join(("forgepilot", *parts))] = path
    return sources


def import_graph(*, eager=False):
    sources = module_sources()
    graph = {module: set() for module in sources}
    for module, path in sources.items():
        lines = eager_import_lines(tree(path)) if eager else None
        for line, target, names in imported_modules(path):
            if lines is not None and line not in lines:
                continue
            candidates = {target, *(target + "." + name for name in names or ())}
            graph[module].update(candidate for candidate in candidates if candidate in sources and candidate != module)
    return graph


def rule_violations(path, allowed):
    failures = []
    for line, module, names in imported_modules(path):
        if module.startswith("forgepilot."):
            if module.removeprefix("forgepilot.") not in allowed:
                failures.append((line, module))
        elif module.split(".")[0] not in PURE_LIBRARIES:
            failures.append((line, module))
        # The graph needs built-in *declarations*, never registry file loading.
        if module == "forgepilot.agents.registry" and names != ["BUILTIN_AGENT_DEFINITIONS"]:
            failures.append((line, module))
    return failures


@pytest.mark.parametrize("relative,allowed", RULE_IMPORTS.items())
def test_rules_and_contracts_do_not_import_transport_or_storage(relative, allowed):
    assert not rule_violations(ROOT / relative, allowed)


def test_rule_helpers_cannot_hide_eager_transport_or_sqlite_imports():
    # Check the local import closure as well: a new HTTP import in path_identity
    # or registry must not bypass the direct rule gate. The registry's lazy
    # configuration-file loader is outside the built-in declaration dependency.
    sources = module_sources()
    graph = import_graph(eager=True)
    pending = ["forgepilot." + name.removesuffix(".py").replace("/", ".") for name in RULE_IMPORTS]
    visited = set()
    while pending:
        module = pending.pop()
        if module in visited:
            continue
        visited.add(module)
        assert module not in SQLITE_IMPLEMENTATIONS | NETWORK_IMPLEMENTATIONS, module
        path = sources[module]
        lines = eager_import_lines(tree(path))
        for line, dependency, _ in imported_modules(path):
            if line in lines:
                assert dependency.split(".")[0] not in TRANSPORTS, (module, line, dependency)
                assert dependency not in SQLITE_IMPLEMENTATIONS | NETWORK_IMPLEMENTATIONS, (module, line, dependency)
                assert dependency not in {"urllib.request", "urllib.error", "http.client"}, (module, line, dependency)
        pending.extend(graph[module])


def sqlite_error_uses_only(parsed):
    """The one legacy exception cannot be used to open a database or run SQL."""
    parents = {child: node for node in ast.walk(parsed) for child in ast.iter_child_nodes(node)}
    for node in ast.walk(parsed):
        if isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
            return False
        if isinstance(node, ast.Import):
            if any(alias.name == "sqlite3" and alias.asname for alias in node.names):
                return False
        if isinstance(node, ast.Name) and node.id == "sqlite3":
            parent = parents.get(node)
            if not isinstance(parent, ast.Attribute) or parent.attr != "Error":
                return False
    return True


def test_application_uses_public_repository_and_runtime_operations():
    failures = []
    accesses = Counter()
    for relative in APPLICATIONS:
        path = ROOT / relative
        for line, module, names in imported_modules(path):
            candidates = {module, *(module + "." + name for name in names or ())}
            if ((module.split(".")[0] in TRANSPORTS
                 or candidates & (SQLITE_IMPLEMENTATIONS | NETWORK_IMPLEMENTATIONS)
                 or module in {"urllib.request", "urllib.error", "http.client"}
                 or "forgepilot.artifacts.SqliteArtifactStore" in candidates)
                    and (relative, module) not in APPLICATION_IMPORT_EXCEPTIONS):
                failures.append(f"{relative}:{line}: concrete transport/storage import {module}")
        parsed = tree(path)
        if relative == "conversations/application.py" and not sqlite_error_uses_only(parsed):
            failures.append(f"{relative}: sqlite3 is allowed only for sqlite3.Error classification")
        for node in ast.walk(parsed):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in {"getattr", "setattr", "hasattr", "delattr"}
                    and len(node.args) > 1 and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str) and node.args[1].value.startswith("_")):
                failures.append(f"{relative}:{node.lineno}: dynamic private member {node.args[1].value}")
            if not isinstance(node, ast.Attribute) or not node.attr.startswith("_"):
                continue
            if node.attr in DATABASE_PRIVATE:
                failures.append(f"{relative}:{node.lineno}: database private member {node.attr}")
            if isinstance(node.value, ast.Name) and node.value.id in {"self", "cls"}:
                continue
            if node.attr.startswith("__"):
                continue
            key = (relative, ast.unparse(node))
            accesses[key] += 1
            limit = PRIVATE_ACCESS.get(key, (0, ""))[0]
            if accesses[key] > limit:
                failures.append(f"{relative}:{node.lineno}: unapproved private access {key[1]}")
    assert not failures, "\n".join(failures)


def test_cross_file_private_imports_have_exact_historical_bounds():
    observed = Counter()
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        for line, module, names in imported_modules(path):
            for name in names or ():
                if name.startswith("_") and not name.startswith("__"):
                    key = (relative, module.removeprefix("forgepilot."), name)
                    observed[key] += 1
                    assert observed[key] <= PRIVATE_IMPORTS.get(key, 0), f"{relative}:{line}: {key}"


def test_mission_stream_does_not_borrow_outbox_writer_database_privates():
    parsed = tree(ROOT / "mission_events.py")
    # The three existing outbox helpers are infrastructure in this mixed legacy
    # file. They are not authorization for stream/subscription SQL access.
    writer_accesses = {
        ("append_mission_event", "store._db"): 1,
        ("is_v2_mission", "store._db"): 1,
        ("publish_mission_event", "store._transaction"): 1,
    }
    observed = Counter()
    for declaration in parsed.body:
        for node in ast.walk(declaration):
            if not isinstance(node, ast.Attribute) or node.attr not in DATABASE_PRIVATE:
                continue
            if isinstance(node.value, ast.Name) and node.value.id == "self" and node.attr == "_lock":
                continue  # The stream's own subscriber-count lock, not SQLite.
            key = (getattr(declaration, "name", ""), ast.unparse(node))
            observed[key] += 1
            assert observed[key] <= writer_accesses.get(key, 0), key


def cycle_edges(graph):
    """Return every edge inside a strongly connected component."""
    visited, order = set(), []

    def visit(node):
        if node in visited:
            return
        visited.add(node)
        for target in graph[node]:
            visit(target)
        order.append(node)

    for node in graph:
        visit(node)
    reverse = {node: set() for node in graph}
    for source, targets in graph.items():
        for target in targets:
            reverse[target].add(source)
    visited.clear()
    result = set()

    def collect(node, component):
        if node in visited:
            return
        visited.add(node)
        component.add(node)
        for source in reverse[node]:
            collect(source, component)

    for node in reversed(order):
        if node in visited:
            continue
        component = set()
        collect(node, component)
        result.update((source, target) for source in component
                      for target in graph[source] if target in component)
    return result


def test_import_graph_cannot_add_or_expand_an_existing_cycle():
    actual = {(source.removeprefix("forgepilot."), target.removeprefix("forgepilot."))
              for source, target in cycle_edges(import_graph())}
    assert actual <= LAZY_CYCLE_EDGES, sorted(actual - LAZY_CYCLE_EDGES)
    assert not cycle_edges(import_graph(eager=True)), "Eager source imports must remain acyclic"


def test_artifact_failures_are_shared_contracts_with_legacy_import_identity():
    from forgepilot import artifacts
    from forgepilot.artifacts import sqlite_store, store
    from forgepilot.artifacts.validation import ArtifactProtocolError

    for name in ("ArtifactStorageError", "ArtifactIntegrityError"):
        contract = getattr(store, name)
        assert getattr(sqlite_store, name) is contract
        assert getattr(artifacts, name) is contract
        assert issubclass(contract, ArtifactProtocolError)


def test_cycle_guard_detects_an_expanded_component():
    graph = {"app": {"port"}, "port": set(), "sqlite": {"app"}}
    assert not cycle_edges(graph)
    graph["port"].add("sqlite")
    assert cycle_edges(graph) == {("app", "port"), ("port", "sqlite"), ("sqlite", "app")}


@pytest.mark.parametrize("source", [
    "import sqlite3\nconnection = sqlite3.connect('data.db')",
    "import sqlite3 as db\nconnection = db.connect('data.db')",
    "from sqlite3 import connect\nconnection = connect('data.db')",
    "import sqlite3\nconnect = getattr(sqlite3, 'connect')",
])
def test_legacy_sqlite_exception_cannot_authorize_connection_access(source):
    assert not sqlite_error_uses_only(ast.parse(source))


@pytest.mark.parametrize("source", ["import httpx", "from .. import sqlite_store", "from ..artifacts import sqlite_store"])
def test_rule_gate_rejects_relative_and_absolute_infrastructure_imports(monkeypatch, source):
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: source)
    assert rule_violations(ROOT / "artifacts" / "models.py", RULE_IMPORTS["artifacts/models.py"])
