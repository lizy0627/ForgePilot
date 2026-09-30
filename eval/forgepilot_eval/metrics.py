"""Explicit denominators; unknown usage and not-applicable phases never become zero/pass."""
import csv
import io
import json


def summarize(rows):
    modifications = [r for r in rows if r["category"] != "调用链分析"]
    def rate(items, predicate):
        n = len(items); passed = sum(bool(predicate(r)) for r in items)
        return {"passed": passed, "denominator": n, "value": passed / n if n else None}
    def average(items, key):
        known = [r[key] for r in items if r.get(key) is not None]
        return {"value": sum(known) / len(known) if known else None,
                "known": len(known), "unknown": len(items) - len(known), "eligible": len(items)}
    return {
        "Task Success Rate": rate(rows, lambda r: r["success"]),
        "Build Pass Rate": rate(modifications, lambda r: r["build_status"] == "passed"),
        "Test Pass Rate": rate(modifications, lambda r: r["test_status"] == "passed" and r["test_count"] > 0),
        "Average Tool Calls": average(rows, "tool_calls"),
        "Average Token Usage": average(rows, "total_tokens"),
        "Average Latency": average(rows, "latency_ms"),
        "Average Repair Rounds": average(modifications, "repair_rounds"),
        "phase_counts": {phase: {state: sum(r[phase] == state for r in rows)
            for state in ("passed", "failed", "not_run", "not_applicable")} for phase in ("build_status", "test_status")},
    }


def write_reports(directory, metadata, rows, redactor):
    directory.mkdir(parents=True, exist_ok=True)
    rows = redactor.data(rows)
    metadata = redactor.data(metadata)
    summary = summarize(rows)
    document = {"metadata": metadata, "metrics": summary, "tasks": rows}
    (directory / "results.json").write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    columns = ["id", "category", "success", "reason", "state", "build_status", "test_status", "test_count",
               "tool_calls", "total_tokens", "latency_ms", "repair_rounds", "repository", "artifact_directory"]
    buf = io.StringIO(newline=""); writer = csv.DictWriter(buf, columns, extrasaction="ignore"); writer.writeheader()
    for row in rows:
        # Keep null as N/A; neutralize spreadsheet formula prefixes in untrusted text.
        record = {k: "N/A" if row.get(k) is None else row.get(k, "") for k in columns}
        record = {k: "'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v for k, v in record.items()}
        writer.writerow(record)
    (directory / "results.csv").write_text(buf.getvalue(), encoding="utf-8-sig", newline="")
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ").replace("<", "&lt;").replace(">", "&gt;")
    lines = ["# ForgePilot Benchmark report", "", f"Mode: **{metadata['mode']}**. Model: {cell(metadata['model'])}.", "",
        "OFFLINE results validate the evaluator and fixed scripts; they are not real LLM performance." if metadata["mode"] == "offline" else
        "One real-model run only; these observations do not establish stable success rates.", "",
        f"Run: {metadata['run_id']}; started: {metadata['started_at']}; finished: {metadata.get('finished_at', 'in progress')}", "",
        "| Metric | Observed value | Denominator / coverage |", "| --- | --- | --- |"]
    for name, item in summary.items():
        if name == "phase_counts": continue
        if "denominator" in item:
            value = "N/A" if item["value"] is None else f"{item['value']:.2%}"
            coverage = f"{item['passed']}/{item['denominator']}"
        else:
            value = "N/A" if item["value"] is None else f"{item['value']:.2f}" + (" ms" if name == "Average Latency" else "")
            coverage = f"known {item['known']}/{item['eligible']}; unknown {item['unknown']}"
        lines.append(f"| {name} | {value} | {coverage} |")
    lines += ["", "Build/Test denominators include every selected modification task, including failures and not-run phases. Analysis is N/A and excluded. Zero tests never pass. Build/Test refer to the fresh protected final verification, not Agent self-assessment. Token averages use only complete known total usage and disclose missing coverage. Tool/latency averages use recorded Agent tasks; repair averages use modification tasks only. Latency excludes fixture setup, evaluator grading and human review wait.", "",
        "| Task | Category | Success | Build | Test | Reason |", "| --- | --- | --- | --- | --- | --- |"]
    lines += [f"| {r['id']} | {r['category']} | {r['success']} | {r['build_status']} | {r['test_status']} | {cell(r['reason'])} |" for r in rows]
    lines += ["", "See results.json and each task artifact directory for environment, exact commands, approvals, baseline, grader output, trace and retained repositories.", ""]
    (directory / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return summary
