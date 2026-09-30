"""Smoke checks for the project environment; no business behavior."""

import asyncio
from pathlib import Path
import sys
import struct

import forgepilot
import httpx
from pydantic import BaseModel
import pytest

from main import main


def test_project_virtual_environment() -> None:
    project = Path(__file__).resolve().parents[1]
    environment = project.parent / "agent.venv"
    assert Path(sys.prefix).resolve() == environment.resolve()
    assert Path(sys.executable).resolve() == (environment / "Scripts" / "python.exe").resolve()
    assert Path(sys.base_prefix).resolve() == Path(r"D:\Python312").resolve()
    assert sys.prefix != sys.base_prefix
    assert sys.version_info[:2] == (3, 12)
    assert struct.calcsize("P") * 8 == 64
    assert forgepilot.__file__ is not None


def test_runtime_entrypoint(capsys: pytest.CaptureFixture[str]) -> None:
    main()
    assert capsys.readouterr().out == "ForgePilot Agent Runtime Ready\n"


@pytest.mark.asyncio
async def test_async_dependencies() -> None:
    class EnvironmentResponse(BaseModel):
        ready: bool

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ready": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as client:
        response = await client.get("https://environment.test/ready")
    await asyncio.sleep(0)
    assert EnvironmentResponse.model_validate(response.json()).ready
