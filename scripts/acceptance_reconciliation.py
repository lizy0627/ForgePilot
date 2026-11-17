"""Offline matching of a trusted, per-request provider detail to the original failed call."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

from forgepilot.models.accounting import response_hash


ROOT = Path(__file__).resolve().parents[1]


def runtime_identity() -> dict[str, str]:
    """Fingerprint the checkout and reject modules loaded from a different checkout."""
    modules = {
        "forgepilot.models.openai": "agent/forgepilot/models/openai.py",
        "forgepilot.models.gateway": "agent/forgepilot/models/gateway.py",
        "forgepilot.models.accounting": "agent/forgepilot/models/accounting.py",
        "acceptance_budget": "scripts/acceptance_budget.py",
        "acceptance_reconciliation": "scripts/acceptance_reconciliation.py",
        "acceptance_recovery": "scripts/acceptance_recovery.py",
        "run_product_acceptance": "scripts/run_product_acceptance.py",
    }
    identity = {}
    for name, relative in modules.items():
        expected = ROOT / relative
        module = sys.modules.get(name)
        if module is not None and Path(module.__file__).resolve(strict=True) != expected:
            raise ValueError(f"Acceptance module loaded outside this checkout: {name}")
        identity[relative] = hashlib.sha256(expected.read_bytes()).hexdigest()
    return identity


def _unique(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate key in reconciliation evidence")
    return result


def _invalid_constant(value):
    raise ValueError(f"Invalid JSON number: {value}")


def _read(path):
    raw = path.read_bytes()
    return json.loads(raw, object_pairs_hook=_unique, parse_constant=_invalid_constant), hashlib.sha256(raw).hexdigest()


def _fields(value, *names):
    if not isinstance(value, dict) or value.keys() != set(names):
        raise ValueError("Missing or unexpected reconciliation fields: " + ", ".join(names))


def _time(value):
    if not isinstance(value, str):
        raise ValueError("Expected timestamp with UTC offset")
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("Expected timestamp with UTC offset")
    return stamp.astimezone(timezone.utc)


def _match_detail(campaign, original, request_id, detail):
    """Correlate content from the provider trace with independently recorded gateway input."""
    label = original["label"]
    if not isinstance(label, str) or not re.fullmatch(r"[a-z0-9-]+-r[1-3]", label):
        raise ValueError("Invalid acceptance run label")
    summary_path, result_path = campaign / "summary.json", campaign / label / "result.json"
    for path in (summary_path, result_path):
        if path.resolve(strict=True) != path or path.stat().st_nlink != 1:
            raise ValueError("Unsafe original acceptance evidence path")
    summary, summary_sha = _read(summary_path)
    result, result_sha = _read(result_path)
    # The earlier summary may still contain the pre-hold provisional charge.
    # Only compare the immutable reservation and request identity here.
    rows = [r for r in summary["budget"]["requests"]
            if r["label"] == label or r["created_at"] == original["created_at"] or r["id"] == request_id]
    if (len(rows) != 1 or rows[0]["id"] != request_id or rows[0]["status"] != "request_error"
            or type(rows[0]["estimated"]) is not int or rows[0]["estimated"] != 1
            or any(rows[0][k] != original[k] for k in ("label", "created_at", "reserved"))):
        raise ValueError("Original summary does not uniquely identify this reservation")
    runs = [run for run in summary["runs"] if run["run"] == label]
    if (len(runs) != 1 or result != runs[0] or result["status"] != "failed"
            or type(result["network_requests"]) is not int or result["network_requests"] != 1):
        raise ValueError("Expected one failed run with one network request")
    calls = [row for row in result["accounting"] if row["status"] != "budget"]
    if len(calls) != 1 or calls[0]["status"] != "network" or calls[0]["attempt"] != "primary":
        raise ValueError("Ambiguous original accounting attempts")
    call = calls[0]
    wire = detail["request"]
    if not isinstance(wire["messages"], list) or not wire["messages"] or wire["model"] != call["model"]:
        raise ValueError("Provider request does not match the original model or input")
    tools = []
    if not isinstance(wire.get("tools", []), list):
        raise ValueError("Invalid provider tool list")
    for tool in wire.get("tools", []):
        _fields(tool, "type", "function")
        function = tool["function"]
        _fields(function, "name", "description", "parameters")
        if tool["type"] != "function":
            raise ValueError("Unexpected provider tool type")
        tools.append({"name": function["name"], "description": function["description"],
                      "input_schema": function["parameters"]})
    input_hash = response_hash({"messages": wire["messages"], "tools": tools})
    started_at = _time(detail["started_at"])
    if (input_hash != call["input_hash"]
            or started_at.strftime("%Y-%m-%d %H:%M:%S") != original["created_at"]):
        raise ValueError("Provider input or UTC request second does not match local accounting")
    return {"summary_sha256": summary_sha, "result_sha256": result_sha, "input_hash": input_hash,
            "model": call["model"], "call_id": call["call_id"], "provider_started_at": started_at.isoformat()}


def load_reconciliation(manifest_path: Path, evidence_path: Path, campaign: Path) -> dict:
    """Validate two independent inputs, returning only content-free audit fields.

    The operator supplies an authentic provider record (or its faithful offline
    export) with the original wire request and actual token parts. SHA-256 pins
    the supplied bytes, not the provider's authorship.
    """
    campaign = campaign.resolve(strict=True)
    manifest, manifest_sha = _read(manifest_path)
    _fields(manifest, "campaign", "request_id", "expected", "evidence_sha256", "operated_at", "runtime_identity")
    if (manifest["campaign"] != str(campaign) or manifest["runtime_identity"] != runtime_identity()
            or not isinstance(manifest["request_id"], str) or not re.fullmatch("[a-f0-9]{32}", manifest["request_id"])):
        raise ValueError("Unexpected campaign, local request or runtime identity")
    original = manifest["expected"]
    _fields(original, "label", "created_at", "reserved", "estimated", "status", "charged")
    if (type(original["estimated"]) is not int or original["estimated"] != 1
            or original["status"] != "unknown_usage_campaign_hold"
            or type(original["reserved"]) is not int or original["reserved"] < 1
            or type(original["charged"]) is not int or original["charged"] < 0):
        raise ValueError("Expected an estimated charge for a failed request")
    detail, evidence_sha = _read(evidence_path)
    if manifest["evidence_sha256"] != evidence_sha:
        raise ValueError("Provider evidence SHA-256 mismatch")
    _fields(detail, "kind", "provider", "provider_request_id", "started_at", "request", "usage")
    provider_id = detail["provider_request_id"]
    if (detail["kind"] != "provider_request_usage_v1" or detail["provider"] != "ark.cn-beijing.volces.com"
            or not isinstance(provider_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}", provider_id)):
        raise ValueError("Expected a single identifiable Ark provider usage record")
    usage = detail["usage"]
    _fields(usage, "input_tokens", "reasoning_tokens", "answer_tokens", "total_tokens")
    if (any(type(n) is not int or n < 0 or n > 2**63 - 1 for n in usage.values())
            or sum(usage[k] for k in ("input_tokens", "reasoning_tokens", "answer_tokens")) != usage["total_tokens"]):
        raise ValueError("Invalid or incomplete provider token counts")
    operated_at = _time(manifest["operated_at"])
    if not _time(detail["started_at"]) <= operated_at <= datetime.now(timezone.utc):
        raise ValueError("Operation timestamp precedes the request or is in the future")
    try:
        link = _match_detail(campaign, original, manifest["request_id"], detail)
    except (KeyError, TypeError) as exc:
        raise ValueError("Missing original request or provider metadata") from exc
    return {**manifest, **link, "manifest_sha256": manifest_sha, "provider": detail["provider"],
            "provider_request_id": provider_id, "operated_at": operated_at.isoformat(), "usage": usage}
