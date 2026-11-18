"""Offline tests at the actual campaign CLI / paid entry boundary."""
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import run_product_acceptance as acceptance
from acceptance_budget import RequestBudget
from reconciliation_support import make_fixture, mock_model_configuration, mock_revalidation_network


@pytest.fixture(name="campaign")
def fixture_campaign(tmp_path, monkeypatch):
    directory = tmp_path / "real-acceptance"
    directory.mkdir()
    monkeypatch.setattr(acceptance, "PAID_CAMPAIGN", directory)
    for name in ("load_saved_key", "load_embedding_key", "saved_configuration"):
        monkeypatch.setattr(acceptance, name, lambda *a: pytest.fail("offline guard reached credentials/config"))
    acceptance.save(directory / "plan.json", acceptance.plan(historical=True))
    return directory


@pytest.mark.asyncio
async def test_lost_database_never_creates_replacement_or_reads_credentials(campaign):
    original = b'{"stop_reason":"shared_budget_exhausted"}\n'
    (campaign / "summary.json").write_bytes(original)
    with pytest.raises((ValueError, FileNotFoundError)):
        await acceptance.execute(campaign)
    assert not (campaign / "budget.sqlite3").exists()
    assert (campaign / "summary.json").read_bytes() == original


@pytest.mark.asyncio
async def test_clearing_old_stop_flags_is_insufficient(campaign):
    fixture_inputs(campaign)
    budget = RequestBudget(campaign / "budget.sqlite3")
    try:
        with budget.transaction():
            budget.db.execute("UPDATE campaign SET held=0")
    finally:
        budget.close()
    summary = json.loads((campaign / "summary.json").read_bytes())
    summary.pop("stop_reason")
    acceptance.save(campaign / "summary.json", summary)
    original = (campaign / "summary.json").read_bytes()
    with pytest.raises(ValueError):
        await acceptance.execute(campaign)
    assert (campaign / "summary.json").read_bytes() == original


def command(monkeypatch, directory, *args):
    monkeypatch.setattr(sys, "argv", ["acceptance", "--output", str(directory), *map(str, args)])
    acceptance.main()


@pytest.mark.parametrize("action", [("--execute", "--resume"), ("--rebuild-recovery",),
                                    ("--reconcile", "manifest", "provider-detail")])
def test_alternate_directory_cannot_initialize_or_restore(campaign, monkeypatch, action):
    other = campaign.with_name("new-budget")
    with pytest.raises(SystemExit) as error:
        command(monkeypatch, other, *action)
    assert error.value.code == 2 and not other.exists()


def fixture_inputs(campaign):
    inputs = make_fixture(campaign)
    summary = json.loads((campaign / "summary.json").read_bytes())
    config = mock_model_configuration()
    summary["model_configuration_sha256"] = hashlib.sha256(config.model_dump_json().encode()).hexdigest()
    summary["models"] = [{"profile": p.profile_id, "model": p.model, "family": p.family} for p in config.profiles]
    acceptance.save(campaign / "summary.json", summary)
    return inputs


def original_files(campaign):
    names = ("plan.json", "summary.json", "preflight.json", "budget-hold-audit.json",
             "bug-negative-clamp-r1/result.json")
    return {name: (campaign / name).read_bytes() for name in names}


def offline_configuration(monkeypatch, campaign, *, revision="2" * 64):
    fake = mock_model_configuration()
    monkeypatch.setattr(acceptance, "saved_configuration", lambda: (campaign / "fixture-config", fake))
    monkeypatch.setattr(acceptance, "configuration_revision", lambda path: revision)


async def single_sample_call(config, key, ledger, label):
    async with acceptance.gateway(config, key, ledger, label, verify_usage=True) as models:
        response = await models.for_route("chat").chat([{"role": "user", "content": label}])
        assert response.message.content == "OK"
    attempt, = ledger.wire_attempts(label)
    return {"network_requests": 1, "reported_tokens": attempt["reported_tokens"],
            "estimated_tokens": attempt["estimated_tokens"], "tokens": attempt["reported_tokens"],
            "wire_attempts": [attempt]}


def test_offline_cli_keeps_reconciliation_and_records_slot_failures_independently(campaign, monkeypatch):
    inputs = fixture_inputs(campaign)
    originals = original_files(campaign)
    command(monkeypatch, campaign, "--reconcile", *inputs)
    immutable = next(campaign.glob("recovery-*.json"))
    first = immutable.read_bytes()
    with pytest.raises(ValueError):
        command(monkeypatch, campaign, "--reconcile", *inputs)
    command(monkeypatch, campaign, "--rebuild-recovery")
    assert immutable.read_bytes() == first and original_files(campaign) == originals
    offline_configuration(monkeypatch, campaign)
    monkeypatch.setattr(acceptance, "load_saved_key", lambda path: "offline-fixture")
    labels = []
    sent = mock_revalidation_network(monkeypatch)

    async def synthetic_sample(task, repeat, config, key, ledger, output, *, identity):
        assert key == "offline-fixture"
        label = task["id"] + "-r" + str(repeat)
        labels.append(label)
        assert len(sent) == 4 + len(labels) - 1
        usage = await single_sample_call(config, key, ledger, label)
        result = {"run": label, "status": "failed", "reason": "llm_budget", "identity": identity, **usage}
        (output / label).mkdir()
        acceptance.save(output / label / "result.json", result)
        return result

    monkeypatch.setattr(acceptance, "run_sample", synthetic_sample)
    command(monkeypatch, campaign, "--execute", "--resume")
    command(monkeypatch, campaign, "--execute", "--resume")
    expected = [f"{task['id']}-r{repeat}" for repeat in range(1, 4) for task in acceptance.TASKS][1:]
    assert labels == expected and len(sent) == 15
    progress = json.loads((campaign / "progress-continuation.json").read_bytes())
    assert progress["budget"]["charged_or_reserved"] == 140 + 15 * 6 and not progress["budget"]["held"]
    assert not progress.get("stop_reason") and progress["attempted_runs"] == 12 and progress["passed_runs"] == 0
    for run in progress["runs"][1:]:
        assert run == json.loads((campaign / run["run"] / "result.json").read_bytes())
        assert len(run["wire_attempts"]) == run["network_requests"] == 1
        assert run["reason"] == "llm_budget" and run["reported_tokens"] == 6
    assert immutable.read_bytes() == first and original_files(campaign) == originals


def test_progress_write_failure_and_config_drift_stop_before_key(campaign, monkeypatch):
    inputs = fixture_inputs(campaign)
    command(monkeypatch, campaign, "--reconcile", *inputs)
    originals = original_files(campaign)
    offline_configuration(monkeypatch, campaign, revision="different")
    with pytest.raises(ValueError, match="configuration"):
        command(monkeypatch, campaign, "--execute", "--resume")
    offline_configuration(monkeypatch, campaign)

    def disk_full(path, value):
        assert path.name.startswith("progress-")
        raise OSError("cannot write checkpoint")

    monkeypatch.setattr(acceptance, "save", disk_full)
    with pytest.raises(OSError, match="checkpoint"):
        command(monkeypatch, campaign, "--execute", "--resume")
    assert original_files(campaign) == originals
    ledger = RequestBudget(campaign / "budget.sqlite3", existing_only=True)
    try:
        assert len(ledger.snapshot()["requests"]) == 2 and len(ledger.reconciliation_audits()) == 1
    finally:
        ledger.close()


@pytest.mark.parametrize("committed", [False, True])
def test_process_death_before_or_after_reconciliation_commit(campaign, committed, monkeypatch):
    inputs = fixture_inputs(campaign)
    originals = original_files(campaign)
    root = Path(acceptance.__file__).resolve().parents[1]
    # os._exit skips Python cleanup and connection.close(). The pre-commit
    # branch dies between inserting the audit and updating the request.
    script = """
import os, sys
from pathlib import Path
from acceptance_budget import RequestBudget
from acceptance_recovery import capture_stop
campaign, manifest, detail = map(Path, sys.argv[1:4])
ledger = RequestBudget(campaign / 'budget.sqlite3', existing_only=True)
if sys.argv[4] == 'False':
    ledger.db.create_function('terminate_fixture', 0, lambda: os._exit(81))
    ledger.db.execute("CREATE TEMP TRIGGER crash BEFORE UPDATE OF charged ON requests "
                      "BEGIN SELECT terminate_fixture(); END")
ledger.reconcile(manifest, detail, anchors=capture_stop(campaign))
os._exit(82)
"""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(str(root / d) for d in ("scripts", "agent", "eval"))}
    child = subprocess.run([sys.executable, "-c", script, str(campaign), *map(str, inputs),
                            str(committed)], cwd=campaign, env=env, capture_output=True)
    assert child.returncode == (82 if committed else 81), child.stderr.decode(errors="replace")
    ledger = RequestBudget(campaign / "budget.sqlite3", existing_only=True)
    try:
        assert len(ledger.reconciliation_audits()) == int(committed)
        assert ledger.snapshot()["charged_or_reserved"] == (140 if committed else 300_000)
        assert ledger.snapshot()["held"] is not committed
        with pytest.raises(acceptance.ForgePilotError, match="held|recovery"):
            ledger.reserve(1, "crash")
    finally:
        ledger.close()
    if committed:
        from acceptance_recovery import open_recovery
        from acceptance_reconciliation import runtime_identity

        # A bounded recovery still needs its derived manifest after a crash.
        original_ledger = RequestBudget(campaign / "budget.sqlite3", existing_only=True)
        try:
            with pytest.raises(FileNotFoundError):
                open_recovery(campaign, original_ledger, runtime_identity())
        finally:
            original_ledger.close()
        command(monkeypatch, campaign, "--rebuild-recovery")
        assert len(list(campaign.glob("recovery-*.json"))) == 1
    assert original_files(campaign) == originals
