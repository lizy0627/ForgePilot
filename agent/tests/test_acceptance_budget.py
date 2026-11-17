"""The product acceptance campaign cannot issue an unreserved real request."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from acceptance_budget import BudgetTransport, RequestBudget
from forgepilot.errors import ForgePilotError


@pytest.mark.asyncio
async def test_paid_entry_rejects_another_directory_before_credentials_or_ledger(tmp_path, monkeypatch):
    import run_product_acceptance as acceptance

    campaign, alternate = tmp_path / "campaign", tmp_path / "another"
    monkeypatch.setattr(acceptance, "PAID_CAMPAIGN", campaign)
    def forbidden_configuration():
        pytest.fail("Rejected paid entry must not load credentials or configuration")
    monkeypatch.setattr(acceptance, "saved_configuration", forbidden_configuration)
    with pytest.raises(ValueError, match="existing stage-six campaign"):
        await acceptance.execute(alternate)
    monkeypatch.setattr(sys, "argv", ["acceptance", "--execute", "--output", str(alternate)])
    with pytest.raises(SystemExit) as error:
        acceptance.main()
    assert error.value.code == 2
    assert not campaign.exists() and not alternate.exists()


@pytest.mark.asyncio
async def test_original_held_campaign_can_run_preflight_before_reconciliation(tmp_path, monkeypatch):
    import run_product_acceptance as acceptance
    from reconciliation_support import mock_revalidation_network
    from test_acceptance_resume_entry import fixture_inputs, offline_configuration, original_files

    monkeypatch.setattr(acceptance, "PAID_CAMPAIGN", tmp_path)
    fixture_inputs(tmp_path)
    originals = original_files(tmp_path)
    offline_configuration(monkeypatch, tmp_path)
    sent = mock_revalidation_network(monkeypatch)
    monkeypatch.setattr(acceptance, "load_saved_key", lambda _: "fixture")
    path = tmp_path / "budget.sqlite3"
    ledger = RequestBudget(path, existing_only=True)
    try:
        before = ledger.snapshot()
        assert before["held"] and before["charged_or_reserved"] == before["limit"]
        await acceptance.execute(tmp_path, revalidate_only=True)
        await acceptance.execute(tmp_path, revalidate_only=True)
        after = ledger.snapshot()
        assert after["held"] and after["charged_or_reserved"] == before["limit"] + 24
        assert all(row in after["requests"] for row in before["requests"])
        assert len(sent) == len(ledger.wire_attempts("revalidation")) == 4
        assert len(after["requests"]) == len(before["requests"]) + 4
        assert ledger.reconciliation_audits() == []
        assert original_files(tmp_path) == originals
    finally:
        ledger.close()


def test_concurrent_reservations_and_crash_reopen_keep_shared_cap(tmp_path):
    path = tmp_path / "budget.sqlite3"
    ledger = RequestBudget(path, 100)
    def reserve(_):
        try:
            return ledger.reserve(30, "parallel")
        except ForgePilotError:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        reservations = [value for value in pool.map(reserve, range(8)) if value]
    assert len(reservations) == 3
    ledger.settle(reservations[0], {"total_tokens": 10}, "success")
    ledger.close()
    restored = RequestBudget(path, 100)
    try:
        assert restored.snapshot()["charged_or_reserved"] == 70
        restored.reserve(30, "restart")
        with pytest.raises(ForgePilotError):
            restored.reserve(1, "overrun")
    finally:
        restored.close()


@pytest.mark.parametrize("usage", [None, {}, {"total_tokens": -1}, {"total_tokens": True},
                                    {"total_tokens": 1, "prompt_tokens": 5, "completion_tokens": 5}])
def test_missing_or_invalid_usage_charges_full_reservation(tmp_path, usage):
    ledger = RequestBudget(tmp_path / "budget.sqlite3", 100)
    try:
        token = ledger.reserve(100, "unknown")
        assert ledger.settle(token, usage, "response")
        assert ledger.snapshot()["charged_or_reserved"] == 100
        assert ledger.snapshot()["requests"][0]["estimated"] == 1
        with pytest.raises(ForgePilotError):
            ledger.reserve(1, "blocked")
    finally:
        ledger.close()


def test_provider_overrun_is_durable_and_stops_later_requests(tmp_path):
    path = tmp_path / "budget.sqlite3"
    ledger = RequestBudget(path, 100)
    token = ledger.reserve(20, "unexpected")
    assert not ledger.settle(token, {"total_tokens": 21}, "response")
    ledger.close()
    ledger = RequestBudget(path, 100)
    try:
        with pytest.raises(ForgePilotError):
            ledger.reserve(1, "after-overrun")
        assert ledger.snapshot()["charged_or_reserved"] == 21
        assert ledger.snapshot()["held"]
    finally:
        ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream,embedding", [(False, False), (True, False), (False, True)])
async def test_http_attempts_chat_stream_and_embedding_share_one_ledger(tmp_path, stream, embedding):
    ledger = RequestBudget(tmp_path / "budget.sqlite3", 10_000)
    async def reply(request):
        usage = {"total_tokens": 17}
        if stream:
            return httpx.Response(200, content=b'data: {"choices":[],"usage":{"total_tokens":17}}\n\ndata: [DONE]\n\n')
        return httpx.Response(200, json={"usage": usage, "data": []})
    try:
        transport = BudgetTransport(ledger, lambda: "test", httpx.MockTransport(reply))
        async with httpx.AsyncClient(transport=transport) as client:
            payload = {"input": "offline query"} if embedding else {
                "messages": [{"role": "user", "content": "offline"}], "max_tokens": 20, "stream": stream}
            response = await client.post("https://example.invalid/" + ("embeddings" if embedding else "chat/completions"), json=payload)
            assert response.status_code == 200
        assert ledger.snapshot()["charged_or_reserved"] == 17
        assert ledger.snapshot()["requests"][0]["estimated"] == 0
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_retry_errors_are_charged_and_later_request_is_blocked_before_network(tmp_path):
    ledger = RequestBudget(tmp_path / "budget.sqlite3", 200)
    attempts = []
    async def unavailable(request):
        attempts.append(request)
        raise httpx.ConnectError("offline", request=request)
    try:
        async with httpx.AsyncClient(transport=BudgetTransport(ledger, lambda: "retry", httpx.MockTransport(unavailable))) as client:
            payload = {"messages": [], "max_tokens": 20}
            for _ in range(3):
                try:
                    await client.post("https://example.invalid/chat/completions", json=payload)
                except (httpx.ConnectError, ForgePilotError):
                    pass
        snapshot = ledger.snapshot()
        assert len(attempts) == 2
        assert snapshot["charged_or_reserved"] <= 200
        assert all(row["estimated"] for row in snapshot["requests"])
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_unbounded_output_is_rejected_without_transport_or_reservation(tmp_path):
    ledger = RequestBudget(tmp_path / "budget.sqlite3", 1000)
    async def unexpected(request):
        pytest.fail("Unbounded request reached network")
    try:
        async with httpx.AsyncClient(transport=BudgetTransport(ledger, lambda: "check", httpx.MockTransport(unexpected))) as client:
            with pytest.raises(ForgePilotError):
                await client.post("https://example.invalid/chat/completions", json={"messages": []})
        assert ledger.snapshot()["requests"] == []
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_gateway_fallback_attempts_and_accounting_use_the_same_ledger(tmp_path):
    from forgepilot.models.gateway import ModelGateway
    from forgepilot.models.profiles import ModelConfiguration, ModelProfile, ModelRoute
    ledger = RequestBudget(tmp_path / "budget.sqlite3", 10_000)
    calls = []
    async def respond(request):
        model = json.loads(request.content)["model"]
        calls.append(model)
        if model == "primary":
            return httpx.Response(503, text="offline failure without usage")
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "OK"},
            "finish_reason": "stop"}], "usage": {"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 9}})
    config = ModelConfiguration(profiles=tuple(ModelProfile(profile_id=name, model=name, family=name,
        base_url="https://example.invalid/v1", api_key_ref="env:FIXTURE_KEY", max_output_tokens=32)
        for name in ("primary", "fallback")), routes=(ModelRoute(route_id="chat", primary="primary", fallback="fallback"),),
        enforce_review_family_separation=False)
    try:
        async with ModelGateway(config, secret_resolver=lambda _: "offline-key",
                transport_factory=lambda _: BudgetTransport(ledger, lambda: "fallback", httpx.MockTransport(respond))) as gateway:
            reply = await gateway.for_route("chat").chat([{"role": "user", "content": "offline"}])
            assert reply.message.content == "OK"
            assert len(gateway.accounting.snapshot()) == 2
        rows = ledger.snapshot()["requests"]
        assert calls == ["primary", "fallback"]
        assert len(rows) == 2 and sum(row["estimated"] for row in rows) == 1
        assert ledger.snapshot()["charged_or_reserved"] == sum(row["charged"] for row in rows)
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_answer_only_limit_cannot_enter_total_output_transport(tmp_path):
    ledger = RequestBudget(tmp_path / "budget.sqlite3", 10_000)
    async def respond(request):
        pytest.fail("Answer-only bound reached a total-output provider")
    try:
        async with httpx.AsyncClient(transport=BudgetTransport(ledger, lambda: "total-output",
                httpx.MockTransport(respond), total_output_parameter="max_completion_tokens")) as client:
            with pytest.raises(ForgePilotError):
                await client.post("https://example.invalid/chat/completions", json={
                    "messages": [{"role": "user", "content": "offline"}], "max_tokens": 128})
        assert ledger.snapshot()["requests"] == []
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_ark_provider_and_campaign_ledger_agree_on_total_output_limit(tmp_path):
    from forgepilot.models.openai import OpenAICompatibleClient
    ledger = RequestBudget(tmp_path / "budget.sqlite3", 10_000)
    async def respond(request):
        payload = json.loads(request.content)
        assert payload["max_completion_tokens"] == 128 and "max_tokens" not in payload
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "OK"},
            "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 90, "total_tokens": 100}})
    try:
        async with OpenAICompatibleClient("https://ark.cn-beijing.volces.com/api/v3", "offline-key",
                "doubao-seed-2-1-lite-260915", max_output_tokens=128,
                transport=BudgetTransport(ledger, lambda: "total-output", httpx.MockTransport(respond),
                                          total_output_parameter="max_completion_tokens")) as provider:
            assert (await provider.chat([])).message.content == "OK"
        assert ledger.snapshot()["charged_or_reserved"] == 100
        assert ledger.snapshot()["requests"][0]["reserved"] >= 128
    finally:
        ledger.close()
