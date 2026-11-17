"""Verify the post-fix preflight report against independent HTTP ledger observations."""
import json
from math import isfinite


def total_output_parameter(model):
    if model.startswith("doubao-seed-2-1-"):
        return "max_completion_tokens"
    if model.startswith("deepseek-v4-1-"):
        return "max_tokens"
    raise ValueError("Unverified acceptance model")


def verify_probe(check, attempt):
    if (not check["request_id"] or check["request_id"] != attempt["request_id"]
            or attempt["status_code"] != 200 or attempt["failure_reason"] is not None
            or check["model"] != attempt["model"] or check["kind"] != attempt["kind"]):
        raise ValueError("Expected a successful matching wire attempt")
    prompt = attempt["prompt_tokens"]
    completion = attempt["completion_tokens"]
    total = attempt["total_tokens"]
    if (type(prompt) is not int or prompt <= 0 or type(total) is not int or total <= 0
            or attempt["reported_tokens"] != total or attempt["estimated_tokens"] != 0):
        raise ValueError("Revalidation needs provider-reported usage")
    if check["kind"] == "embedding":
        if (completion is not None and (type(completion) is not int or completion != 0)
                or prompt != total or attempt["output_parameter"] is not None or attempt["output_limit"] is not None):
            raise ValueError("Unexpected embedding usage or output limit")
    elif (check["kind"] != "chat" or type(completion) is not int or completion <= 0
            or total != prompt + completion or attempt["output_parameter"] != total_output_parameter(check["model"])
            or type(attempt["output_limit"]) is not int or not 1 <= attempt["output_limit"] <= 1024
            or check["output_limit"] != attempt["output_limit"] or completion > attempt["output_limit"]):
        raise ValueError("Chat output limit or usage contract failed")


def verify_revalidation(result, attempts, saved_models, *, require_embedding=False):
    """Require all configured profile checks, both routes and any required embedding."""
    try:
        profiles = {item["profile"]: item["model"] for item in saved_models}
        required = ["connection:" + p for p in profiles] + ["chat", "summary"]
        if require_embedding or result["embedding_enabled"] is True:
            required.append("embedding")
        checks = result["checks"]
        if (not profiles or len(profiles) != len(saved_models) or result["run"] != "revalidation"
                or result["status"] != "passed" or result["first_failure_reason"] is not None
                or result["final_stop_reason"] is not None or type(result["embedding_enabled"]) is not bool
                or type(result["duration_seconds"]) not in (int, float)
                or not isfinite(result["duration_seconds"]) or result["duration_seconds"] < 0
                or type(result["network_requests"]) is not int or result["network_requests"] != len(attempts)
                or len(attempts) != len(required)
                or json.dumps(result["wire_attempts"], sort_keys=True) != json.dumps(attempts, sort_keys=True)
                or [check["name"] for check in checks] != required
                or len({a["request_id"] for a in attempts}) != len(attempts)):
            raise ValueError("Incomplete revalidation probe sequence")
        for check, attempt in zip(checks, attempts, strict=True):
            if check["status"] != "passed":
                raise ValueError("Revalidation probe did not complete")
            if check["name"] == "embedding":
                if check["kind"] != "embedding" or not result["embedding_enabled"]:
                    raise ValueError("Missing configured embedding probe")
            elif (check["kind"] != "chat" or profiles.get(check["profile"]) != check["model"]
                  or check["name"].startswith("connection:") and check["name"] != "connection:" + check["profile"]):
                raise ValueError("Probe does not match the frozen model profile")
            verify_probe(check, attempt)
        if (type(result["reported_tokens"]) is not int or type(result["estimated_tokens"]) is not int
                or result["reported_tokens"] != sum(a["reported_tokens"] for a in attempts)
                or result["estimated_tokens"] != 0):
            raise ValueError("Revalidation token totals differ from ledger")
    except (KeyError, TypeError, AttributeError, OverflowError) as exc:
        raise ValueError("Incomplete revalidation evidence") from exc
