"""Bind the original stage-six stop to a specific audited request correction."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


def _same(left, right):
    # JSON booleans must not stand in for integers in a frozen snapshot.
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def _read(directory, name):
    path = directory / name
    if (not path.is_relative_to(directory) or path.resolve(strict=True) != path
            or not path.is_file() or path.stat().st_nlink != 1):
        raise ValueError(f"Unsafe acceptance evidence: {name}")
    return path.read_bytes()


def _labels(plan):
    return [task["id"] + "-r" + str(n) for n in range(1, plan["rounds"] + 1) for task in plan["tasks"]]


def capture_stop(campaign: Path) -> dict:
    """Fingerprint original evidence and reconstruct the conservative held ledger."""
    directory = campaign.resolve(strict=True)
    files = {}

    def read(name):
        raw = _read(directory, name)
        files[name] = hashlib.sha256(raw).hexdigest()
        return json.loads(raw)

    plan = read("plan.json")
    old = read("summary.json")
    hold = read("budget-hold-audit.json")
    read("preflight.json")
    if (directory / "final-acceptance-report.json").exists():
        read("final-acceptance-report.json")
    labels = _labels(plan)
    runs = old["runs"]
    original = hold["original_request"]
    if (old.get("stop_reason") != "shared_budget_exhausted"
            or hold.get("no_more_paid_requests") is not True
            or hold.get("reason") != "timeout_without_usage_and_original_max_tokens_did_not_bound_reasoning_output"
            or old["budget"].get("held") is not True
            or old.get("planned_runs") != len(labels) or hold.get("fixed_denominator") != len(labels)
            or old.get("protocol_sha256") != hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
            or not old.get("model_configuration_sha256") or not old.get("launcher_revision")
            or [r["run"] for r in runs] != labels[:len(runs)]
            or not any(r["run"] == original["label"] and r["status"] == "failed" for r in runs)):
        raise ValueError("Original hold, stop, protocol and failed request do not agree")
    for run in runs:
        name = run["run"]
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", name) or not _same(read(name + "/result.json"), run):
            raise ValueError("Original result is inconsistent with the stopped summary")
        folder = directory / name
        # The harness writes per-run artifacts directly in the sample directory.
        # Hash these as raw bytes; they can include arbitrary provider text.
        for path in folder.iterdir():
            relative = path.relative_to(directory).as_posix()
            if relative not in files:
                files[relative] = hashlib.sha256(_read(directory, relative)).hexdigest()
    budget = old["budget"]
    rows = budget["requests"]
    matches = [row for row in rows if row["id"] == original["id"]]
    committed = sum(r["charged"] if r["charged"] is not None else r["reserved"] for r in rows)
    known = committed - original["charged"]
    limit = plan["token_cap"]
    if (len(matches) != 1 or not _same(matches[0], original)
            or original["estimated"] != 1 or original["status"] != "request_error"
            or budget["charged_or_reserved"] != committed or budget["limit"] != limit
            or known != hold["known_reported_tokens"]
            or known + hold["conservative_final_charge"] != limit):
        raise ValueError("Original request and conservative adjustment do not explain the ledger")
    held_row = {**original, "charged": hold["conservative_final_charge"],
                "status": "unknown_usage_campaign_hold"}
    return {"campaign": str(directory), "request_id": original["id"], "files": files,
            "held_request": held_row, "limit": limit,
            "original_requests": [held_row if r["id"] == original["id"] else r for r in rows]}


def _derive(campaign, ledger, runtime_identity):
    audits = ledger.reconciliation_audits()
    if len(audits) != 1:
        raise ValueError("The original campaign stop requires its unique request reconciliation")
    audit = audits[0]
    anchors = capture_stop(campaign)
    digest = audit["audit_sha256"]
    if (not re.fullmatch("[0-9a-f]{64}", digest)
            or audit["request_id"] != anchors["request_id"] or not _same(audit["anchors"], anchors)
            or audit["summary_sha256"] != anchors["files"]["summary.json"]
            or audit["result_sha256"] != anchors["files"][anchors["held_request"]["label"] + "/result.json"]
            or not _same(audit["before"], anchors["held_request"])
            or not _same(audit["runtime_identity"], runtime_identity)):
        raise ValueError("Reconciliation does not match frozen evidence or runtime identity")
    expected = {**audit["before"], "status": "reconciled_usage", "estimated": 0,
                "charged": audit["usage"]["total_tokens"]}
    rows = [expected if row["id"] == audit["request_id"] else row for row in anchors["original_requests"]]
    total = sum(row["charged"] if row["charged"] is not None else row["reserved"] for row in rows)
    if not _same(audit["after"], expected) or total != audit["charged_or_reserved_after"]:
        raise ValueError("Reconciled request does not explain post-reconciliation usage")
    return {"audit_sha256": digest, "campaign": anchors["campaign"],
            "runtime_identity": runtime_identity, "original_files_sha256": anchors["files"],
            "original_stop_reason": "shared_budget_exhausted", "request_id": audit["request_id"],
            "budget_after_reconciliation": {"limit": anchors["limit"], "held": audit["held_after"],
                                           "charged_or_reserved": total, "requests": rows}}


def materialize_recovery(campaign, ledger, runtime_identity) -> Path:
    """Publish or verify an immutable generation. This does not authorize reserve()."""
    directory = campaign.resolve(strict=True)
    state = _derive(directory, ledger, runtime_identity)
    name = f"recovery-{state['audit_sha256']}.json"
    data = (json.dumps(state, indent=2, sort_keys=True) + "\n").encode()
    target = directory / name
    # Serialize competing publishers with the ledger writer lock. After a
    # crash the final name is either absent or holds the complete synced file.
    with ledger.transaction():
        if not os.path.lexists(target):
            temp = tempfile.NamedTemporaryFile(dir=directory, prefix=".recovery-", delete=False)
            try:
                with temp:
                    temp.write(data)
                    temp.flush()
                    os.fsync(temp.fileno())
                os.replace(temp.name, target)
            finally:
                Path(temp.name).unlink(missing_ok=True)
        if _read(directory, name) != data:
            raise ValueError("Existing recovery manifest differs from the audited generation")
    return target


def open_recovery(campaign, ledger, runtime_identity):
    """Check durable manifest and progress before enabling this paid connection."""
    directory = campaign.resolve(strict=True)
    manifest = _derive(directory, ledger, runtime_identity)
    digest = manifest["audit_sha256"]
    stored = json.loads(_read(directory, f"recovery-{digest}.json"))
    if not _same(manifest, stored):
        raise ValueError("Stored recovery manifest is inconsistent with audited evidence")
    old = json.loads(_read(directory, "summary.json"))
    progress = directory / f"progress-{digest}.json"
    current = ledger.snapshot()
    frozen = manifest["budget_after_reconciliation"]
    if frozen["held"] or current["held"]:
        raise ValueError("Reconciled ledger remains held")
    previous = {**old, "recovery_audit_sha256": digest, "budget": frozen}
    previous.pop("stop_reason")
    if progress.exists():
        previous = json.loads(_read(directory, progress.name))
        fields = ("model_configuration_sha256", "launcher_revision", "protocol_sha256", "planned_runs", "models")
        if (previous.get("recovery_audit_sha256") != digest
                or any(not _same(previous.get(key), old.get(key)) for key in fields)
                or not _same(previous.get("runtime_identity"), runtime_identity)
                or previous.get("stop_reason") or not _same(previous["runs"][:len(old["runs"])], old["runs"])):
            raise ValueError("Recovery progress is stopped or inconsistent with the original runs")
    if not _same(previous["budget"], current):
        raise ValueError("Ledger changed outside the saved recovery progress")
    by_id = {row["id"]: row for row in current["requests"]}
    if any(not _same(by_id.get(row["id"]), row) for row in frozen["requests"]):
        raise ValueError("A frozen request changed after reconciliation")
    labels = _labels(json.loads(_read(directory, "plan.json")))
    runs = previous["runs"]
    if [run["run"] for run in runs] != labels[:len(runs)]:
        raise ValueError("Recovery progress violates the frozen run order")
    new_runs = runs[len(old["runs"]):]
    original_ids = {row["id"] for row in frozen["requests"]}
    resumed_requests = [row for row in current["requests"] if row["id"] not in original_ids]
    completed_labels = {run["run"] for run in new_runs}
    if any(row["label"] not in completed_labels for row in resumed_requests):
        raise ValueError("Reserved requests are missing from recorded recovery runs")
    for run in new_runs:
        calls = [row for row in resumed_requests if row["label"] == run["run"]]
        if (run.get("reason") == "llm_budget" or "llm_budget" in run.get("errors", [])
                or len(calls) != run["network_requests"] or any(row["charged"] is None for row in calls)
                or not _same(run, json.loads(_read(directory, run["run"] + "/result.json")))):
            raise ValueError("Recovered run stopped on budget or does not match its result")
    if any((directory / label).exists() for label in labels[len(runs):]):
        raise ValueError("An incomplete run directory requires inspection")
    ledger._authorize_verified_recovery(digest, current)
    return previous, progress
