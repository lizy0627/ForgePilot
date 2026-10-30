"""Benchmark arithmetic and reports over every scheduled run, including failures."""

import csv
import io
import json
import math
from collections import defaultdict


def _rate(items, predicate):
    count = len(items)
    passed = sum(bool(predicate(item)) for item in items)
    return {"passed": passed, "denominator": count,
            "value": passed / count if count else None}


def _known(items, key):
    values = []
    for item in items:
        value = item.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"Invalid benchmark measurement: {key}")
        values.append(value)
    return values


def _average(items, key):
    values = _known(items, key)
    return {"value": sum(values) / len(values) if values else None,
            "known": len(values), "unknown": len(items) - len(values), "eligible": len(items)}


def _percentile(items, key, percentile):
    values = sorted(_known(items, key))
    # Nearest-rank percentile. For small samples P95 is normally the maximum.
    rank = max(0, math.ceil(percentile * len(values)) - 1)
    return {"value": values[rank] if values else None,
            "known": len(values), "unknown": len(items) - len(values), "eligible": len(items)}


def _scheduled_rows(rows, expected_repetitions, selected_tasks, categories):
    """Represent each planned slot once, even when execution stopped early."""
    if expected_repetitions is None:
        return list(rows)
    if not isinstance(expected_repetitions, int) or isinstance(expected_repetitions, bool) or expected_repetitions < 1:
        raise ValueError("Invalid benchmark repetition count")
    ids = list(selected_tasks) if selected_tasks is not None else list(dict.fromkeys(row["id"] for row in rows))
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate selected benchmark task")
    indexed = {}
    for row in rows:
        task_id, index = row["id"], row.get("run_index")
        if task_id not in ids or not isinstance(index, int) or isinstance(index, bool) or not 1 <= index <= expected_repetitions:
            raise ValueError("Invalid benchmark task or run index")
        key = (task_id, index)
        if key in indexed:
            raise ValueError("Duplicate benchmark run index")
        indexed[key] = row
    scheduled = []
    for task_id in ids:
        category = (categories or {}).get(task_id)
        if category is None:
            category = next((row["category"] for row in rows if row["id"] == task_id), "unknown")
        for index in range(1, expected_repetitions + 1):
            row = indexed.get((task_id, index))
            if row is None:
                row = {"id": task_id, "category": category, "run_index": index,
                       "success": False, "first_pass_success": False, "repair_success": False,
                       "reason": "missing_result", "state": "not_run",
                       "build_status": "not_applicable" if category == "调用链分析" else "not_run",
                       "ctest_status": "not_applicable" if category == "调用链分析" else "not_run",
                       "test_status": "not_applicable" if category == "调用链分析" else "not_run",
                       "oracle_status": "not_applicable" if category == "调用链分析" else "not_run",
                       "integrity_status": "not_run", "scope_valid": None,
                       "test_count": 0, "tool_calls": None, "total_tokens": None,
                       "latency_ms": None, "agent_latency_ms": None, "repair_rounds": None}
            elif row["category"] != category:
                raise ValueError("Task category changed between runs")
            scheduled.append(row)
    return scheduled


def task_results(rows, *, expected_repetitions=None, selected_tasks=None, categories=None):
    """One task row per selected ID; the formal stable rule is exactly 2/3.

    A missing scheduled run is a failure. Duplicate run indices are rejected so
    a caller cannot overwrite or select one of several outcomes after the fact.
    """
    rows = _scheduled_rows(rows, expected_repetitions, selected_tasks, categories)
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["id"]].append(row)
    ids = list(selected_tasks) if selected_tasks is not None else list(grouped)
    if len(ids) != len(set(ids)) or set(grouped) - set(ids):
        raise ValueError("Selected benchmark tasks and result rows disagree")
    results = []
    for task_id in ids:
        attempts = grouped[task_id]
        category = (categories or {}).get(task_id, attempts[0].get("category") if attempts else "unknown")
        if any(row.get("category") != category for row in attempts):
            raise ValueError("Task category changed between runs")
        indices = [row.get("run_index") for row in attempts]
        if expected_repetitions is not None:
            if not isinstance(expected_repetitions, int) or isinstance(expected_repetitions, bool) or expected_repetitions < 1:
                raise ValueError("Invalid benchmark repetition count")
            if any(not isinstance(i, int) or isinstance(i, bool) or not 1 <= i <= expected_repetitions for i in indices):
                raise ValueError("Missing or invalid benchmark run index")
            if len(set(indices)) != len(indices):
                raise ValueError("Duplicate benchmark run index")
            scheduled = expected_repetitions
        else:
            scheduled = len(attempts)
        successful = sum(row.get("success") is True for row in attempts)
        first_pass = sum(row.get("success") is True and row.get("first_pass_success") is True for row in attempts)
        repair_success = sum(row.get("success") is True and row.get("repair_success") is True for row in attempts)
        results.append({"id": task_id, "category": category,
                        "successful_runs": successful,
                        "completed_runs": sum(row.get("success") is True or row.get("state") != "not_run"
                                              or row.get("reason") not in {"not_run", "missing_result"}
                                              for row in attempts),
                        "scheduled_runs": scheduled, "run_success_rate": successful / scheduled if scheduled else None,
                        "first_pass_successful_runs": first_pass,
                        "repair_successful_runs": repair_success,
                        "stable_success": successful >= 2 if scheduled == 3 else
                            (successful == scheduled if scheduled else False),
                        "stability_evidence": "two_of_three_rule" if scheduled == 3 else "provisional"})
    return results


def summarize(rows, *, expected_repetitions=None, selected_tasks=None, categories=None):
    """Use scheduled denominators; unknown usage is never silently zero."""
    rows = _scheduled_rows(rows, expected_repetitions, selected_tasks, categories)
    tasks = task_results(rows, expected_repetitions=expected_repetitions,
                         selected_tasks=selected_tasks, categories=categories)
    modifications = [row for row in rows if row["category"] != "调用链分析"]
    repair_attempts = [row for row in modifications if (row.get("repair_rounds") or 0) > 0]
    return {
        "Task Success Rate": _rate(tasks, lambda task: task["stable_success"]),
        "Run Success Rate": _rate(rows, lambda row: row.get("success") is True),
        "First-pass Success Rate": _rate(rows, lambda row: row.get("success") is True and row.get("first_pass_success") is True),
        "Post-repair Success Rate": _rate(repair_attempts, lambda row: row.get("success") is True and row.get("repair_success") is True),
        "Build Pass Rate": _rate(modifications, lambda row: row["build_status"] == "passed"),
        "Test Pass Rate": _rate(modifications, lambda row: row["test_status"] == "passed" and row["test_count"] > 0),
        "CTest Pass Rate": _rate(modifications, lambda row: row.get("ctest_status") == "passed"),
        "Oracle Pass Rate": _rate(modifications, lambda row: row.get("oracle_status") == "passed"),
        "Integrity Pass Rate": _rate(rows, lambda row: row.get("integrity_status") == "passed"),
        "Average Tool Calls": _average(rows, "tool_calls"),
        "Average Token Usage": _average(rows, "total_tokens"),
        "Average Latency": _average(rows, "latency_ms"),
        "Average Agent Latency": _average(rows, "agent_latency_ms"),
        "P50 Latency": _percentile(rows, "latency_ms", 0.50),
        "P95 Latency": _percentile(rows, "latency_ms", 0.95),
        "Average Repair Rounds": _average(modifications, "repair_rounds"),
        "phase_counts": {phase: {state: sum(row.get(phase) == state for row in rows)
            for state in ("passed", "failed", "not_run", "not_applicable")}
            for phase in ("build_status", "ctest_status", "oracle_status", "integrity_status", "test_status")},
    }


def _evidence_label(metadata):
    label = metadata.get("evidence_label", metadata.get("mode", "unknown"))
    if label not in {"offline", "fake", "retrieval", "real"}:
        raise ValueError("Benchmark evidence label must be offline, fake, retrieval or real")
    return label


def _cell(value):
    return (str(value).replace("\\", "\\\\").replace("|", "\\|")
            .replace("\n", " ").replace("\r", " ")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _metric_row(name, item):
    if "denominator" in item:
        value = "N/A" if item["value"] is None else f"{item['value']:.2%}"
        coverage = f"{item['passed']}/{item['denominator']}"
    else:
        value = "N/A" if item["value"] is None else f"{item['value']:.2f}"
        if item["value"] is not None and "Latency" in name:
            value += " ms"
        coverage = f"known {item['known']}/{item['eligible']}; unknown {item['unknown']}"
    return f"| {_cell(name)} | {value} | {coverage} |"


def _csv(path, rows, columns):
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        record = {key: "N/A" if row.get(key) is None else row.get(key, "") for key in columns}
        # Neutralize formulas in untrusted task text when opened in a spreadsheet.
        record = {key: "'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")) else value
                  for key, value in record.items()}
        writer.writerow(record)
    path.write_text(buf.getvalue(), encoding="utf-8-sig", newline="")


def _evidence_note(label, metadata, rows):
    if label == "offline":
        return "OFFLINE results validate the evaluator and fixed scripts; they are not real LLM performance."
    if label == "fake":
        return "FAKE results validate controlled model behavior; they are not real LLM performance."
    if label == "retrieval":
        return "RETRIEVAL results measure retrieval only; they are not Agent success rates."
    if not metadata.get("real_llm_results_produced"):
        return "Real-model evaluation has not produced completed evidence; formal success rates remain pending."
    if (metadata.get("stopped_reason") or not metadata.get("finished_at")
            or any(row.get("state") == "not_run" or row.get("reason") in {"not_run", "missing_result", "cancelled"}
                   for row in rows)):
        return "Real-model evaluation is incomplete or interrupted; observed partial metrics are not formal success rates."
    if metadata.get("repetitions") != 3:
        return "Real-model observations are provisional. Formal stability requires three runs per task."
    dataset = (metadata.get("comparison_configuration") or {}).get("dataset")
    expected_tasks = {"micro": 15, "real": 5}.get(dataset)
    if expected_tasks is None or len(metadata.get("selected_tasks") or []) != expected_tasks:
        return "Real-model observations do not cover a complete locked dataset; the formal benchmark remains pending."
    return "REAL Agent outcomes are observed under this saved configuration; all failures and missing runs are included."


def _comparison_lines(metadata):
    config = metadata.get("comparison_configuration") or {}
    if not config:
        return ["- Comparison configuration: unavailable; inspect `results.json` metadata."]
    fields = (("Dataset", "dataset"), ("Base commit", "base_commit"),
              ("Main model", "model"), ("Max total tokens", "max_total_tokens"),
              ("Max tokens per run", "max_tokens_per_run"),
              ("Task timeout", "task_timeout"), ("LLM timeout", "llm_timeout"),
              ("Agent steps", "agent_steps"), ("Repair rounds", "repair_rounds"),
              ("Generator", "generator"), ("Approval", "approval"), ("RAG", "rag"))
    lines = []
    for title, key in fields:
        if key in config:
            lines.append(f"- {title}: `{_cell(config[key])}`")
    for title, key in (("Tools", "tools"), ("Validation", "validation")):
        if key in config:
            lines.append(f"- {title}: `{_cell(json.dumps(config[key], ensure_ascii=False, sort_keys=True, separators=(',', ':')))}`")
    lines.append("- The complete locked configuration is in `results.json` under `metadata.comparison_configuration`.")
    return lines


def write_reports(directory, metadata, rows, redactor):
    """Persist raw rows before deriving CSV, report and Benchmark Card from them."""
    directory.mkdir(parents=True, exist_ok=True)
    rows = redactor.data(rows)
    metadata = redactor.data(metadata)
    label = _evidence_label(metadata)
    repetitions = metadata.get("repetitions")
    selected = metadata.get("selected_tasks")
    categories = metadata.get("task_categories")
    rows = _scheduled_rows(rows, repetitions, selected, categories)
    tasks = task_results(rows, expected_repetitions=repetitions,
                         selected_tasks=selected, categories=categories)
    summary = summarize(rows, expected_repetitions=repetitions,
                        selected_tasks=selected, categories=categories)
    document = {"schema_version": 2, "metadata": metadata, "evidence_label": label,
                "metrics": summary, "tasks": rows, "task_results": tasks}
    (directory / "results.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    run_columns = ["id", "category", "run_index", "success", "first_pass_success", "repair_success",
                   "reason", "state", "build_status", "ctest_status", "test_status", "test_count", "oracle_status",
                   "integrity_status", "scope_valid", "tool_calls", "total_tokens", "latency_ms", "agent_latency_ms",
                   "repair_rounds", "config_hash", "repository", "artifact_directory"]
    _csv(directory / "results.csv", rows, run_columns)
    _csv(directory / "task-results.csv", tasks, ["id", "category", "stable_success", "stability_evidence",
        "successful_runs", "completed_runs", "scheduled_runs", "run_success_rate",
        "first_pass_successful_runs", "repair_successful_runs"])
    rag = metadata.get("rag", "off")
    header = ["# ForgePilot Benchmark report", "",
              f"Evidence: **{label}**. Mode: **{_cell(metadata['mode'])}**. Model: {_cell(metadata['model'])}. RAG: **{_cell(rag)}**.", "",
              _evidence_note(label, metadata, rows), "",
              f"Run: {_cell(metadata['run_id'])}; started: {_cell(metadata['started_at'])}; finished: {_cell(metadata.get('finished_at') or 'in progress')}", "",
              f"Configuration hash: {_cell(metadata.get('config_hash') or 'N/A')}", "",
              "| Metric | Observed value | Denominator / coverage |", "| --- | --- | --- |"]
    metric_rows = [_metric_row(name, item) for name, item in summary.items() if name != "phase_counts"]
    notes = ["", "Task Success Rate uses at least 2 successes in exactly 3 scheduled runs. Any other repetition count is provisional. Run rates include every scheduled run, including failures, cancellations and not-run rows. Post-repair rate is among modification runs that attempted repair. First-pass means final success without repair.",
             "", "Build, CTest, Oracle and combined Test denominators include every selected modification run, including failures and not-run phases. Analysis is N/A and excluded. Combined Test requires public CTest and Protected Oracle; zero tests never pass. All refer to fresh final verification. Token, tool and latency averages use known values only and disclose missing coverage. P50/P95 use nearest rank over full per-run wall time, including fixture setup and grading; Agent latency excludes those stages and human review wait.",
             "", "| Task | Category | Stable success | Successful runs | Scheduled | Evidence |",
             "| --- | --- | --- | ---: | ---: | --- |"]
    task_lines = [f"| {_cell(task['id'])} | {_cell(task['category'])} | {_cell(task['stable_success'])} | {task['successful_runs']} | {task['scheduled_runs']} | {_cell(task['stability_evidence'])} |" for task in tasks]
    run_header = ["", "| Task | Run | Success | First pass | After repair | Build | CTest | Oracle | Integrity | Scope | Reason |",
                  "| --- | ---: | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    run_lines = [f"| {_cell(row['id'])} | {_cell(row.get('run_index', 'N/A'))} | {_cell(row['success'])} | {_cell(row.get('first_pass_success', 'N/A'))} | {_cell(row.get('repair_success', 'N/A'))} | {_cell(row['build_status'])} | {_cell(row.get('ctest_status', 'N/A'))} | {_cell(row.get('oracle_status', 'N/A'))} | {_cell(row.get('integrity_status', 'N/A'))} | {_cell(row.get('scope_valid', 'N/A'))} | {_cell(row['reason'])} |" for row in rows]
    footer = ["", "See results.json for raw run rows, task-results.csv for task aggregation, and each run artifact directory for environment, exact commands, approvals, baseline, grader output, trace and retained repositories.", ""]
    (directory / "report.md").write_text("\n".join(header + metric_rows + notes + task_lines + run_header + run_lines + footer), encoding="utf-8")

    card = ["# ForgePilot Benchmark Card", "",
            f"Evidence label: **{label}**", "", _evidence_note(label, metadata, rows), "",
            "## Locked evaluation", "",
            f"- Run ID: `{_cell(metadata['run_id'])}`",
            f"- Configuration hash: `{_cell(metadata.get('config_hash') or 'N/A')}`",
            f"- Dataset SHA-256: `{_cell(metadata.get('dataset_sha256') or 'N/A')}`",
            f"- Real task lock SHA-256: `{_cell(metadata.get('real_task_lock_sha256') or 'N/A')}`",
            f"- Main model: `{_cell(metadata['model'])}`",
            f"- RAG: `{_cell(rag)}`",
            f"- Selected tasks: {len(tasks)}",
            f"- Scheduled runs per task: {_cell(repetitions if repetitions is not None else 'unspecified')}",
            ""]
    card += _comparison_lines(metadata)
    card += ["", "## Observed metrics", "",
            "| Metric | Value | Coverage |", "| --- | --- | --- |"]
    card += metric_rows
    card += ["", "## Interpretation", "",
             "Stable task success is 2/3 only when three runs were scheduled. Cancellations, task errors and not-run rows count as failures. Missing token/latency values remain unknown. Offline, fake and retrieval evidence cannot be used as real-model Agent success rates.",
             "", "## Reproduction and evidence", "",
             "- `results.json`: saved metadata, every raw run and task aggregation.",
             "- `results.csv`: flat run-level rows; `task-results.csv`: task-level rows.",
             "- `report.md`: complete run table with every failure retained.",
             "- Per-run artifact directories contain traces, scope checks and protected verification summaries.", ""]
    (directory / "benchmark-card.md").write_text("\n".join(card), encoding="utf-8")
    return summary
