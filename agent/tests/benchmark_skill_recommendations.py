"""Explicit loopback benchmark. Run via scripts/Test-Agent.cmd -q -s tests/benchmark_skill_recommendations.py."""
from contextlib import closing
from datetime import datetime, timezone
import json
from math import ceil
from pathlib import Path
import platform
from time import perf_counter
from uuid import uuid4

import httpx

from forgepilot.skill_models import SkillDraftContent, SkillDraftData
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot_eval.reviewed_qt_skill import publish_qt_skill
from skill_cases import load_cases
from test_api_network import serving
from test_skill_recommendations_http import PATH, guard_recommendation_boundaries


OUTPUT_ROOT = Path(__file__).resolve().parents[2] / "logs" / "skill-evaluation"


def identity(version):
    return {key: version[key] for key in ("skill_id", "version", "content_sha256")}


def query(client, description):
    body = {"description": description, "task_type": "bug_fix", "project_tags": ["Qt"]}
    started = perf_counter()
    try:
        response = client.post(PATH, json=body)
        response.raise_for_status()
        ranked = [identity(item) for item in response.json()["recommendations"]]
        if len(ranked) > 3:
            raise ValueError("Recommendation endpoint exceeded Top-3")
        error = None
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        ranked, error = None, str(exc)
    return {"request": body, "ranked": ranked, "error": error, "elapsed_ms": (perf_counter() - started) * 1000}


def quality_metrics(rows):
    positives = sum(bool(row["expected"]) for row in rows)
    hits = wrong_items = wrong_queries = unknown = 0
    for row in rows:
        if row["ranked"] is None:
            unknown += 1
            continue
        hits += any(item in row["expected"] for item in row["ranked"][:3])
        wrong = sum(item not in row["expected"] for item in row["ranked"])
        wrong_items += wrong
        wrong_queries += wrong > 0
    return {"queries": len(rows), "unknown": unknown, "positive_queries": positives, "top3_hits": hits,
            "top3_hit_rate": hits / positives if positives else None,
            "wrong_recommendations": wrong_items, "queries_with_wrong_recommendations": wrong_queries}


def test_quality_metrics_count_all_recommendations_and_keep_unknowns():
    rows = [
        {"expected": ["A"], "ranked": ["B", "C", "A"]},
        {"expected": ["A"], "ranked": []},
        {"expected": ["A"], "ranked": None},
        {"expected": [], "ranked": ["B"]},
        {"expected": [], "ranked": []},
    ]
    assert quality_metrics(rows) == {
        "queries": 5, "unknown": 1, "positive_queries": 3, "top3_hits": 1, "top3_hit_rate": 1 / 3,
        "wrong_recommendations": 3, "queries_with_wrong_recommendations": 2,
    }
    assert quality_metrics(rows[-1:])["top3_hit_rate"] is None


def test_hot_recommendations_over_loopback(tmp_path, monkeypatch):
    with closing(SqliteEventStore(tmp_path / "skills.sqlite3")) as store:
        target = identity(publish_qt_skill(store.skills).model_dump())
        panels = []
        for index in range(499):
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
            panels.append(identity(version.model_dump()))

        labels = [(case.task.id, case.split, case.task.request, case.relevant) for case in load_cases()]
        labels.append(("unrelated-export", "unrelated", "给导出文件增加时间戳", False))
        quality, timing = [], []
        with serving(None, store=store) as (url, _), httpx.Client(base_url=url, trust_env=False) as client:
            with monkeypatch.context() as patch:
                spies = guard_recommendation_boundaries(patch)
                for case_id, split, description, relevant in labels:
                    quality.append({"id": case_id, "split": split, "expected": [target] if relevant else [],
                                    **query(client, description)})
                for sequence in range(55):
                    # Warm and time distinct queries on the same loopback connection.
                    index = sequence * 9
                    timing.append({"warmup": sequence < 5, "expected": [panels[index]],
                                   **query(client, f"panel{index:03} 保存后仍显示旧值")})
                calls = {name: spy.call_count for name, spy in spies.items()}

    timed = [row for row in timing if not row["warmup"]]
    durations = sorted(row["elapsed_ms"] for row in timed)
    p95 = durations[ceil(0.95 * len(durations)) - 1]
    valid_latency = all(row["ranked"] == row["expected"] for row in timing)
    report = {
        "evidence": "offline_recommendation_http", "measured_at_utc": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(), "processor": platform.processor(), "python": platform.python_version(),
        "catalog": {"reviewed_qt": target, "synthetic_panels": len(panels), "total": len(panels) + 1},
        "quality_rows": quality, "quality": quality_metrics(quality),
        "quality_by_split": {split: quality_metrics([row for row in quality if row["split"] == split])
                             for split in sorted({row["split"] for row in quality})},
        "timing_rows": timing, "warmups": len(timing) - len(timed), "timed_queries": len(timed),
        "nearest_rank_p95_ms": p95, "target_100ms_met": valid_latency and p95 <= 100,
        "all_timing_queries_returned_expected_result": valid_latency,
        "observed_boundary_calls": calls,
    }
    directory = OUTPUT_ROOT / f"recommendations-{uuid4().hex}"
    directory.mkdir(parents=True, exist_ok=False)
    path = directory / "recommendations.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Loopback HTTP: 500 skills, 5 warmups, 50 timed, P95 {p95:.2f} ms. Report {path}")
    assert all(row["ranked"] is not None for row in quality)
    assert valid_latency
    assert not any(calls.values())
