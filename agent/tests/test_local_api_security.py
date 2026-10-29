"""Local transport authentication, bounded parsing, and safe error regression tests."""

import httpx
import pytest
from fastapi.responses import StreamingResponse

from forgepilot.api import create_app, MAX_API_BODY
from forgepilot.errors import ForgePilotError
from forgepilot.security import Redactor


@pytest.mark.asyncio
async def test_token_covers_http_and_sse_and_origin_is_still_checked():
    token = "fixture-local-unguessable-token"
    app = create_app(lambda trace: None, api_token=token)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1") as client:
            for path in ("/api/v1/tasks", "/api/v2/missions/00000000-0000-4000-8000-000000000000/events"):
                denied = await client.get(path)
                assert denied.status_code == 401 and token not in denied.text
                allowed = await client.get(path, headers={"X-ForgePilot-Token": token})
                assert allowed.status_code != 401
                duplicate = await client.get(path, headers=[("X-ForgePilot-Token", token),
                                                            ("X-ForgePilot-Token", token)])
                assert duplicate.status_code == 401
            cross_origin = await client.get("/api/v1/tasks", headers={
                "X-ForgePilot-Token": token, "Origin": "https://attacker.invalid"})
            assert cross_origin.status_code == 403
    finally:
        app.state.task_host.store.close()


@pytest.mark.asyncio
async def test_legacy_body_limit_and_error_paths_do_not_echo_secrets(monkeypatch):
    secret = "fixture-secret-from-stack"
    app = create_app(lambda trace: None)
    app.state.task_host.store.redactor = Redactor((secret,))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1") as client:
            async def oversized():
                for _ in range(MAX_API_BODY // 65536 + 1):
                    yield b"s" * 65536
            large = await client.post("/api/v1/tasks", content=oversized())
            assert large.status_code == 413 and secret not in large.text

            async def denied(*args, **kwargs):
                raise ForgePilotError("permission_denied", "Access denied: " + secret)
            monkeypatch.setattr(app.state.task_host, "create", denied)
            result = await client.post("/api/v1/tasks", json={
                "repository_path": "D:/fixture", "prompt": "fix"})
            assert result.status_code == 422 and secret not in result.text
            invalid = await client.post("/api/v1/tasks", json={
                "repository_path": "D:/fixture", secret: "private"})
            assert invalid.status_code == 422 and secret not in invalid.text
    finally:
        app.state.task_host.store.close()


@pytest.mark.asyncio
async def test_unexpected_endpoint_and_stream_exceptions_never_escape_to_server_log(caplog):
    secret = "unconfigured-private-stack-value-79"
    app = create_app(lambda trace: None)

    @app.get("/api/v1/_fault")
    def fault():
        raise RuntimeError("private traceback: " + secret)

    @app.get("/api/v1/_stream_fault")
    def stream_fault():
        async def frames():
            yield "event: progress\ndata: {}\n\n"
            raise RuntimeError("private stream traceback: " + secret)
        return StreamingResponse(frames(), media_type="text/event-stream")

    try:
        # ASGITransport's default raise_app_exceptions=True reproduces the
        # ServerErrorMiddleware re-raise path that Uvicorn would log.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1") as client:
            result = await client.get("/api/v1/_fault")
            assert result.status_code == 500 and secret not in result.text
            streamed = await client.get("/api/v1/_stream_fault")
            assert streamed.status_code == 200 and secret not in streamed.text
        assert secret not in caplog.text
    finally:
        app.state.task_host.store.close()


def test_encoded_known_credentials_and_sensitive_structured_fields():
    import base64
    secret = "api-secret-opaque-567"
    redactor = Redactor((secret,))
    variants = (base64.b64encode(secret.encode()).decode(), secret.encode().hex(),
                base64.b64encode(secret.encode("utf-16le")).decode(),
                "".join(f"\\u{ord(char):04x}" for char in secret))
    for variant in variants:
        assert variant not in redactor.text("stack: " + variant)
        stream = redactor.streaming()
        visible = stream.feed(variant[:5]) + stream.feed(variant[5:]) + stream.finish()
        assert variant not in "".join(visible)
    payload = {"environment": {"HOME": "private-home"}, "reasoning": "private-thought",
               "oracle_source": "private-oracle", "dpapi_blob": "private-blob",
               "nested": [{"full_repository": "all files"}, {"safe": "visible"},
                          {"FORGEPILOT_API_KEY": "inherited-key", "chainOfThought": "hidden"},
                          {"reasoning_content": "model-internal", "repository_source": "all source",
                           "dpapi_material": "decrypted-material"}]}
    safe = redactor.data(payload)
    assert all(safe[field] == "[REDACTED]" for field in
               ("environment", "reasoning", "oracle_source", "dpapi_blob"))
    assert safe["nested"] == [{"full_repository": "[REDACTED]"}, {"safe": "visible"},
                              {"FORGEPILOT_API_KEY": "[REDACTED]", "chainOfThought": "[REDACTED]"},
                              {"reasoning_content": "[REDACTED]", "repository_source": "[REDACTED]",
                               "dpapi_material": "[REDACTED]"}]
