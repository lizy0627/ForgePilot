from functools import partial

from pydantic import Field

from .registry import FunctionTool
from .repository_tools import Arguments, PathArgument


class SymbolArguments(Arguments):
    path: PathArgument
    line: int = Field(ge=1, le=1_000_000)
    column: int = Field(ge=1, le=262_144)
    max_results: int = Field(default=50, ge=1, le=100)


class ReferenceArguments(SymbolArguments):
    include_declaration: bool = False


def register_symbol_tools(registry, intelligence):
    position = " Input path is repository-relative; line/column start at 1, columns count Unicode code points. Place cursor inside an identifier."
    fallback = " Check source/semantic/limits: rg fallback is text candidates, NOT proven definitions/references. No configure/build is performed."
    registry.register(FunctionTool("find_symbol",
        "Resolve the identifier at a C/C++ source position to clangd definition targets and source context." + position + fallback,
        SymbolArguments, partial(intelligence.query, "find_symbol")))
    registry.register(FunctionTool("find_references",
        "Find clangd semantic references to the identifier at a C/C++ source position, within bounded indexed coverage." + position + fallback,
        ReferenceArguments, partial(intelligence.query, "find_references")))
