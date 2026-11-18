"""Run the post-fix probes through the actual SDK/transport with synthetic HTTP only."""
from copy import deepcopy
import json
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import run_product_acceptance as harness
from acceptance_budget import BudgetTransport, RequestBudget
from forgepilot.models.profiles import ModelConfiguration, ModelProfile, ModelRoute


@pytest.fixture
def setup_probes(tmp_path, monkeypatch):
    profiles = tuple(ModelProfile(profile_id=p, model=m, family=p,
                                  base_url="https://ark.cn-beijing.volces.com/api/v3",
                                  max_output_tokens=32, api_key_ref="env:FORGEPILOT_API_KEY")
                     for p, m in (("first", "doubao-seed-2-1-test"), ("second", "deepseek-v4-1-test")))
    conf = ModelConfiguration(profiles=profiles, routes=(
        ModelRoute(route_id="chat", primary="second", fallback="first"),
        ModelRoute(route_id="summary", primary="first", fallback="second")))
    models = [{"profile": p.profile_id, "family": p.family, "model": p.model} for p in profiles]
    incoming = []
    failures = {}

    async def fake_config(_):
        return {"config_revision": "fixture", "fields": {
            "embedding_enabled": True, "embedding_model": "test-embedding",
            "embedding_base_url": profiles[0].base_url, "base_url": profiles[0].base_url}}

    def reply(request):
        incoming.append(json.loads(request.content))
        if len(incoming) in failures:
            return failures[len(incoming)](request)
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}],
                                             "usage": {"prompt_tokens": 3, "total_tokens": 3}})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "OK"},
                                                      "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}})

    monkeypatch.setattr(harness, "load_configuration", fake_config)
    monkeypatch.setattr(harness, "configuration_revision", lambda _: "fixture")
    monkeypatch.setattr(harness, "load_embedding_key", lambda _: "synthetic-key")
    monkeypatch.setattr(harness, "BudgetTransport", lambda *args, **kw: BudgetTransport(
        *args, **kw, inner=httpx.MockTransport(reply)))
    ledger = RequestBudget(tmp_path / "budget.sqlite3")
    old = tmp_path / "preflight.json"
    old.write_bytes(b'{"chat":"passed","summary":"passed","embedding":"passed"}')
    identity = {"runtime_identity": {"sdk": "fixture"}, "launcher_revision": "fixture"}

    async def run(*, label="revalidation", limits=None):
        configuration = conf
        if limits is not None:
            configuration = conf.model_copy(update={"profiles": tuple(
                p.model_copy(update={"max_output_tokens": limits[p.profile_id]}) for p in profiles)})
        data = await harness.run_revalidation(tmp_path / "mock-config", configuration, "synthetic-key",
                                              ledger, label=label, identity=identity)
        assert old.read_bytes() == b'{"chat":"passed","summary":"passed","embedding":"passed"}'
        assert data["identity"] == identity
        return data

    try:
        yield run, ledger, incoming, failures, models
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_revalidation_proves_each_probe_with_its_own_wire_usage(setup_probes):
    run, ledger, incoming, _, models = setup_probes
    data = await run()
    assert data["status"] == "passed" and data["network_requests"] == len(incoming) == 5
    assert data["reported_tokens"] == 39 and data["estimated_tokens"] == 0
    assert data["first_failure_reason"] is None and data["final_stop_reason"] is None
    assert [p["model"] for p in incoming] == [models[i]["model"] for i in (0, 1, 1, 0)] + ["test-embedding"]
    assert [p.get("max_completion_tokens", p.get("max_tokens")) for p in incoming] == [32, 32, 32, 32, None]
    from acceptance_revalidation import verify_revalidation

    attempts = ledger.wire_attempts("revalidation")
    verify_revalidation(data, attempts, models, require_embedding=True)
    for forged in ({"checks": []}, {"reported_tokens": 999}, {"network_requests": 0}):
        with pytest.raises(ValueError):
            verify_revalidation({**data, **forged}, attempts, models)
    changed = deepcopy(attempts)
    changed[0]["output_parameter"] = "max_tokens"
    with pytest.raises(ValueError):
        verify_revalidation({**data, "wire_attempts": changed}, changed, models)
    # Self-consistent truncated evidence cannot drop the frozen embedding check.
    truncated = {**data, "checks": data["checks"][:-1], "embedding_enabled": False, "network_requests": 4,
                 "reported_tokens": 36, "wire_attempts": attempts[:-1]}
    with pytest.raises(ValueError):
        verify_revalidation(truncated, attempts[:-1], models, require_embedding=True)
    with pytest.raises(ValueError):
        await run()
    assert len(incoming) == 5


@pytest.mark.asyncio
async def test_configured_preflight_uses_and_verifies_each_profiles_exact_output_limit(setup_probes):
    from acceptance_revalidation import verify_revalidation

    run, ledger, incoming, _, models = setup_probes
    limits = {"first": 4096, "second": 8192}
    legacy = await run(limits=limits)
    assert legacy["status"] == "passed"
    assert [c["output_limit"] for c in legacy["checks"]] == [1024, 1024, 1024, 1024, None]
    label = "revalidation-configured"
    result = await run(label=label, limits=limits)
    assert result["status"] == "passed" and result["network_requests"] == 5
    attempts = ledger.wire_attempts(label)
    verify_revalidation(result, attempts, models, require_embedding=True, configured_limits=limits)
    expected = [4096, 8192, 8192, 4096, None]
    assert [p.get("max_completion_tokens", p.get("max_tokens")) for p in incoming[5:]] == expected
    assert [check["output_limit"] for check in result["checks"]] == expected
    assert [a["output_limit"] for a in attempts] == expected
    with pytest.raises(ValueError):
        await run(label=label, limits=limits)
    assert len(incoming) == 10

    for bad_limits in (None, {"first": 4096}, {**limits, "unknown": 4096}, {**limits, "first": True}):
        with pytest.raises(ValueError):
            verify_revalidation(result, attempts, models, configured_limits=bad_limits)
    with pytest.raises(ValueError):
        verify_revalidation({**result, "run": "revalidation"}, attempts, models)
    # Even a matching result and wire observation must respect the saved profile.
    changed = deepcopy(result)
    changed["checks"][2]["output_limit"] = changed["wire_attempts"][2]["output_limit"] = 4096
    with pytest.raises(ValueError):
        verify_revalidation(changed, changed["wire_attempts"], models, configured_limits=limits)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,expected_count", [("missing", 1), ("timeout", 3), ("overrun", 1),
                                                     ("embedding_usage", 5)])
async def test_revalidation_stops_on_first_failed_probe(setup_probes, failure, expected_count):
    run, ledger, incoming, failures, _ = setup_probes

    def fail(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        if failure == "embedding_usage":
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})
        body = {"choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]}
        if failure == "overrun":
            body["usage"] = {"prompt_tokens": 1, "completion_tokens": 34, "total_tokens": 35}
        return httpx.Response(200, json=body)

    failures[expected_count] = fail
    data = await run()
    assert data["status"] == "failed" and data["first_failure_reason"] and data["final_stop_reason"]
    assert data["network_requests"] == len(incoming) == expected_count
    assert ledger.snapshot()["held"]
    if failure == "overrun":
        assert data["first_failure_reason"] == "output_limit_exceeded"
        assert data["reported_tokens"] == 35 and data["estimated_tokens"] == 0
    else:
        assert data["estimated_tokens"] > 0
    if failure == "timeout":
        assert data["first_failure_reason"] == "llm_timeout"


@pytest.mark.asyncio
async def test_config_drift_before_embedding_produces_no_charge(setup_probes, monkeypatch):
    run, ledger, incoming, _, _ = setup_probes
    monkeypatch.setattr(harness, "configuration_revision", lambda _: "changed")
    result = await run()
    assert result["first_failure_reason"] == result["final_stop_reason"] == "acceptance_config_drift"
    assert result["status"] == "failed" and result["checks"] == []
    assert not incoming and not ledger.snapshot()["requests"]
