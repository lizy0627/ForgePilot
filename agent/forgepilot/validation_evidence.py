"""Pipeline evidence required for a ValidationReport to certify a change."""
from .artifacts.models import ValidationReport


PIPELINE_STAGES = (
    "Workspace Integrity", "Test Integrity", "CMake Configure", "Build", "CTest",
    "Protected Oracle", "Diff Scope Check", "ValidationReport",
)
PUBLIC_PIPELINE_STAGES = (*PIPELINE_STAGES[:5], "Protected Oracle (not run)", *PIPELINE_STAGES[6:])


def validation_passed(report: ValidationReport, *, require_oracle: bool = True) -> bool:
    oracle_passed = report.hidden_oracle == "passed"
    if not oracle_passed and (require_oracle or report.hidden_oracle != "not_run"):
        return False
    expected_stages = PIPELINE_STAGES if oracle_passed else PUBLIC_PIPELINE_STAGES
    kinds = {command.kind for command in report.commands}
    return (
        report.status == "passed" and report.failure_classification == "none"
        and report.workspace_integrity == report.test_integrity == report.diff_scope == "passed"
        and report.tests_passed > 0 and report.tests_failed == 0
        and tuple(report.pipeline_stages) == expected_stages
        and kinds >= {"configure", "build", "test", "integrity"}
        and ("protected_test" in kinds) == oracle_passed
        and all(command.exit_code == 0 for command in report.commands)
    )
