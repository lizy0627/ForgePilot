"""Exercise the corrected acceptance contract at the actual HTTP dispatch boundary."""
import json
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from acceptance_budget import BudgetTransport, RequestBudget
from forgepilot.errors import ForgePilotError


@pytest.fixture
def ledger(tmp_path):
    budget = RequestBudget(tmp_path / "budget.sqlite3", 10_000)
    try:
        yield budget
    finally:
        budget.close()


def units(prompt, completion):
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


PAYLOAD = {"model": "doubao-seed-2-1-fixture", "messages": [{"role": "user", "content": "private prompt"}],
           "max_completion_tokens": 10}


def client(ledger, handler, label="revalidation"):
    return httpx.AsyncClient(transport=BudgetTransport(
        ledger, lambda: label, httpx.MockTransport(handler),
        verify_usage=True, total_output_parameter="max_completion_tokens"),
        base_url="https://example.invalid", headers={"Authorization": "Bearer never-log-this"})


@pytest.mark.asyncio
async def test_confirmed_output_overrun_within_request_reservation_freezes_actual_charge(ledger):
    calls = []

    def reply(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"usage": {**units(3, 11), "completion_tokens_details": {"reasoning_tokens": 8}},
                                        "choices": [{"message": {"content": "private reply"}}]})

    async with client(ledger, reply) as sdk:
        with pytest.raises(ForgePilotError, match="contract"):
            await sdk.post("/chat/completions", json=PAYLOAD)
        with pytest.raises(ForgePilotError, match="held"):
            await sdk.post("/chat/completions", json=PAYLOAD)
    assert len(calls) == 1
    snapshot = ledger.snapshot()
    assert snapshot["held"] and snapshot["charged_or_reserved"] == 14 < snapshot["requests"][0]["reserved"]
    attempt, = ledger.wire_attempts("revalidation")
    assert attempt["request_id"] == snapshot["requests"][0]["id"]
    assert attempt["failure_reason"] == "output_limit_exceeded"
    assert attempt["reported_tokens"] == 14 and not attempt["estimated_tokens"]
    assert attempt["output_parameter"] == "max_completion_tokens" and attempt["output_limit"] == 10
    assert attempt["observed_usage"][0]["reasoning_tokens"] == 8 and attempt["duration_ms"] >= 0
    stored = json.dumps(attempt)
    assert all(s not in stored for s in ("private prompt", "private reply", "never-log-this"))


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", [
    None, {}, {"total_tokens": 2}, {"prompt_tokens": 3, "completion_tokens": True, "total_tokens": 4},
    {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 900},
    {**units(3, 4), "completion_tokens_details": {"reasoning_tokens": 5}},
    units(0, 0),
])
async def test_http_ok_with_anomalous_usage_retains_observations_and_occupies_allowance(ledger, usage):
    def reply(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}], "usage": usage})
    async with client(ledger, reply) as sdk:
        with pytest.raises(ForgePilotError):
            await sdk.post("/chat/completions", json=PAYLOAD)
    attempt, = ledger.wire_attempts("revalidation")
    row, = ledger.snapshot()["requests"]
    assert attempt["failure_reason"] == "invalid_usage"
    assert row["estimated"] == 1 and row["charged"] >= row["reserved"] and ledger.snapshot()["held"]
    assert attempt["estimated_tokens"] == row["charged"] and attempt["reported_tokens"] == 0
    if usage is not None:
        assert attempt["observed_usage"][0].get("total_tokens") == usage.get("total_tokens")
    if usage and usage.get("total_tokens") == 900:
        assert row["charged"] == 900


@pytest.mark.asyncio
@pytest.mark.parametrize("same", [False, True])
async def test_stream_retains_both_reports_and_does_not_select_favorable_tail(ledger, same):
    first = units(900, 3) if not same else units(8, 3)
    second = units(8, 3)
    stream = "".join("data: " + json.dumps({"usage": v}) + "\n\n" for v in (first, second)) + "data: [DONE]\n\n"
    async with client(ledger, lambda _: httpx.Response(200, content=stream.encode())) as sdk:
        if same:
            assert (await sdk.post("/chat/completions", json={**PAYLOAD, "stream": True})).status_code == 200
        else:
            with pytest.raises(ForgePilotError):
                await sdk.post("/chat/completions", json={**PAYLOAD, "stream": True})
    attempt, = ledger.wire_attempts("revalidation")
    assert attempt["observed_usage"] == [first, second]
    if same:
        assert attempt["failure_reason"] is None and attempt["reported_tokens"] == 11
    else:
        assert attempt["failure_reason"] == "conflicting_usage"
        assert attempt["estimated_tokens"] == 903 and ledger.snapshot()["held"]


@pytest.mark.asyncio
@pytest.mark.parametrize("network", [True, False])
async def test_failed_attempt_preserves_first_safe_reason_and_prevents_fallback(ledger, network):
    calls = []
    def reply(request):
        calls.append(request)
        if network:
            raise httpx.ReadTimeout("sensitive provider text", request=request)
        return httpx.Response(503, json={"usage": units(4, 0)})
    async with client(ledger, reply) as sdk:
        with pytest.raises(ForgePilotError) as failure:
            await sdk.post("/chat/completions", json=PAYLOAD)
        assert failure.value.code == "llm_budget"
        with pytest.raises(ForgePilotError, match="held"):
            await sdk.post("/chat/completions", json={**PAYLOAD, "model": "unreached-alternative"})
    assert len(calls) == 1 and ledger.snapshot()["held"]
    attempt, = ledger.wire_attempts("revalidation")
    assert attempt["failure_reason"] == ("llm_timeout" if network else "http_503")
    assert attempt["transport_error"] == ("ReadTimeout" if network else None)
    assert (attempt["estimated_tokens"] > 0 if network else attempt["reported_tokens"] == 4)
    assert "sensitive" not in json.dumps(attempt)


@pytest.mark.asyncio
async def test_rejected_stream_preserves_billed_usage_from_json_error_document(ledger):
    async with client(ledger, lambda _: httpx.Response(429, json={"error": "throttled", "usage": units(17, 2)})) as sdk:
        with pytest.raises(ForgePilotError):
            await sdk.post("/chat/completions", json={**PAYLOAD, "stream": True})
    attempt, = ledger.wire_attempts("revalidation")
    assert attempt["failure_reason"] == "http_429" and attempt["reported_tokens"] == 19
    assert ledger.snapshot()["held"] and ledger.snapshot()["charged_or_reserved"] == 19


@pytest.mark.asyncio
async def test_embedding_contract_counts_input_and_has_no_generation_limit(ledger):
    async with client(ledger, lambda _: httpx.Response(200, json={"usage": {"prompt_tokens": 8, "total_tokens": 8},
                                                                  "data": [[7]]})) as sdk:
        await sdk.post("/embeddings", json={"model": "fixture-embedding", "input": "private input"})
    attempt, = ledger.wire_attempts("revalidation")
    assert attempt["kind"] == "embedding" and attempt["reported_tokens"] == 8
    assert attempt["output_parameter"] is attempt["output_limit"] is attempt["completion_tokens"] is None
    assert attempt["failure_reason"] is None


@pytest.mark.asyncio
async def test_wrong_total_parameter_produces_no_dispatched_attempt(ledger):
    async with client(ledger, lambda _: pytest.fail("Unbounded dispatch")) as sdk:
        with pytest.raises(ForgePilotError, match="output limit"):
            await sdk.post("/chat/completions", json={**PAYLOAD, "max_tokens": 10})
    assert ledger.wire_attempts("revalidation") == [] and ledger.snapshot()["requests"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,url,payload", [
    ("chat", "/v1/chat/completions", {**PAYLOAD, "model": "different-model"}),
    ("chat", "/v1/chat/completions", {**PAYLOAD, "max_completion_tokens": 11}),
    ("chat", "/v1/embeddings", {"model": PAYLOAD["model"], "input": "offline"}),
    ("chat", "https://other.invalid/v1/chat/completions", PAYLOAD),
    ("chat", "/different-api/chat/completions", PAYLOAD),
    ("embedding", "/v1/chat/completions", PAYLOAD),
    ("embedding", "/v1/embeddings/multimodal", {"model": "different-model", "input": "offline"}),
])
async def test_profile_pins_are_enforced_before_reservation_or_network(ledger, kind, url, payload):
    transport = BudgetTransport(
        ledger, lambda: "revalidation", httpx.MockTransport(lambda _: pytest.fail("Unexpected dispatch")),
        total_output_parameter="max_completion_tokens", verify_usage=True, expected_model=PAYLOAD["model"],
        api_root="https://example.invalid/v1", request_kind=kind, output_ceiling=10 if kind == "chat" else None)
    async with httpx.AsyncClient(transport=transport, base_url="https://example.invalid") as sdk:
        with pytest.raises(ForgePilotError) as failure:
            await sdk.post(url, json=payload)
    assert failure.value.code == "llm_budget"
    assert ledger.snapshot()["requests"] == [] and ledger.wire_attempts("revalidation") == []
