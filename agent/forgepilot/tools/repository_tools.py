from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from ..repository import Repository
from .registry import FunctionTool, ToolRegistry

PathArgument = Annotated[str, Field(min_length=1, max_length=1_024)]


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ListArguments(Arguments):
    path: PathArgument = "."
    limit: int = Field(default=200, ge=1, le=1_000)


class ReadArguments(Arguments):
    path: PathArgument
    start_line: int = Field(default=1, ge=1, le=1_000_000)
    end_line: int = Field(default=200, ge=1, le=1_000_000)


class SearchArguments(Arguments):
    query: str = Field(min_length=1, max_length=256, pattern=r"^[^\r\n\x00]+$")
    path: PathArgument = "."
    max_results: int = Field(default=50, ge=1, le=100)


def register_repository_tools(registry: ToolRegistry, repository: Repository) -> None:
    for name, description, model in (
        ("list_files", "List bounded repository-relative file paths; excludes sensitive/generated files and links.", ListArguments),
        ("read_file", "Read numbered UTF-8 lines within the repository; reports partial/truncated content.", ReadArguments),
        ("search_code", "Case-sensitive literal text search; returns file paths, LF-based line numbers and text. Text matches are not semantic references.", SearchArguments),
    ):
        registry.register(FunctionTool(name, description, model, getattr(repository, name),
                                       read_only=True, concurrent_safe=True))
