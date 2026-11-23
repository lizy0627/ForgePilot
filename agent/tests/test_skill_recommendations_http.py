"""Exercise reviewed-skill discovery through the production loopback HTTP server."""
import logging
import subprocess
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest

from forgepilot.api_v2 import MAX_MISSION_BODY
from forgepilot.git import SafeGit
from forgepilot.mission_host import MissionHost
from forgepilot.models.gateway import GatewayClient
from forgepilot.protocol_examples import examples
from forgepilot.rag.embedding import OpenAICompatibleEmbeddingProvider
from forgepilot.rag.vector_store import QdrantVectorStore
from forgepilot.repository import Repository
from forgepilot.skill_models import SkillDraftContent, SkillDraftData
from forgepilot.workspace_manager import WorkspaceManager
from test_api_network import serving


PATH = "/api/v2/skills/recommendations"
TOKEN = "test-local-token"
STALE = "列表仍显示旧值"
SAVE_TASK = "保存成功但列表仍显示旧值"


@pytest.fixture
def api():
    with serving(lambda trace: None, api_token=TOKEN) as (url, app):
        with httpx.Client(base_url=url, headers={"X-ForgePilot-Token": TOKEN}, trust_env=False) as client:
            yield app.state.task_host.store.skills, client


def reviewed(skills, data):
    draft = skills.create_draft(request_id=str(uuid4()), data=data)
    return skills.publish_local(draft.draft_id, expected_revision=draft.revision, request_id=str(uuid4()))


def skill(name, *, triggers=(STALE,), **rules):
    return SkillDraftData(name=name, content=SkillDraftContent(
        body="先查看对应模型行。", stages=("root_cause_analysis",), triggers=triggers,
        boundaries=("仅用于只读分析。",), **rules,
    ))


def recommend(client, description, **options):
    response = client.post(PATH, json={"description": description, **options})
    assert response.status_code == 200, response.text
    return response.json()["recommendations"]


def test_published_protocol_examples_match_live_responses(api, qt_draft):
    skills, client = api
    version = reviewed(skills, qt_draft)
    samples = examples()
    expected = samples["skill_recommendations_response"]
    expected["recommendations"][0]["skill_id"] = version.skill_id
    positive_request = samples["skill_recommendations_request"]
    empty = samples["skill_recommendations_empty_response"]
    for request, response in (
        (positive_request, expected),
        (samples["skill_recommendations_excluded_request"], empty),
        (samples["skill_recommendations_unmatched_request"], empty),
    ):
        result = client.post(PATH, json=request)
        assert result.status_code == 200 and result.json() == response
    skills.disable(version.skill_id, expected_revision=skills.get_skill(version.skill_id).revision)
    result = client.post(PATH, json=positive_request)
    assert result.status_code == 200 and result.json() == empty


def test_reviewed_qt_skill_explains_bilingual_matches_and_excludes_other_causes(api, qt_draft, caplog, monkeypatch):
    skills, client = api
    version = reviewed(skills, qt_draft)
    monkeypatch.setattr(logging.getLogger("forgepilot.api_v2"), "propagate", True)
    caplog.set_level(logging.INFO, logger="forgepilot.api_v2")

    # Missing project attributes still allow a symptom match.
    chinese, = recommend(client, SAVE_TASK)
    assert (chinese["skill_id"], chinese["version"], chinese["content_sha256"]) == (
        version.skill_id, version.version, version.content_sha256,
    )
    assert chinese["name"] == version.name and chinese["summary"] == version.content.summary
    assert any(STALE in reason for reason in chinese["reasons"])
    assert all("Qt" not in reason for reason in chinese["reasons"])

    english, = recommend(client, "After saving, the LIST STILL SHOWS THE OLD VALUE.",
                         task_type="bug_fix", project_tags=["qt"])
    assert english["skill_id"] == version.skill_id
    assert any("list still shows the old value" in reason.lower() for reason in english["reasons"])
    assert any("Qt" in reason for reason in english["reasons"])
    combo, = recommend(client, "Does DATAchanged send the correct displayRole?")
    assert combo["skill_id"] == version.skill_id
    assert any("datachanged" in reason.lower() and "displayrole" in reason.lower() for reason in combo["reasons"])

    assert recommend(client, f"{SAVE_TASK}，日志确认重复发出信号。", project_tags=["Qt"]) == []
    assert recommend(client, "A duplicate signal fires and the list still shows the old value.") == []
    assert recommend(client, "dataChanged is called", project_tags=["Qt"]) == []
    assert recommend(client, "Add a toolbar icon", project_tags=["Qt"]) == []
    assert recommend(client, "Rotate the log files") == []
    assert recommend(client, SAVE_TASK, task_type="feature") == []
    counts = [r.recommendation_count for r in caplog.records if hasattr(r, "recommendation_count")]
    assert counts == [1, 1, 1, 0, 0, 0, 0, 0, 0]


def test_score_priority_and_stable_cutoff_at_three(api):
    skills, client = api
    group = (("dataChanged", "DisplayRole"),)
    strongest = reviewed(skills, skill("带项目标签", keyword_groups=group, project_tags=("Qt",)))
    tied = [reviewed(skills, skill(name, keyword_groups=group)) for name in ("乙", "甲", "丙")]
    symptom = reviewed(skills, skill("只有症状和标签", project_tags=("Qt", "Desktop")))
    keywords = reviewed(skills, skill("仅关键词组合", triggers=(), keyword_groups=group,
                                      project_tags=("Qt",)))
    query = SAVE_TASK + "。检查 dataChanged 与 DisplayRole。"
    ranked = recommend(client, query, project_tags=["Qt", "Desktop"])
    assert [item["skill_id"] for item in ranked] == [strongest.skill_id, *sorted(v.skill_id for v in tied)[:2]]
    assert recommend(client, query, project_tags=["Desktop", "Qt"]) == ranked
    assert recommend(client, SAVE_TASK, project_tags=["Desktop", "Qt"])[0]["skill_id"] == symptom.skill_id
    assert {item["skill_id"] for item in recommend(client, "dataChanged: DisplayRole", project_tags=["Qt"])[:2]} == {
        strongest.skill_id, keywords.skill_id,
    }


def test_recommendations_follow_review_updates_and_disable(api):
    skills, client = api
    data = skill("批次 01 的触发条件")
    draft = skills.create_draft(request_id="pending-review", data=data)
    assert recommend(client, SAVE_TASK) == []
    first = skills.publish_local(draft.draft_id, expected_revision=draft.revision, request_id="approved")
    match, = recommend(client, SAVE_TASK)
    assert match["content_sha256"] == first.content_sha256 and STALE in match["summary"]
    revision = skills.draft_from_version(first.skill_id, first.version, request_id="revise")
    revision = skills.edit_draft(revision.draft_id, expected_revision=revision.revision,
                                 data=skill(data.name, triggers=("排序颠倒",)))
    assert recommend(client, "列表排序颠倒") == []
    second = skills.publish_local(revision.draft_id, expected_revision=revision.revision, request_id="approved-v2")
    assert recommend(client, SAVE_TASK) == []
    updated, = recommend(client, "列表排序颠倒")
    assert (updated["skill_id"], updated["version"], updated["content_sha256"]) == (
        first.skill_id, 2, second.content_sha256,
    )
    skills.disable(first.skill_id, expected_revision=skills.get_skill(first.skill_id).revision)
    assert recommend(client, "列表排序颠倒") == []


def test_matching_and_zero_results_never_open_project_or_start_external_services(api, qt_draft, monkeypatch):
    skills, client = api
    reviewed(skills, qt_draft)
    spies = []
    for owner, method in (
        (SafeGit, "run"), (subprocess, "Popen"), (Repository, "open"),
        (Repository, "_files"), (Repository, "audit_manifest"),
        (WorkspaceManager, "create"), (MissionHost, "preview"), (GatewayClient, "_request"),
        (OpenAICompatibleEmbeddingProvider, "_send_request"), (QdrantVectorStore, "__init__"),
        (QdrantVectorStore, "search"),
    ):
        name = f"{owner.__name__}.{method}"
        spy = Mock(name=name, side_effect=AssertionError(name))
        monkeypatch.setattr(owner, method, spy)
        spies.append(spy)

    assert len(recommend(client, SAVE_TASK)) == 1
    assert recommend(client, "统计源文件数量", project_tags=["Qt"]) == []
    for spy in spies:
        spy.assert_not_called()


def test_recommendation_http_auth_and_strict_request_guard(api):
    _, client = api
    body = {"description": SAVE_TASK}
    denied = client.post(PATH, json=body, headers={"X-ForgePilot-Token": "incorrect"})
    assert (denied.status_code, denied.json()["error"]["code"]) == (401, "unauthorized")
    for invalid in ({"repository_path": "D:/Repo"}, {"description": " "}, {"task_type": "other"},
                    {"project_tags": [1]}, {"project_tags": [" "]}):
        response = client.post(PATH, json={**body, **invalid})
        assert (response.status_code, response.json()["error"]["code"]) == (422, "invalid_request")
    json_headers = {"Content-Type": "application/json"}
    duplicate = client.post(PATH, content='{"description":"a","description":"b"}', headers=json_headers)
    assert (duplicate.status_code, duplicate.json()["error"]["code"]) == (422, "invalid_request")
    oversized = client.post(PATH, json={"description": "a" * MAX_MISSION_BODY})
    assert (oversized.status_code, oversized.json()["error"]["code"]) == (413, "request_too_large")
