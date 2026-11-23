"""Deterministic public examples for both wire protocol versions.

The v1 source is frozen separately so adding mission examples cannot rewrite a
legacy message. Example paths, identifiers and hashes carry no authority.
"""
import json
from pathlib import Path

from .artifacts.canonical import content_sha256
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


def examples():
    result = json.loads(Path(__file__).with_name("protocol_examples.v1.json").read_text(encoding="utf-8"))
    result.update(_manual_skill_examples())
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
    result["mission_skills_response"] = {
        "task_id": TASK_ID, "selected_skills": selected,
        "injections": [{**selected[0], "node_id": "root_cause_analysis", "attempt": 1}],
    }
    result["mission_skills_empty_response"] = {"task_id": TASK_ID, "selected_skills": [], "injections": []}
    result["mission_missing_current_evidence_node"] = {
        "node_id": "root_cause_analysis", "node_type": "agent", "status": "failed", "attempt": 1,
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
