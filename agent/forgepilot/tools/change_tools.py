import json

from pydantic import Field, field_validator, model_validator

from .registry import FunctionTool
from .repository_tools import Arguments, PathArgument

WRITE_TOOLS = frozenset({"write_file", "edit_file"})


class WriteArguments(Arguments):
    path: PathArgument
    content: str = Field(max_length=12_000)


class TextEdit(Arguments):
    old_text: str = Field(min_length=1, max_length=6_000)
    new_text: str = Field(max_length=6_000)

    @field_validator("old_text", "new_text")
    @classmethod
    def valid_text(cls, value):
        if "\x00" in value or "\r" in value.replace("\r\n", "\n"):
            raise ValueError("NUL and standalone CR are not supported")
        return value  # Preserve indentation and raw lengths for the aggregate limit.

    @model_validator(mode="after")
    def changed(self):
        if self.old_text.replace("\r\n", "\n") == self.new_text.replace("\r\n", "\n"):
            raise ValueError("Edit must change text after newline normalization")
        return self


class EditArguments(Arguments):
    path: PathArgument
    edits: list[TextEdit] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def bounded_content(self):
        if sum(len(edit.old_text) + len(edit.new_text) for edit in self.edits) > 12_000:
            raise ValueError("Total edit text exceeds 12000 characters")
        return self


def register_change_tools(registry, git, changes, *, on_changed=None):
    async def diff():
        return json.dumps(changes.diff(), ensure_ascii=False)
    async def write_file(path, content):
        output = await changes.write_file(path, content)
        if on_changed is not None:
            try:
                on_changed(json.loads(output)["path"])
            except Exception:
                pass
        return output
    async def edit_file(path, edits):
        output = await changes.edit_file(path, edits)
        if on_changed is not None:
            try:
                on_changed(json.loads(output)["path"])
            except Exception:
                pass
        return output
    registry.register(FunctionTool("git_status", "Read current Git status, including pre-existing user changes; excludes protected paths.", Arguments, git.status))
    registry.register(FunctionTool("git_diff", "Show only this task's Agent changes relative to original byte snapshots, plus restore/conflict status.", Arguments, diff))
    registry.register(FunctionTool("write_file", "Write complete UTF-8 text to an approved file during modifying. Read existing files with read_file before overwriting; retrieved RAG context is not a current-file read. After a first-write conflict, read again and regenerate the change. New scope requires another human-approved plan. Never delete or commit.", WriteArguments, write_file))
    registry.register(FunctionTool("edit_file", "Prefer for local changes to an existing approved UTF-8 file after read_file; retrieved RAG context is not a current-file read. Supply 1-8 unique exact old_text/new_text pairs; all match the pre-call content and ranges must not overlap. Preserve indentation; LF snippets also match CRLF files. After a first-write conflict, read again and regenerate edits. Never creates files; new scope requires human approval.", EditArguments, edit_file))
