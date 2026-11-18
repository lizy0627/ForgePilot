"""Opt-in observations for the two-editor HTTP regression, without payloads.

Only the target app and the connections it creates in an editor context are
wrapped. The caller installs this before Gate.configure and removes all patches
after the server/worker have joined. No deadlines or cancellation are changed.
"""

import asyncio


def _error_fields(exc):
    # Exception messages can contain endpoints, headers or response fragments.
    fields = {"exception_type": type(exc).__name__}
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code.startswith("mcp_"):
        fields["error_code"] = code
    return fields


def install_server_trace(trace, monkeypatch, app):
    """Install observations only when explicitly called by the trace fixture."""
    host = app.state.task_host
    service = host.mcp_service

    def observe_async(target, name, prefix, *, role=None, resources=None, editor=None):
        original = getattr(target, name)

        async def observed(*args, **kwargs):
            editor_id = editor or trace.editor.get()
            if editor_id is None:
                return await original(*args, **kwargs)
            token = trace.editor.set(editor_id)
            try:
                if role:
                    trace.track(role, asyncio.current_task())
                trace.event(prefix + ".start")
                try:
                    result = await original(*args, **kwargs)
                except BaseException as exc:
                    trace.event(prefix + ".exception", **_error_fields(exc),
                                **(resources() if resources else {}))
                    raise
                trace.event(prefix + ".complete", **(resources() if resources else {}))
                return result
            finally:
                trace.editor.reset(token)

        monkeypatch.setattr(target, name, observed)

    observe_async(host, "run_mcp", "host.run_mcp", role="http-waiter",
                  resources=lambda: {"active_count": len(host._mcp_active)})
    # These boundaries observe the actual worker invocation without inserting
    # a nested coroutine into HostRuntime's scheduling/cancellation protocol.
    observe_async(service, "save", "worker.operation", role="worker-operation")

    def observe_sync(target, name, prefix):
        original = getattr(target, name)

        def observed(*args, **kwargs):
            if trace.editor.get() is None:
                return original(*args, **kwargs)
            trace.event(prefix + ".start")
            try:
                result = original(*args, **kwargs)
            except BaseException as exc:
                trace.event(prefix + ".exception", **_error_fields(exc))
                raise
            fields = {}
            if name == "save":
                fields["revision"] = result["revision"]
            trace.event(prefix + ".complete", **fields)
            return result

        monkeypatch.setattr(target, name, observed)

    observe_sync(service, "_catalog", "catalog.validate")
    observe_sync(service.store, "recheck", "cas.recheck")
    observe_sync(service.store, "save", "cas.save")
    original_factory = service.connection_factory

    def connection(config):
        client = original_factory(config)
        editor = trace.editor.get()
        if editor is None:
            return client

        def emit(event, **fields):
            # A cancel caller or done callback may have another context.
            token = trace.editor.set(editor)
            try:
                trace.event(event, **fields)
            finally:
                trace.editor.reset(token)

        def resource_fields():
            owner = client.owner
            return {"owner_done": owner is not None and owner.done(),
                    "transport_closed": client.transport.closed}

        original_run = client._run

        async def run():
            # start() assigns owner before this coroutine gets its first turn.
            # Wrapping this one Task's bound cancel method observes the real
            # calls from _abort, _close_owned and its absolute cleanup timer.
            owner = asyncio.current_task()
            trace.track("owner", owner)
            original_cancel = owner.cancel

            def cancel(*args, **kwargs):
                emit("owner.cancel", done=owner.done(), cancelling=owner.cancelling())
                return original_cancel(*args, **kwargs)

            monkeypatch.setattr(owner, "cancel", cancel)

            def completed(task):
                emit("owner.complete", cancelled=task.cancelled(), **resource_fields())

            owner.add_done_callback(completed)
            emit("owner.start")
            try:
                return await original_run()
            finally:
                emit("owner.finally", **resource_fields())

        monkeypatch.setattr(client, "_run", run)
        observe_async(client, "list_tools", "list_tools", editor=editor)
        observe_async(client, "close", "client.close", resources=resource_fields, editor=editor)
        observe_async(client, "_close_owned", "client.close_owned", resources=resource_fields,
                      role="closer", editor=editor)
        observe_async(client.transport, "aclose", "transport.close", resources=resource_fields, editor=editor)
        emit("connection.created")
        return client

    monkeypatch.setattr(service, "connection_factory", connection)
