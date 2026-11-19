"""Source dependency guards complement the compiled Qt behavior tests.

These checks cover the Mission presentation slice; v1 transport sharing remains
explicit in AgentApiClient, SseClient and Protocol, outside the projection layer.
"""

from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[2] / "client/src"
WORKBENCH = ROOT / "workbench"
INCLUDES = re.compile(r'^\s*#\s*include\s*[<"]([^>"\n]+)[>"]', re.MULTILINE)
CAPABILITIES = re.compile(
    r"\b(?:QNetwork\w*|Q(?:Tcp|Udp|Ssl)\w*|QSql\w*|QProcess|"
    r"AgentApiClient|SseClient|MissionController|MissionSnapshotLoader)\b"
)
MUTATIONS = re.compile(
    r"\b(?:createMission|approveMission|cancelMission|retryMission|"
    r"resumeMission|deliverMission|abortPending)\s*\("
)
PROJECTIONS = re.compile(
    r"\b(?:getMission|getMissionGraph|getMissionArtifacts|getMissionDiff|"
    r"getMissionValidation|getMissionScopeExpansion)\s*\("
)


def source(text):
    # Keep string literals (including #include paths), remove only comments.
    return re.sub(r'("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')|/\*.*?\*/|//[^\n]*',
                  lambda match: match[1] or "", text, flags=re.DOTALL)


def projection_dependencies(start, root):
    """Follow local includes, including their implementations, to catch bridges."""
    pending, seen, errors = list(start), set(), []
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        text = source(path.read_text(encoding="utf-8-sig"))
        for match in CAPABILITIES.finditer(text):
            errors.append(f"{path.relative_to(root)}: forbidden projection dependency {match[0]}")
        for include in INCLUDES.findall(text):
            local = (path.parent / include).resolve()
            if not local.is_file():
                local = (root / include).resolve()
            if local.is_file() and local.is_relative_to(root.resolve()):
                pending.append(local)
                implementation = local.with_suffix(".cpp")
                if implementation.is_file():
                    pending.append(implementation)
    return errors


def test_mission_projections_and_protocol_have_no_transport_or_controller_dependency():
    files = [*WORKBENCH.glob("*Model.h"), *WORKBENCH.glob("*Model.cpp"),
             WORKBENCH / "MissionProtocol.h", WORKBENCH / "MissionProtocol.cpp"]
    assert files
    assert projection_dependencies(files, ROOT) == []


def test_snapshot_reader_cannot_mutate_or_abort_another_consumers_requests():
    for suffix in ("h", "cpp"):
        text = source((WORKBENCH / f"MissionSnapshotLoader.{suffix}").read_text(encoding="utf-8"))
        assert not MUTATIONS.search(text)
        assert not re.search(r"\b(?:QNetwork\w*|SseClient|MissionController|\w+Model)\b", text)


def test_controller_delegates_projection_reads_to_snapshot_loader():
    text = source((WORKBENCH / "MissionController.cpp").read_text(encoding="utf-8"))
    assert not PROJECTIONS.search(text)


@pytest.mark.parametrize("dependency", ["AgentApiClient", "QNetworkAccessManager", "QSqlDatabase"])
def test_projection_guard_rejects_indirect_infrastructure_dependency(tmp_path, dependency):
    (tmp_path / "Projection.h").write_text('#include "Bridge.h"\n', encoding="utf-8")
    (tmp_path / "Bridge.h").write_text("// bridge\n", encoding="utf-8")
    (tmp_path / "Bridge.cpp").write_text(f"{dependency} *resource;\n", encoding="utf-8")
    assert projection_dependencies([tmp_path / "Projection.h"], tmp_path)


@pytest.mark.parametrize("call", ["approveMission(task)", "abortPending()", "resumeMission(task)"])
def test_reader_guard_rejects_mutation_and_global_cancellation(call):
    assert MUTATIONS.search(source(f"api->{call}; // injected violation"))


def test_source_guard_ignores_comments_but_preserves_include_paths():
    text = source('// AgentApiClient\n/* QNetworkReply */\n#include "Bridge.h"\n')
    assert not CAPABILITIES.search(text)
    assert INCLUDES.findall(text) == ["Bridge.h"]
