import hashlib
import json
from pathlib import Path
import sys
from dataclasses import asdict

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from acceptance_reconciliation import load_reconciliation
from reconciliation_support import write_reconciliation_fixture


@pytest.fixture
def proof(tmp_path):
    row = {"id": "5" * 32, "label": "bug-negative-clamp-r1", "reserved": 20,
           "charged": 100, "estimated": 1, "status": "unknown_usage_campaign_hold", "created_at": "2025-01-01 00:00:00"}
    manifest, evidence = write_reconciliation_fixture(tmp_path, row, actual=10)
    return manifest, evidence, tmp_path


def edit(path, action):
    data = json.loads(path.read_bytes())
    action(data)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_provider_record_binds_one_failed_request_without_storing_prompt(proof):
    record = load_reconciliation(*proof)
    assert record["provider_request_id"] == "provider-fixture-record-123"
    assert record["expected"]["charged"] == 100
    assert record["usage"] == {"input_tokens": 3, "reasoning_tokens": 3, "answer_tokens": 4, "total_tokens": 10}
    assert "fixture only" not in json.dumps(record)


@pytest.mark.parametrize("action", [
    lambda detail: detail.update(kind="daily_invoice"),
    lambda detail: detail.pop("provider_request_id"),
    lambda detail: detail.update(started_at="2025-01-01T00:00:25Z"),
    lambda detail: detail["request"].update(model="another-model"),
    lambda detail: detail["request"]["messages"][0].update(content="different request"),
    lambda detail: detail["request"].update(tools=[{"type": "function", "function": {
        "description": "", "parameters": {}, "name": "unexpected"}}]),
    lambda detail: detail["usage"].pop("reasoning_tokens"),
    lambda detail: detail["usage"].update(answer_tokens=-5),
    lambda detail: detail["usage"].update(reasoning_tokens=True),
    lambda detail: detail["usage"].update(input_tokens=1.0),
    lambda detail: detail["usage"].update(total_tokens=5),
    lambda detail: detail["usage"].update(total_tokens=2**64, answer_tokens=2**64-6),
])
def test_rejects_unmatched_or_incomplete_provider_detail(proof, action):
    manifest, evidence, _ = proof
    edit(evidence, action)
    edit(manifest, lambda m: m.update(evidence_sha256=hashlib.sha256(evidence.read_bytes()).hexdigest()))
    with pytest.raises(ValueError):
        load_reconciliation(*proof)


@pytest.mark.parametrize("action", [
    lambda m: m.update(evidence_sha256="b" * 64),
    lambda m: m.update(campaign="other"),
    lambda m: m.update(operated_at="2026-01-01"),
    lambda m: m.update(operated_at="2020-01-01T00:00:00Z"),
    lambda m: m.update(runtime_identity={}),
    lambda m: m["expected"].update(estimated=True),
    lambda m: m["expected"].update(status="request_error"),
    lambda m: m["expected"].update(label="../outside"),
])
def test_rejects_invalid_operator_manifest(proof, action):
    edit(proof[0], action)
    with pytest.raises(ValueError):
        load_reconciliation(*proof)


def test_multiple_or_changed_local_attempts_do_not_match(proof):
    _, _, root = proof
    result = root / "bug-negative-clamp-r1/result.json"
    edit(result, lambda run: run.update(network_requests=2))
    with pytest.raises(ValueError):
        load_reconciliation(*proof)
    edit(root / "summary.json", lambda s: s["runs"][0].update(network_requests=2))
    with pytest.raises(ValueError):
        load_reconciliation(*proof)


def test_same_second_second_request_is_ambiguous(proof):
    _, _, root = proof
    def duplicate(summary):
        rows = summary["budget"]["requests"]
        rows.append({**rows[0], "id": "a" * 32, "label": "concurrent"})
    edit(root / "summary.json", duplicate)
    with pytest.raises(ValueError):
        load_reconciliation(*proof)


@pytest.mark.asyncio
async def test_sdk_wire_tool_shape_binds_to_gateway_network_accounting(proof):
    import httpx
    from forgepilot.errors import ForgePilotError
    from forgepilot.models.accounting import ModelBudget
    from test_model_gateway import READ, gateway_for

    sent = []

    def timeout(request):
        sent.append(json.loads(request.content))
        raise httpx.ReadTimeout("offline timeout", request=request)

    async with gateway_for(timeout, fallback=False) as gateway:
        with pytest.raises(ForgePilotError):
            await gateway.for_route("analysis", budget=ModelBudget(max_model_calls=1)).chat(
                [{"role": "user", "content": "搜索测试"}], tools=[READ])
        attempts = [asdict(row) for row in gateway.accounting.snapshot()]
    assert len(sent) == 1
    assert sent[0]["tools"][0]["function"]["parameters"] == READ.input_schema
    assert attempts[0]["status"] == "network"
    manifest, evidence, root = proof
    result = root / "bug-negative-clamp-r1/result.json"
    edit(result, lambda run: run.update(accounting=attempts))
    edit(root / "summary.json", lambda summary: summary.update(runs=[json.loads(result.read_bytes())]))
    edit(evidence, lambda detail: detail.update(request=sent[0]))
    edit(manifest, lambda m: m.update(evidence_sha256=hashlib.sha256(evidence.read_bytes()).hexdigest()))
    audit = load_reconciliation(*proof)
    assert audit["input_hash"] == attempts[0]["input_hash"]
    assert audit["call_id"] == attempts[0]["call_id"]
