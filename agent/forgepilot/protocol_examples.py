"""Deterministic public examples for both wire protocol versions.

The v1 source is frozen separately so adding mission examples cannot rewrite a
legacy message. Example paths, identifiers and hashes carry no authority.
"""
import json
from pathlib import Path

from .artifacts.canonical import content_sha256
from .orchestration.models import FailureKind, NodeResult, NodeStatus
from .skill_models import SkillContent, SkillDraftContent


TASK_ID = "11111111-1111-4111-8111-111111111111"
STAMP = "2026-10-27T08:00:00Z"


def _manual_skill_examples():
    """One manual review starting from a cancelled Mission with no artifacts."""
    draft_id = "55555555-5555-4555-8555-555555555555"
    skill_id = "66666666-6666-4666-8666-666666666666"
    note = "保存成功后列表仍是旧值。下次先检查模型行。"
    draft_data = {
        "name": "", "content": SkillDraftContent().model_dump(mode="json"), "developer_note": note,
        "evidence_revision": 12, "evidence_refs": [], "generation_status": "manual", "model_suggestion": "",
    }
    draft = {
        "draft_id": draft_id, "source_mission_id": TASK_ID, "revision": 1, "data": draft_data,
        "base_skill_id": None, "base_version": None, "result_skill_id": None, "result_version": None,
        "created_at": STAMP, "updated_at": STAMP,
    }
    name = "Qt 列表保存后仍显示旧值"
    content = SkillContent(
        body="先读取刚保存的模型行。值仍旧时，检查回写路径。值已更新时，检查 dataChanged 的范围和角色。",
        triggers=["列表仍显示旧值"], stages=["root_cause_analysis"],
        boundaries=["适用于 Qt Model/View。用当前任务的模型行与视图证据选择分支。"],
    ).model_dump(mode="json")
    inference = "可能漏发了模型通知，需检查对应行的值和通知范围。"
    saved = {**draft, "revision": 2,
             "data": {**draft_data, "name": name, "content": content, "pending_inference": inference}}
    published = {**saved, "revision": 3, "result_skill_id": skill_id, "result_version": 1}
    version = {
        "skill_id": skill_id, "version": 1, "content_sha256": content_sha256(content),
        "name": name, "content": content, "created_at": STAMP,
    }
    identity = {"skill_id": skill_id, "current_version": 1, "enabled": True, "revision": 1, "source": "local"}
    return {
        "skill_draft_create_request": {"developer_note": note, "request_id": "note-1"},
        "skill_draft_create_response": draft,
        "skill_draft_with_validation_response": {
            **draft, "data": {**draft_data, "evidence_refs": [{
                "kind": "artifact", "record_id": "88888888-8888-4888-8888-888888888888",
                "content_sha256": "a" * 64, "locator": "/status", "classification": "verified_result",
                "recorded_value": "failed",
            }]},
        },
        "skill_draft_save_request": {
            "expected_revision": 1, "name": name, "content": content, "pending_inference": inference,
        },
        "skill_draft_save_response": saved,
        "skill_draft_page_request": {
            "method": "GET", "path": f"/api/v2/skill-drafts?source_mission_id={TASK_ID}&limit=20&offset=0",
        },
        "skill_draft_page_response": {"drafts": [published], "next_offset": None},
        "skill_draft_detail_request": {"method": "GET", "path": f"/api/v2/skill-drafts/{draft_id}"},
        "skill_draft_detail_response": published,
        "skill_publish_local_request": {"expected_revision": 2, "request_id": "review-1"},
        "skill_publish_local_response": version,
        "skill_page_request": {"method": "GET", "path": "/api/v2/skills?limit=20&offset=0"},
        "skill_page_response": {"skills": [{**identity, "name": name}], "next_offset": None},
        "skill_version_detail_request": {"method": "GET", "path": f"/api/v2/skills/{skill_id}/versions/1"},
        "skill_version_detail_response": version,
        "skill_revision_draft_request": {"version": 1, "request_id": "revision-draft-1"},
        "skill_revision_draft_response": {
            **draft, "draft_id": "77777777-7777-4777-8777-777777777777", "source_mission_id": None,
            "base_skill_id": skill_id, "base_version": 1,
            "data": {**draft_data, "name": name, "content": content, "developer_note": "", "evidence_revision": None},
        },
        "skill_disable_request": {"expected_revision": 1, "enabled": False},
        "skill_disable_response": {**identity, "enabled": False, "revision": 2},
        "skill_save_conflict_error": {"error": {
            "code": "skill_revision_conflict", "message": "Revision changed; refresh before saving",
            "details": [], "task_id": None,
        }},
        "skill_evidence_mismatch_error": {"error": {
            "code": "invalid_skill_evidence", "message": "Evidence reference does not match a source Mission record",
            "details": [], "task_id": None,
        }},
    }


def _skill_generation_examples(manual):
    draft = manual["skill_draft_with_validation_response"]
    attempt_id = "99999999-9999-4999-8999-999999999999"
    path = f"/api/v2/skill-drafts/{draft['draft_id']}"
    attempt_path = f"{path}/attempts/{attempt_id}"
    attempt = {
        "attempt_id": attempt_id, "draft_id": draft["draft_id"], "start_revision": draft["revision"],
        "status": "generating", "template_version": "skill-draft.v1", "route": None,
        "elapsed_ms": None, "usage": None, "error_code": None, "suggestion": None,
    }
    accepted = {**draft, "latest_attempt": attempt, "data": {**draft["data"], "generation_status": "generating"}}
    note = "先看公开验证结果，再定位失败阶段。"
    suggestion = {
        "phenomenon": "改动后公开验证失败。",
        "priority_checks": "先查看失败阶段及其退出码。",
        "decision_branches": "构建失败时先查构建步骤，构建成功而测试失败时检查测试结果。",
        "stop_conditions": "确定第一个失败阶段后，转入对应验证。",
        "boundaries": "适用于有公开验证记录的任务。",
        "conclusion": "本次验证失败，根因仍待确认。",
        "basis": [
            {"key": "E-e8d95748107aceb9-1", "classification": "verified_result",
             "reference": draft["data"]["evidence_refs"][0]},
            {"key": "unknown", "classification": "pending_inference", "reference": None},
        ],
    }
    ready = {**attempt, "status": "ready", "route": "summary/local", "elapsed_ms": 420,
             "usage": {"prompt_tokens": 180, "completion_tokens": 64}, "suggestion": suggestion}
    return {
        "skill_generate_request": {"generation_mode": "model", "request_id": "write-lesson", "developer_note": note},
        "skill_generate_accepted_response": {
            **accepted, "data": {**accepted["data"], "developer_note": note}},
        "skill_generation_attempt_request": {"method": "GET", "path": attempt_path},
        "skill_generation_ready_response": ready,
        "skill_generation_insufficient_response": {
            **attempt, "status": "failed", "route": "summary/local", "elapsed_ms": 3,
            "error_code": "skill_draft_evidence_insufficient"},
        "skill_generation_interrupted_response": {**attempt, "status": "interrupted",
                                                 "error_code": "generation_interrupted"},
        "skill_regenerate_request": {"expected_revision": draft["revision"], "request_id": "try-again"},
        "skill_cancel_generation_request": {"method": "POST", "path": attempt_path + "/cancel", "body": {}},
        "skill_save_edited_suggestion_request": {
            "expected_revision": draft["revision"], "name": "定位公开验证失败阶段",
            "edited_lesson": {**{key: value for key, value in suggestion.items() if key != "basis"},
                              "attempt_id": attempt_id, "conclusion": "需核对第一个失败阶段。",
                              "basis_note": "状态记录支持验证失败，第二处引用仍待确认。"},
            "content": {"triggers": ["公开验证失败"], "stages": ["impact_test_analysis"],
                        "body": "查看第一个失败的退出码。构建失败就检查构建日志，测试失败就核对用例。定位阶段后停止排查。",
                        "boundaries": ["需要有可复核的公开验证结果。"]},
            "developer_note": "验证记录显示失败。", "pending_inference": "需要进一步核对失败阶段。",
        },
    }


def _mission_create_contract_cases(create, selected):
    minimal = {key: create[key] for key in ("repository_path", "request")}
    server_ids = [f"11111111-1111-4111-8111-{index:012d}" for index in range(1, 6)]

    def case(name, valid, **fields):
        return {"name": name, "request": minimal | fields, "valid": valid}

    return [
        case("omitted_optional_fields", True),
        {"name": "missing_repository", "request": {"request": minimal["request"]}, "valid": False},
        {"name": "missing_request", "request": {"repository_path": minimal["repository_path"]}, "valid": False},
        case("all_fields", True, **create, language="en-US", mcp_server_ids=server_ids[:4],
             use_rag=True, selected_skills=selected, baseline_paths=["src/main.cpp"],
             preview_token="a" * 64, baseline_confirmed=True, configure_preset="debug",
             build_preset="debug-build", test_preset="debug-test"),
        case("english_locale", True, language="en-US"),
        case("three_letter_language", True, language="eng"),
        case("invalid_language", False, language="en-us"),
        case("language_trailing_newline", False, language="en-US\n"),
        case("null_language", False, language=None),
        case("unknown_field", False, lang="en-US"),
        case("empty_selections", True, selected_skills=[], mcp_server_ids=[], baseline_paths=[]),
        case("one_skill", True, selected_skills=selected[:1]),
        case("skill_version_maximum", True, selected_skills=[selected[0] | {"version": 2**63 - 2}]),
        case("skill_version_over_limit", False, selected_skills=[selected[0] | {"version": 2**63 - 1}]),
        case("duplicate_skill_id", False, selected_skills=[selected[0], selected[0] | {"version": 2}]),
        case("too_many_skills", False, selected_skills=[*selected, selected[0] | {"skill_id": "third"}]),
        case("unknown_skill_field", False, selected_skills=[selected[0] | {"name": "extra"}]),
        case("duplicate_mcp_id", False, mcp_server_ids=[server_ids[0], server_ids[0]]),
        case("too_many_mcp_servers", False, mcp_server_ids=server_ids),
        case("null_presets_and_token", True, configure_preset=None, build_preset=None,
             test_preset=None, preview_token=None),
        case("nonboolean_rag", False, use_rag=1),
        case("invalid_toolchain_profile", False, toolchain_profile="gcc--debug"),
        case("blank_request", False, request=" \t\n"),
        case("nul_request", False, request="fix\x00issue"),
        case("posix_absolute_path", True, repository_path="/work/My Project"),
        case("repository_path_over_limit", False, repository_path="/" + "x" * 4096),
        case("windows_absolute_path", True, repository_path=r"C:\work\My Project"),
        case("relative_path", False, repository_path="work/project"),
        case("drive_relative_path", False, repository_path="C:project"),
        case("unc_path", False, repository_path=r"\\server\share\project"),
        case("double_slash_path", False, repository_path="//server/share"),
        case("dot_segment", False, repository_path="C:/work/./project"),
        case("parent_segment", False, repository_path=r"C:\work\..\project"),
        case("colon_in_path", False, repository_path="C:/work:alt"),
        case("control_in_path", False, repository_path="/work/\x7fproject"),
        case("duplicate_baseline_path", False, baseline_paths=["main.cpp", "main.cpp"]),
        case("nul_baseline_path", False, baseline_paths=["src/\x00.cpp"]),
        case("git_revision_expression", True, base_ref="refs/heads/main~1"),
        case("git_ref_option", False, base_ref="--help"),
        case("git_ref_whitespace", False, base_ref="feature\u00a0branch"),
        case("git_ref_control", False, base_ref="main\x7f"),
        case("git_ref_colon", False, base_ref="HEAD:file"),
        case("git_ref_backslash", False, base_ref=r"feature\branch"),
        case("git_ref_max_length", True, base_ref="x" * 255 + "🚀"),
        case("git_ref_over_limit", False, base_ref="x" * 257),
    ]


def examples():
    result = json.loads(Path(__file__).with_name("protocol_examples.v1.json").read_text(encoding="utf-8"))
    manual = _manual_skill_examples()
    result.update(manual)
    result.update(_skill_generation_examples(manual))
    result.update({
        "skill_recommendations_request": {
            "description": "保存成功但列表仍显示旧值", "task_type": "bug_fix", "project_tags": ["Qt"],
        },
        "skill_recommendations_response": {"recommendations": [{
            "skill_id": "33333333-3333-4333-8333-333333333333", "version": 1,
            "content_sha256": "d408c9d2375c0e929e54f54aba97c85222b538bdbff363cfd64fc9bb5241da4d",
            "name": "Qt 保存成功后列表仍显示旧值", "summary": "先比较保存结果和模型行，再核对视图通知。",
            "reasons": ["命中症状「列表仍显示旧值」", "命中项目标签「Qt」"],
        }]},
        "skill_recommendations_excluded_request": {
            "description": "列表仍显示旧值，实际问题是重复发出信号", "task_type": "bug_fix",
        },
        "skill_recommendations_unmatched_request": {"description": "给导出文件增加时间戳"},
        "skill_recommendations_empty_response": {"recommendations": []},
        "mission_create_request": {
            "repository_path": "D:/Projects/Example",
            "request": "修复窗口关闭后 Worker 未停止的问题",
            "task_type": "auto",
            "orchestration_mode": "multi_agent",
            "base_ref": "HEAD",
            "toolchain_profile": "auto",
        },
        "mission_approve_request": {
            "approval_type": "execution", "expected_revision": 8,
            "task_spec_hash": "a" * 64, "plan_hash": "b" * 64,
            "scope_hash": "c" * 64, "decision": "approved",
        },
        "mission_reject_request": {
            "approval_type": "execution", "expected_revision": 8,
            "task_spec_hash": "a" * 64, "plan_hash": "b" * 64,
            "scope_hash": "c" * 64, "decision": "rejected",
        },
        "mission_scope_approval_request": {
            "approval_type": "scope_expansion", "expected_revision": 42,
            "task_spec_hash": "e" * 64, "plan_hash": "f" * 64,
            "scope_hash": "a" * 64, "decision": "approved",
        },
        "mission_execution_pending_node": {
            "node_id": "execution_approval", "node_type": "human_gate", "status": "awaiting_human",
            "attempt": 1, "pending_action": "execution", "retry_actions": [],
            "result": NodeResult(NodeStatus.AWAITING_HUMAN, decision_summary="Approve the frozen execution plan",
                                 output_artifact_ids=("22222222-2222-4222-8222-222222222222",
                                                      "33333333-3333-4333-8333-333333333333")).to_dict(),
        },
        "mission_scope_pending_node": {
            "node_id": "repair_1", "node_type": "agent", "status": "awaiting_human",
            "attempt": 1, "pending_action": "scope_expansion", "retry_actions": [],
            "result": NodeResult(NodeStatus.AWAITING_HUMAN, decision_summary="Review the requested files").to_dict(),
        },
        "mission_retryable_node": {
            "node_id": "root_cause_analysis", "node_type": "agent", "status": "retryable",
            "attempt": 1, "pending_action": None, "retry_actions": ["retry"],
            "result": NodeResult(NodeStatus.RETRYABLE, retryable=True, failure_kind=FailureKind.MODEL_TRANSIENT,
                                 error_summary="Model temporarily unavailable").to_dict(),
        },
        "mission_interrupted_node": {
            "node_id": "implementer", "node_type": "agent", "status": "interrupted",
            "attempt": 1, "pending_action": None, "retry_actions": ["resume"],
            "result": NodeResult(NodeStatus.INTERRUPTED, failure_kind=FailureKind.APPLICATION_INTERRUPTED,
                                 error_summary="Execution interrupted").to_dict(),
        },
        "mission_scope_expansion_preview": {
            "task_id": TASK_ID, "revision": 42, "node_id": "repair_1",
            "attempt": 1, "status": "pending",
            "files": [{"path": "src/worker.h", "reason": "Declare the shutdown method",
                       "plan_step_id": "STEP-1", "acceptance_criteria_ids": ["AC-1"]}],
            "previous_max_changed_files": 1, "max_changed_files": 2,
            "task_spec_artifact_id": "44444444-4444-4444-8444-444444444444",
            "task_spec_hash": "e" * 64,
            "plan_artifact_id": "55555555-5555-4555-8555-555555555555",
            "plan_hash": "f" * 64, "scope_hash": "a" * 64,
            "checkpoint_id": "checkpoint-" + "1" * 64,
            "partial_change_set_artifact_id": None, "partial_patch": "",
            "replay_behavior": "Approval restores the writer entry checkpoint and reruns repair_1.",
        },
        "mission_cancel_request": {"expected_revision": 8},
        "mission_retry_request": {"expected_revision": 8, "node_id": "root_cause_analysis"},
        "mission_resume_request": {"expected_revision": 8, "recovery_token": "d" * 64},
        "mission_delivery_request": {"expected_revision": 8, "mode": "preview"},
        "mission_sse_reconnect_request": {
            "method": "GET", "path": f"/api/v2/missions/{TASK_ID}/events",
            "headers": {"Last-Event-ID": f"{TASK_ID}:8", "Accept": "text/event-stream"},
        },
        "mission_revision_conflict_error": {"error": {
            "code": "mission_revision_conflict",
            "message": "Mission revision changed; refresh the mission before retrying",
            "details": [], "task_id": TASK_ID,
        }},
        "mission_skill_selection_changed_error": {"error": {
            "code": "skill_selection_changed", "message": "Selected skill was disabled or superseded",
            "details": [], "task_id": None,
        }},
        "mission_approval_conflict_error": {"error": {
            "code": "mission_approval_conflict",
            "message": "TaskSpec, Plan or Scope changed; review the current approval package",
            "details": [], "task_id": TASK_ID,
        }},
    })
    candidate = result["skill_recommendations_response"]["recommendations"][0]
    selected = [
        {key: candidate[key] for key in ("skill_id", "version", "content_sha256")},
        {"skill_id": "44444444-4444-4444-8444-444444444444", "version": 2, "content_sha256": "e" * 64},
    ]
    result["mission_create_with_skills_request"] = {
        **result["mission_create_request"], "request": "保存成功但列表仍显示旧值", "selected_skills": selected,
    }
    result["mission_create_contract_cases"] = _mission_create_contract_cases(result["mission_create_request"], selected)
    result["mission_skills_response"] = {
        "task_id": TASK_ID, "selected_skills": selected,
        "injections": [{**selected[0], "node_id": "root_cause_analysis", "attempt": 1}],
    }
    result["mission_skills_empty_response"] = {"task_id": TASK_ID, "selected_skills": [], "injections": []}
    result["mission_missing_current_evidence_node"] = {
        "node_id": "root_cause_analysis", "node_type": "agent", "status": "failed", "attempt": 1,
        "pending_action": None, "retry_actions": [],
        "result": {"status": "failed", "output_artifact_ids": [], "decision_summary": "",
                   "next_route": None, "retryable": False, "failure_kind": "model_contract_violation",
                   "error_summary": "evidence_location",
                   "metrics": {"duration_ms": 650, "model_calls": 2, "tool_calls": 0, "total_tokens": 430}},
    }
    result["mission_query_response"] = {
        **result["mission_create_request"], "task_id": TASK_ID,
        "status": "awaiting_human", "current_revision": 8,
        "graph_id": "task-analysis.v1", "graph_version": 1,
        "created_at": STAMP, "updated_at": STAMP, "finished_at": None,
        "last_event_id": f"{TASK_ID}:8", "recovery_required": False,
        "review": {
            "task_id": TASK_ID, "revision": 8,
            "task_spec_artifact_id": "22222222-2222-4222-8222-222222222222",
            "task_spec_hash": "a" * 64,
            "plan_artifact_id": "33333333-3333-4333-8333-333333333333",
            "plan_hash": "b" * 64, "scope_hash": "c" * 64,
            "review_hash": "d" * 64,
        },
    }
    samples = (
        ("task.created", None, "mission-host", {"status": "pending"}),
        ("node.completed", "root_cause_analysis", "root-cause-analyst.v1",
         {"artifact_ids": ["22222222-2222-4222-8222-222222222222"], "duration_ms": 10322}),
        ("artifact.created", "root_cause_analysis", "root-cause-analyst.v1",
         {"artifact_id": "22222222-2222-4222-8222-222222222222", "artifact_type": "EvidenceBundle"}),
        ("approval.required", "execution_approval", "orchestrator.v1", {"approval_type": "execution"}),
        ("tool.completed", "root_cause_analysis", "root-cause-analyst.v1", {"name": "read_file", "duration_ms": 12}),
        ("model.completed", "root_cause_analysis", "root-cause-analyst.v1",
         {"model": "configured-profile", "prompt_version": "1", "input_tokens": 1200, "output_tokens": 300}),
        ("validation.completed", "validation", "validator.v1", {"status": "passed", "commit": "e" * 40}),
        ("workspace.created", None, "workspace-manager", {"workspace_id": "workspace-example"}),
        ("security.recorded", None, "policy-engine", {"code": "permission_denied", "severity": "warning"}),
    )
    for sequence, (event_type, node_id, actor, data) in enumerate(samples, start=1):
        result[f"mission_event_{event_type.split('.')[0]}"] = {
            "event_id": f"{TASK_ID}:{sequence}", "task_id": TASK_ID,
            "task_revision": sequence, "timestamp": STAMP, "event_type": event_type,
            "node_id": node_id, "actor": actor, "data": data,
        }
    result["mission_event_skill_injection"] = {
        "event_id": f"{TASK_ID}:10", "task_id": TASK_ID, "task_revision": 10, "timestamp": STAMP,
        "event_type": "node.skills_injected", "node_id": "root_cause_analysis", "actor": "orchestrator",
        "data": {"attempt": 1, "skills": [selected[0]]},
    }
    return result
