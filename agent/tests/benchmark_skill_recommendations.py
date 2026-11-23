"""Run with scripts/Test-Agent.cmd -q -s tests/benchmark_skill_recommendations.py."""
from contextlib import closing
from math import ceil
import platform
from time import perf_counter

import httpx

from forgepilot.skill_models import SkillDraftContent, SkillDraftData
from forgepilot.sqlite_store import SqliteEventStore
from test_api_network import serving


def test_hot_recommendations_over_loopback(tmp_path):
    with closing(SqliteEventStore(tmp_path / "skills.sqlite3")) as store:
        expected_ids = []
        for index in range(500):
            panel = f"panel{index:03}"
            content = SkillDraftContent(
                triggers=(f"{panel} 保存后仍显示旧值", f"{panel} shows stale data"),
                keyword_groups=((panel, "dataChanged"),),
                exclusions=("duplicate signal",), project_tags=("Qt",), task_types=("bug_fix",),
                stages=("root_cause_analysis",), boundaries=("Verify against the current model.",),
                body=f"Inspect the model row and changed roles bound to {panel}.",
            )
            draft = store.skills.create_draft(
                request_id=f"draft-{panel}", data=SkillDraftData(name=panel, content=content),
            )
            version = store.skills.publish_local(
                draft.draft_id, expected_revision=draft.revision, request_id=f"publish-{panel}",
            )
            expected_ids.append(version.skill_id)

        samples = []
        with serving(None, store=store) as (url, _), httpx.Client(base_url=url, trust_env=False) as client:
            for sequence in range(55):
                # A different known match each time avoids timing identical-response caches.
                index = sequence * 9
                body = {"description": f"panel{index:03} 保存后仍显示旧值", "task_type": "bug_fix",
                        "project_tags": ["Qt"]}
                started = perf_counter()
                response = client.post("/api/v2/skills/recommendations", json=body)
                response.raise_for_status()
                recommendations = response.json()["recommendations"]
                elapsed_ms = (perf_counter() - started) * 1000
                assert [item["skill_id"] for item in recommendations] == [expected_ids[index]]
                if sequence >= 5:
                    samples.append(elapsed_ms)

    p95 = sorted(samples)[ceil(0.95 * len(samples)) - 1]
    print(f"Loopback HTTP with 500 published skills, 5 warmups, {len(samples)} timed requests. "
          f"{platform.platform()}, {platform.processor()}, Python {platform.python_version()}. "
          f"nearest-rank P95 {p95:.2f} ms, 100 ms target {'met' if p95 <= 100 else 'missed'}")
