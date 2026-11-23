"""Explicit, worker-loop-owned generation jobs for local skill drafts."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from functools import partial
from time import monotonic

from .artifacts.models import DataPolicy
from .errors import ForgePilotError
from .models.accounting import ModelBudget
from .models.routing import route_uses_cloud
from .skill_draft_evidence import TEMPLATE_VERSION, prepare_draft_input, suggestion_schema, validate_suggestion


MODEL_ROUTE = "summary"
TIMEOUT_SECONDS = 60


@dataclass
class _Job:
    attempt_id: str
    start_revision: int
    started: float = field(default_factory=monotonic)
    route: str | None = None
    task: asyncio.Task | None = None


class SkillDraftJobs:
    def __init__(self, store, runtime, gateway, current_policy, *, capacity):
        self.store = store
        self.runtime = runtime
        self.gateway = gateway
        self.current_policy = current_policy
        self.capacity = capacity
        self._accepting = False
        self._jobs: dict[str, _Job] = {}

    async def start(self):
        # Opening the store has already interrupted any orphaned attempts.
        async def enable():
            self._accepting = True
        await self.runtime.submit(enable())

    async def create(self, task_id, *, developer_note="", request_id=None):
        return await self.runtime.submit(self._admit(
            self.store.skills.create_generation, task_id, developer_note=developer_note, request_id=request_id))

    async def regenerate(self, draft_id, *, expected_revision, request_id):
        return await self.runtime.submit(self._admit(
            self.store.skills.regenerate, draft_id, expected_revision=expected_revision, request_id=request_id))

    async def _admit(self, persist, identity, **kwargs):
        if not self._accepting or self.runtime.stopping:
            raise ForgePilotError("server_stopping", "Draft generation service is stopping")
        draft, attempt_id, started = persist(identity, template_version=TEMPLATE_VERSION, **kwargs)
        if not started:
            return draft, attempt_id
        job = _Job(attempt_id, draft.latest_attempt.start_revision)
        try:
            self.capacity.acquire("skill_draft", attempt_id, message="Draft generation capacity reached")
        except ForgePilotError as exc:
            self._finish(job, "failed", error_code=exc.code)
            return self.store.skills.get_draft(draft.draft_id, verify_artifacts=False), attempt_id
        self._jobs[attempt_id] = job
        job.task = asyncio.create_task(self._generate(job, draft))
        job.task.add_done_callback(lambda task: self._retire(job, task))
        return draft, attempt_id

    def _retire(self, job, task):
        self._jobs.pop(job.attempt_id, None)
        self.capacity.release("skill_draft", job.attempt_id)
        # Storage failures must be visible without logging model inputs or exception payloads.
        if not task.cancelled() and task.exception() is not None:
            asyncio.get_running_loop().call_exception_handler({"message": "Failed to persist skill generation outcome"})

    def _finish(self, job, status, **result):
        return self.store.skills.finish_generation(job.attempt_id, start_revision=job.start_revision,
            status=status, elapsed_ms=int((monotonic() - job.started) * 1000), route=job.route, **result)

    def _stop(self, job, status, code):
        # Commit first. A slow provider may suppress cancellation and return later.
        self._finish(job, status, error_code=code)
        if job.task is not None:
            job.task.cancel()

    async def _generate(self, job, draft):
        timer = None
        usage = None
        try:
            if self.gateway is None:
                raise ForgePilotError("skill_model_unavailable", "No model gateway for generation")
            if not isinstance(self.current_policy, DataPolicy):
                raise ForgePilotError("skill_draft_policy", "Missing current model policy")
            client = self.gateway.for_route(MODEL_ROUTE, prompt_version=TEMPLATE_VERSION,
                budget=ModelBudget(max_model_calls=1, max_tool_calls=0, timeout_seconds=TIMEOUT_SECONDS))
            job.route = f"{MODEL_ROUTE}/{client.route.primary.profile_id}"
            prepared = prepare_draft_input(self.store, draft, job.attempt_id, self.current_policy,
                                           redactor=self.gateway.redactor)
            if not prepared.policy.cloud_model_allowed and route_uses_cloud(client.route):
                raise ForgePilotError("skill_route_denied", "Generation requires local primary and fallback")
            if not self.store.skills.record_generation_route(job.attempt_id, job.start_revision, job.route):
                return
            timeout = min(client.route.primary.timeout_seconds, client.budget.remaining_seconds())
            # GatewayClient owns the provider timeout. Publish the durable deadline
            # independently so a provider suppressing its cancellation cannot revive it.
            timer = asyncio.get_running_loop().call_later(timeout, partial(
                self._finish, job, "timed_out", error_code="llm_timeout"))
            records = []
            try:
                with self.gateway.accounting.scoped_sink(records.append):
                    reply = await client.chat(prepared.messages, output_schema=suggestion_schema(), allow_recovery=False)
            finally:
                # Even a reply rejected by the output schema can have metered usage.
                if records and not records[0]["usage_estimated"]:
                    usage = {"prompt_tokens": records[0]["input_tokens"], "completion_tokens": records[0]["output_tokens"]}
            usage = reply.usage or usage
            suggestion = validate_suggestion(reply.message.content, prepared)
        except asyncio.CancelledError:
            # Cancel and shutdown handlers persist the terminal state before signalling.
            self._finish(job, "cancelled", error_code="generation_cancelled")
            raise
        except ForgePilotError as exc:
            # Gateway errors can originate in a provider. Store only application-defined codes.
            code = exc.code if exc.code in {
                "skill_model_unavailable", "skill_draft_policy", "invalid_skill_evidence",
                "skill_draft_evidence_insufficient", "invalid_skill_suggestion", "skill_route_denied",
                "llm_budget", "llm_config", "llm_timeout", "llm_http", "llm_schema",
            } else "skill_generation_failed"
            self._finish(job, "timed_out" if code == "llm_timeout" else "failed",
                         error_code=code, usage=usage)
        except Exception:
            self._finish(job, "failed", error_code="skill_generation_failed", usage=usage)
        else:
            self._finish(job, "ready", suggestion=suggestion, usage=usage)
        finally:
            if timer is not None:
                timer.cancel()

    async def cancel(self, draft_id, attempt_id):
        async def stop():
            attempt = self.store.skills.get_attempt(draft_id, attempt_id)
            job = self._jobs.get(attempt_id)
            if job is not None and attempt.status == "generating":
                self._stop(job, "cancelled", "generation_cancelled")
                return self.store.skills.get_attempt(draft_id, attempt_id)
            return attempt
        return await self.runtime.submit(stop())

    async def close(self):
        async def drain():
            self._accepting = False
            jobs = tuple(self._jobs.values())
            for job in jobs:
                self._stop(job, "interrupted", "generation_interrupted")
            await asyncio.gather(*(job.task for job in jobs), return_exceptions=True)
        await self.runtime.drain(drain())
