"""Audit the scheduled offline pairs before computing any comparison."""
import json
import math
from pathlib import Path
import re
from statistics import median

from forgepilot.artifacts.canonical import content_sha256
from forgepilot.skill_models import SelectedSkill


ARMS = ("off", "on")
MEASURES = ("repair_rounds", "tool_calls", "investigation_tool_calls", "input_tokens", "latency_ms")


def _validate_outcome(row):
    for key in ("first_pass_success", "success"):
        if row[key] is not None and type(row[key]) is not bool:
            raise ValueError(f"Invalid outcome: {key}")
    for key in MEASURES:
        value = row[key]
        types = (int, float) if key == "latency_ms" else (int,)
        if value is not None and (type(value) not in types or not math.isfinite(value) or value < 0):
            raise ValueError(f"Invalid measurement: {key}")
    if row["first_pass_success"] is True and (row["success"] is not True or row["repair_rounds"] != 0):
        raise ValueError("First-pass result contradicts final outcome or repair count")
    if row["success"] is True and (
        row["repair_rounds"] is None or row["first_pass_success"] != (row["repair_rounds"] == 0)
    ):
        raise ValueError("Successful result contradicts first-pass or repair evidence")
    if row["success"] is not True and not row["reason"]:
        raise ValueError("Unsuccessful or unobserved row needs a reason")
    total, investigation = row["tool_calls"], row["investigation_tool_calls"]
    if None not in (total, investigation) and investigation > total:
        raise ValueError("Investigation calls exceed total calls")


def _validate_lock(row, skill):
    conditions = row["conditions"]
    digests = ("task_spec_sha256", "task_sha256", "fixture_sha256", "oracle_sha256",
               "solution_sha256", "runtime_sha256", "evaluator_sha256")
    if (content_sha256(conditions) != row["conditions_sha256"]
            or conditions.get("skill_content_sha256") != skill["content_sha256"]
            or conditions.get("task_id") != row["id"]
            or any(not re.fullmatch("[a-f0-9]{64}", conditions.get(key, "")) for key in digests)
            or any(not conditions.get(key) for key in ("budget", "model", "topology", "validator"))):
        raise ValueError("Invalid condition lock")


def _validate_pass(row, skill):
    if row["success"] is not True:
        return
    receipts = [dict(skill, node_id="root_cause_analysis", attempt=1)] if row["arm"] == "on" else []
    if row.get("stage_receipts") != receipts:
        raise ValueError("Successful row lacks matching injection receipts")
    inputs = row.get("analyst_input_sha256", {})
    if (set(inputs) != {"root_cause_analysis", "impact_test_analysis"}
            or any(not re.fullmatch("[a-f0-9]{64}", value) for value in inputs.values())):
        raise ValueError("Successful row lacks analyst input hashes")
    verification = row.get("verification", {})
    if not all(verification.get(key) is True for key in ("passed", "protected_unchanged", "candidate_unchanged")):
        raise ValueError("Successful row lacks native validation")
    if (any(verification.get(key) != "passed" for key in
            ("configure_status", "build_status", "public_test_status", "oracle_status"))
            or any(verification.get(key) != 1 for key in ("public_test_count", "oracle_test_count"))
            or row.get("candidate_unchanged_by_grading") is not True
            or row.get("scope", {}).get("valid") is not True):
        raise ValueError("Successful row contradicts validation evidence")


def _audit(document):
    if document["evidence_label"] != "offline" or document.get("paid_model_calls") != 0:
        raise ValueError("This report accepts only offline skill pairs")
    skill = SelectedSkill.model_validate(document["skill"]).model_dump()
    repetitions, tasks = document["repetitions"], document["tasks"]
    if type(repetitions) is not int or repetitions < 1 or not tasks:
        raise ValueError("Empty or invalid schedule")
    splits = {task["id"]: task["split"] for task in tasks}
    if len(splits) != len(tasks) or set(splits.values()) - {"development", "holdout", "counterexample"}:
        raise ValueError("Duplicate task or invalid split")
    pending = {(task_id, index, arm) for task_id in splits for index in range(1, repetitions + 1) for arm in ARMS}
    locks = {}
    observed_inputs = {}
    for row in document["rows"]:
        task_id, index, arm = row["id"], row["run_index"], row["arm"]
        slot = (task_id, index, arm)
        if type(index) is not int or slot not in pending:
            raise ValueError(f"Duplicate or unscheduled arm: {slot}")
        pending.remove(slot)
        _validate_lock(row, skill)
        conditions = row["conditions"]
        if conditions != locks.setdefault(task_id, conditions):
            raise ValueError(f"Conditions changed between arms or repetitions: {slot}")
        if row["selected_skills"] != ([skill] if arm == "on" else []):
            raise ValueError(f"Skill selection differs from assigned arm: {slot}")
        _validate_outcome(row)
        _validate_pass(row, skill)
        for stage, digest in row.get("analyst_input_sha256", {}).items():
            if digest != observed_inputs.setdefault((task_id, stage), digest):
                raise ValueError(f"Observed analyst inputs changed for {stage}: {slot}")
    if pending:
        raise ValueError(f"Missing scheduled arm: {min(pending)}")
    return splits


def _rate(rows, key):
    passed = sum(row[key] is True for row in rows)
    return {"passed": passed, "unknown": sum(row[key] is None for row in rows),
            "denominator": len(rows), "rate": passed / len(rows)}


def _group(rows):
    result = {"first_pass": _rate(rows, "first_pass_success"), "final_pass": _rate(rows, "success")}
    for key in MEASURES:
        values = [row[key] for row in rows if row[key] is not None]
        result[key] = {
            "known": len(values), "unknown": len(rows) - len(values), "denominator": len(rows),
            "median": median(values) if values else None, "mean": sum(values) / len(values) if values else None,
        }
    return result


def summarize(document):
    splits = _audit(document)
    rows = document["rows"]

    def arms(group):
        return {arm: _group([r for r in group if r["arm"] == arm]) for arm in ARMS}

    return {
        "evidence_label": "offline", "pairs": len(rows) // 2, "arms": arms(rows),
        "by_task": {task_id: arms([r for r in rows if r["id"] == task_id]) for task_id in splits},
        "by_split": {split: arms([r for r in rows if splits[r["id"]] == split]) for split in sorted(set(splits.values()))},
        "failures_or_unknowns": [{k: r[k] for k in ("id", "run_index", "arm", "success", "reason")}
                                 for r in rows if r["success"] is not True],
        "authorized_mission_trial": "not_run", "formal_real_model_benchmark": "not_run",
        "promotion": "pending_real_evidence",
    }


def write_report(document, output_dir):
    summary = summarize(document)
    output_dir = Path(output_dir)
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False,
                                                         allow_nan=False) + "\n", encoding="utf-8")
    lines = ["# Qt 技能配对评测", "", f"离线工程链路共 {summary['pairs']} 对。每题的两臂和各次重复已通过条件锁校验。",
             "通过率按计划次数计算。下表的计数依次为通过数 / 分母（未知数）。", "",
             "| 样本 | 选择 | 首次通过 | 最终通过 |", "| --- | --- | ---: | ---: |"]
    for name, arms in [("全部", summary["arms"]), *summary["by_task"].items()]:
        for arm, group in arms.items():
            rates = [group[key] for key in ("first_pass", "final_pass")]
            cells = [f"{r['passed']}/{r['denominator']}（{r['unknown']}）" for r in rates]
            lines.append(f"| {name} | {arm} | {' | '.join(cells)} |")
    lines.extend(["", "| 全部样本 | 选择 | 中位数 | 已知 / 分母 | 未知 |", "| --- | --- | ---: | ---: | ---: |"])
    names = ("修复轮数", "工具调用", "调查工具调用", "输入 token", "端到端耗时（ms）")
    for name, key in zip(names, MEASURES, strict=True):
        for arm, group in summary["arms"].items():
            m = group[key]
            value = "未知" if m["median"] is None else f"{m['median']:.2f}"
            lines.append(f"| {name} | {arm} | {value} | {m['known']}/{m['denominator']} | {m['unknown']} |")
    failures = summary["failures_or_unknowns"]
    lines.extend(["", f"最终失败或未知共 {len(failures)} 次。逐次记录在同目录 results.json。", ""])
    for failure in failures:
        lines.append(f"- {failure['id']} #{failure['run_index']} {failure['arm']} — {failure['reason']}")
    lines.extend(["", "## 晋升决定", "", "暂缓内置。主动授权的真实 Mission 试用和正式真实模型基准均待验证。",
                  "需在真实留出正例观察到至少一项首次通过改善，或在最终成功率持平时调查工具调用中位数下降至少 20%。",
                  "还需核实反例未稳定诱导错误修复，且权限、证据和恢复无回归。"])
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary
