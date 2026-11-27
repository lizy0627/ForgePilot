"""Use the saved desktop model routes for a benchmark invocation."""
import json
import os
from pathlib import Path
import sys

from .desktop_configuration import InvalidCredentialReference, default_config_path, load_benchmark_settings


_CONFIG_ERRORS = {
    "config_missing": "Saved model configuration is missing.",
    "model_config": "Saved model endpoint or task model is missing.",
    "model_config_task": "Saved model endpoint or task model is missing.",
    "credential_reference_invalid": "Invalid saved credential reference.",
    "credential_required": "Saved encrypted API key is missing or invalid.",
    "credential_invalid": "Saved encrypted API key is missing or invalid.",
    "credential_unavailable": "Encrypted API key is unavailable to this Windows user.",
}


def run_saved_benchmark(runner=None):
    try:
        invocation = json.loads(os.environ.pop("FORGEPILOT_BENCHMARK_INVOCATION"))
        args = invocation["arguments"] or []
        config = default_config_path(Path(invocation["root"]))
        settings, routes_valid = load_benchmark_settings(
            config, review_model=invocation["review_model"], task_model_family=invocation["task_model_family"],
            review_model_family=invocation["review_model_family"])
    except (OSError, ValueError, TypeError, KeyError) as exc:
        code = "credential_reference_invalid" if isinstance(exc, InvalidCredentialReference) else str(exc)
        message = _CONFIG_ERRORS.get(code, "Saved model configuration or encrypted API key is unavailable.")
        print(message, "Run Configure-ForgePilot.cmd.", file=sys.stderr)
        return 1

    if runner is None:
        review = "configured" if settings["FORGEPILOT_REVIEW_MODEL"] else "missing"
        routes = "configured_distinct" if routes_valid else "missing_or_invalid"
        print("saved_model_config=present")
        print("encrypted_api_key=decryptable")
        print(f"review_model={review}")
        print(f"model_routes={routes}")
        print("api_connectivity=not_checked")
        print("benchmark_model_tokens=0")
        return 0 if routes_valid else 2

    if not routes_valid:
        print("Formal benchmark requires a configured Reviewer model and two distinct verified model families.",
              file=sys.stderr)
        return 1
    # This is the evaluation process, after toolchain setup. The calling shell
    # and setup scripts never receive the decrypted credential.
    os.environ.update(settings)
    return runner(args, require_real=True)
