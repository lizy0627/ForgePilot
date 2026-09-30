from copy import deepcopy
from pathlib import Path
import shutil
import subprocess

import pytest

from forgepilot.llm import ChatReply
from forgepilot.process import executable


@pytest.fixture(autouse=True)
def isolated_history_database(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEPILOT_DATABASE",str(tmp_path / "test-history.sqlite3"))


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
