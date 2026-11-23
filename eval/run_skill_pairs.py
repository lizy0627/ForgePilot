"""Run an offline skill-selection comparison or audit an existing results file."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from forgepilot.artifacts.canonical import content_sha256
from forgepilot.skill_selection import FrozenSkill, skill_reference_payload
from forgepilot_eval.manifest import DATA_ROOT
from forgepilot_eval.reviewed_qt_skill import QT_SKILL_SHA256, load_qt_skill
from forgepilot_eval.skill_pair_metrics import write_report
from forgepilot_eval.skill_pairs import ANALYST_STAGES, locked_spec, run_campaign
from skill_cases import load_cases


def audit_corpus(document, cases):
    if document["tasks"] != [{"id": c.task.id, "split": c.split} for c in cases]:
        raise ValueError("Saved schedule differs from the registered Qt corpus")
    if (document["skill"]["version"], document["skill"]["content_sha256"]) != (1, QT_SKILL_SHA256):
        raise ValueError("Unexpected reviewed skill identity")
    skill = FrozenSkill(**document["skill"], content=load_qt_skill().reviewed_content())
    locks = {}
    for case in cases:
        hashes = case.locks
        locks[case.task.id] = {
            "task_id": case.task.id, "skill_content_sha256": QT_SKILL_SHA256,
            "task_spec_sha256": content_sha256(locked_spec(case)),
            **{key + "_sha256": hashes[key] for key in ("fixture", "solution", "task", "oracle")},
        }
    for row in document["rows"]:
        expected = locks[row["id"]]
        if any(row["conditions"].get(key) != value for key, value in expected.items()):
            raise ValueError(f"Saved inputs differ from registered locks for {row['id']}")
        if row.get("analysis_status") == "awaiting_human" or row.get("verification") or row["success"]:
            expected_fields = {}
            receipts = []
            for stage in ANALYST_STAGES:
                receiving = row["arm"] == "on" and stage in skill.content.stages
                expected_fields[stage] = skill_reference_payload((skill,), stage) if receiving else {}
                if receiving:
                    receipts.append(dict(skill.identity(), node_id=stage, attempt=1))
            if (row.get("selection_payload_sha256") != content_sha256(expected_fields)
                    or row.get("stage_receipts") != receipts):
                raise ValueError("Saved skill dispatch differs from frozen guidance or stage receipts")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    destination = parser.add_mutually_exclusive_group()
    destination.add_argument("--output", type=Path, help="New directory for a fresh campaign")
    destination.add_argument("--audit", type=Path, metavar="RESULTS", help="Audit saved results without running Qt")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--jobs", type=int, default=1)
    args = parser.parse_args(argv)
    cases = load_cases()
    if args.audit:
        document = json.loads(args.audit.read_text(encoding="utf-8"))
        output = args.audit.parent
    else:
        from forgepilot_eval.skill_pair_native import NativeValidator
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        output = args.output or DATA_ROOT.parent / "logs" / "skill-pairs" / stamp
        document = asyncio.run(run_campaign(cases, NativeValidator(), output, args.repetitions, args.jobs))
    audit_corpus(document, cases)
    write_report(document, output)
    print((output / "report.md").resolve())
    return 0 if args.audit or all(row["success"] is True for row in document["rows"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
