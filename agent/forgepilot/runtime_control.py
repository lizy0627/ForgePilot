"""Local launcher restart gate on the existing admission loop."""
from pydantic import BaseModel, ConfigDict

from .errors import ForgePilotError


class RuntimeStatus(BaseModel):
    active_count: int
    restarting: bool


class PrepareRestart(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


def register_runtime_routes(app, host):
    async def status(prepare=False):
        # This coroutine has no yield between the count and admission fence.
        count = host.capacity.active_count()
        if prepare:
            if count:
                raise ForgePilotError("runtime_busy", "Tasks are still active; saved settings apply after they finish")
            host.runtime.stop_accepting()
        return RuntimeStatus(active_count=count, restarting=host.runtime.stopping)

    @app.get("/api/v2/runtime/status", response_model=RuntimeStatus)
    async def runtime_status():
        return await host.runtime.submit(status())

    @app.post("/api/v2/runtime/prepare-restart", response_model=RuntimeStatus)
    async def prepare_restart(body: PrepareRestart):
        return await host.runtime.submit(status(prepare=True))
