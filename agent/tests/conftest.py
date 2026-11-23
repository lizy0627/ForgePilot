from copy import deepcopy
from pathlib import Path
import shutil
import subprocess

import httpx
import pytest

from forgepilot.llm import ChatReply
from forgepilot.process import executable


@pytest.fixture
def qt_draft():
    """Handwritten review fixture for a Qt model/view that still displays the old value."""
    from forgepilot_eval.reviewed_qt_skill import load_qt_skill

    return load_qt_skill().model_copy(update={
        "developer_note": "私有手记：此判断来自开发者观察，尚未验证修复。",
    })


def drop_skill_schema(db):
    """Rebuild pre-v12 test databases after seeding their legacy records."""
    for table in ("skill_generation_attempts", "mission_skill_snapshots", "skill_drafts", "skill_versions", "skills"):
        db.execute(f'DROP TABLE "{table}"')


@pytest.fixture(autouse=True)
def isolated_history_database(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEPILOT_DATABASE",str(tmp_path / "test-history.sqlite3"))


@pytest.fixture(autouse=True)
def no_real_model_transport(monkeypatch):
    """Fail closed for provider HTTP paths; fake transports bypass this boundary.

    Other HTTP paths remain available to local API/MCP integration fixtures.
    Subprocess CLI tests explicitly start their own loopback fake model server.
    """
    async_send = httpx.AsyncHTTPTransport.handle_async_request
    sync_send = httpx.HTTPTransport.handle_request

    def check(request):
        path = request.url.path.rstrip("/")
        if path.endswith("/chat/completions") or "/embeddings" in path:
            pytest.fail("Real model HTTP transport is disabled in tests; inject a fake provider or MockTransport")

    async def guarded_async(self, request):
        check(request)
        return await async_send(self, request)

    def guarded_sync(self, request):
        check(request)
        return sync_send(self, request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", guarded_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", guarded_sync)


@pytest.fixture(scope="session")
def git_exe():
    return executable(None, "git")


@pytest.fixture(scope="session")
def rg_exe():
    return executable(None, "rg")


@pytest.fixture
def cpp_repo(tmp_path, git_exe):
    root = tmp_path / "C++ 项目 with spaces"
    shutil.copytree(Path(__file__).parent / "fixtures/cpp_counter", root)
    subprocess.run([git_exe, "init", "-q", str(root)], check=True, capture_output=True)
    # Preserve all three kinds of user data, including a repository with no HEAD.
    subprocess.run([git_exe, "-C", str(root), "add", "main.cpp"], check=True, capture_output=True)
    with (root / "main.cpp").open("a", encoding="utf-8") as stream:
        stream.write("\n// existing unstaged user change\n")
    (root / "notes.txt").write_text("existing untracked user file", encoding="utf-8")
    return root


def reply(content="Analysis complete", calls=None, usage=None):
    return ChatReply.model_validate({"message": {"role": "assistant", "content": content,
                                                "tool_calls": calls}, "usage": usage})


def call(name="read_file", arguments='{"path":"Counter.cpp"}', id="call_1"):
    return {"id": id, "type": "function", "function": {"name": name, "arguments": arguments}}


class ScriptedLLM:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    async def chat(self, messages, tools=None):
        self.requests.append(deepcopy(messages))
        item = next(self.replies)
        if isinstance(item, Exception):
            raise item
        return item
