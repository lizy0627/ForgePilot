"""File expansion is an additive revision of an existing writer step."""
from uuid import uuid4

import pytest

from forgepilot.artifacts import content_sha256
from forgepilot.errors import ForgePilotError
from forgepilot.orchestration.scope_expansion import ScopeExpansionFile, _revised_contents
from forgepilot.repository import Repository
from test_artifact_protocol import plan, task_spec


def file_request(path="include/worker.hpp", **changes):
    return ScopeExpansionFile.model_validate({"path": path, "reason": "Declare the cancellation entry point",
        "plan_step_id": "STEP-1", "acceptance_criteria_ids": ["AC-1"]} | changes)


def revise(root, files):
    original = task_spec()
    spec = original.model_copy(update={
        "allowed_scope": original.allowed_scope.model_copy(update={"max_changed_files": 1}),
        "forbidden_scope": ("private/**",)})
    return _revised_contents(spec, plan(), files, Repository(root, "rg"), str(uuid4()))


def test_expansion_budget_increases_by_at_most_the_newly_approved_files(tmp_path):
    spec, revised = revise(tmp_path, [file_request()])
    # Two files were listed in the original plan but only one could change.
    assert spec.allowed_scope.max_changed_files == 2
    assert spec.allowed_scope.path_patterns == ("src/**", "tests/**", "include/worker.hpp")
    assert revised.steps[0].expected_files == ("src/window.cpp", "include/worker.hpp")
    assert revised.steps[1] == plan().steps[1]
    mapped, = revised.steps[0].file_criteria
    assert mapped.path == "include/worker.hpp" and mapped.acceptance_criteria_ids == ("AC-1",)


def test_expanded_plan_hash_binds_criteria_and_reason(tmp_path):
    original = plan()
    step = original.steps[0].model_copy(update={"acceptance_criteria_ids": ("AC-1", "AC-2")})
    expanded = original.model_copy(update={"steps": (step, original.steps[1])})
    spec_id = str(uuid4())

    def plan_hash(request):
        _, revised = _revised_contents(task_spec(), expanded,
            [request], Repository(tmp_path, "rg"), spec_id)
        return content_sha256(revised)

    first = plan_hash(file_request())
    assert plan_hash(file_request(acceptance_criteria_ids=["AC-2"])) != first
    assert plan_hash(file_request(reason="A different reason")) != first
    assert "file_criteria" not in original.model_dump(mode="json")["steps"][0]


@pytest.mark.parametrize("changes", [{"reason": "   "}, {"reason": "Header\x00file"},
                                     {"path": "src/\u202e.cpp"}])
def test_expansion_requires_reviewable_paths_and_reasons(changes):
    with pytest.raises(ValueError):
        file_request(**changes)


@pytest.mark.parametrize("requests", [
    [file_request("include/*.hpp")], [file_request("include/[01].hpp")],
    [file_request("../external.hpp")], [file_request("private/new.hpp")],
    [file_request("tests/existing_test.cpp")], [file_request("src/window.cpp")],
    [file_request(acceptance_criteria_ids=["AC-2"])], [file_request(plan_step_id="STEP-99")],
    [file_request(), file_request()],
])
def test_expansion_rejects_invalid_additions(tmp_path, requests):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "existing_test.cpp").write_text("int main() {}")
    with pytest.raises((ValueError, ForgePilotError)):
        revise(tmp_path, requests)
