"""Position-based C++ inspection with bounded clangd sessions and honest rg fallback."""
import asyncio
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit
from urllib.request import url2pathname

from .compile_database import CPP_SUFFIXES, locate_database
from .errors import ForgePilotError
from .lsp import LspClient
from .process import executable

MAX_SYNC_FILES = 32
MAX_SYNC_BYTES = 2_097_152
MAX_OUTPUT = 12_000
COORDINATES = "1-based lines and Unicode-code-point columns; end is exclusive"


@dataclass(frozen=True)
class CodeIntelligenceConfig:
    clangd: str | None = None
    compile_commands_dir: str | None = None
    timeout: float = 18.0
    request_timeout: float = 8.0

    def __post_init__(self):
        if any(isinstance(v, bool) or not math.isfinite(v) or not 0 < v <= 20
               for v in (self.timeout, self.request_timeout)):
            raise ForgePilotError("clangd_config", "clangd timeouts must be finite and within (0, 20] seconds")


def lines(text):
    # LSP line separators are LF/CRLF, not every Unicode separator accepted by str.splitlines().
    return [line.removesuffix("\r") for line in text.split("\n")]


def position(text, line, column):
    rows = lines(text)
    if not 1 <= line <= len(rows) or not 1 <= column <= len(rows[line - 1]) + 1:
        raise ForgePilotError("symbol_position", "Position is outside the UTF-8 source file")
    return {"line": line - 1, "character": len(rows[line - 1][:column - 1].encode("utf-16-le")) // 2}


def public_position(text, value):
    if not isinstance(value, dict) or type(value.get("line")) is not int or type(value.get("character")) is not int:
        raise ForgePilotError("clangd_protocol", "Invalid LSP position")
    row, units = value["line"], value["character"]
    rows = lines(text)
    if not 0 <= row < len(rows) or units < 0:
        raise ForgePilotError("clangd_protocol", "LSP position is outside the source snapshot")
    units = min(units, len(rows[row].encode("utf-16-le")) // 2)  # LSP clamps offsets beyond EOL.
    try:
        column = len(rows[row].encode("utf-16-le")[:units * 2].decode("utf-16-le")) + 1
    except UnicodeError:
        raise ForgePilotError("clangd_protocol", "LSP position splits a UTF-16 surrogate pair") from None
    return row + 1, column


def identifier(text, line, column):
    row = lines(text)[line - 1]
    for match in re.finditer(r"[^\W\d]\w*", row, re.UNICODE):
        if match.start() <= column - 1 < match.end():
            if len(match.group()) > 256:
                break
            return match.group()
    raise ForgePilotError("symbol_position", "Place the cursor inside a C/C++ identifier")


class CodeIntelligence:
    def __init__(self, repository, config=None, *, selected_directory=lambda: None, client_factory=LspClient,
                 on_status=None):
        self.repository = repository
        self.config = config or CodeIntelligenceConfig()
        self.selected_directory = selected_directory
        self.client_factory = client_factory
        self.on_status = on_status

    def availability(self):
        """Inspect local metadata/tools without starting clangd or a build."""
        try:
            locate_database(self.repository, configured=self.config.compile_commands_dir,
                            selected=self.selected_directory())
            try:
                executable(self.config.clangd, "clangd")
            except ForgePilotError:
                raise ForgePilotError("clangd_unavailable", "clangd executable is unavailable") from None
        except (ForgePilotError, OSError) as exc:
            reason = exc.code if isinstance(exc, ForgePilotError) else "clangd_unavailable"
            return {"enabled": True, "used": False, "fallback_reasons": [reason]}
        return {"enabled": True, "used": False, "fallback_reasons": []}

    async def query(self, operation, *, path, line, column, max_results=50, include_declaration=False):
        file = self.repository._path(path)
        if file.suffix.casefold() not in CPP_SUFFIXES:
            raise ForgePilotError("symbol_file", "Choose a C/C++ source or header file")
        text, truncated = self.repository._read(path)
        if truncated:
            raise ForgePilotError("symbol_file_limit", "Symbol position requires an untruncated source file")
        cursor = position(text, line, column)
        token = identifier(text, line, column)
        path = file.relative_to(self.repository.root).as_posix()
        common = {"operation": operation, "symbol": token, "position": {"path": path, "line": line, "column": column},
                  "coordinates": COORDINATES}
        try:
            async with asyncio.timeout(self.config.timeout):
                result = await self._semantic(operation, file, text, cursor, max_results, include_declaration)
            result.update(common)
        except asyncio.CancelledError:
            raise  # Explicit cancellation never becomes a successful text fallback.
        except (ForgePilotError, TimeoutError, OSError) as exc:
            reason = exc.code if isinstance(exc, ForgePilotError) else "clangd_timeout" if isinstance(exc, TimeoutError) else "clangd_unavailable"
            result = await self._fallback(token, max_results, reason)
            result.update(common)
        while len(json.dumps(result, ensure_ascii=False)) > MAX_OUTPUT:
            # Preserve valid structured output rather than relying on Registry's final string slice.
            values = result.get("synchronized_files") or result.get("locations") or result.get("text_matches")
            if not values:
                raise ForgePilotError("symbol_output_limit", "Symbol metadata exceeds the output budget")
            values.pop()
            result["truncated"] = True
            if "output_limit" not in result["limits"]:
                result["limits"].append("output_limit")
        if self.on_status is not None:
            self.on_status({"enabled": True, "used": result["semantic"],
                            "fallback_reasons": [result["fallback_reason"]] if result.get("fallback_reason") else []})
        return json.dumps(result, ensure_ascii=False)

    async def _semantic(self, operation, file, text, cursor, max_results, include_declaration):
        database = locate_database(self.repository, configured=self.config.compile_commands_dir,
                                   selected=self.selected_directory())
        try:
            clangd = executable(self.config.clangd, "clangd")
        except ForgePilotError:
            raise ForgePilotError("clangd_unavailable", "clangd executable is unavailable") from None
        snapshots = {file.relative_to(self.repository.root).as_posix(): text}
        size = len(text.encode("utf-8"))
        omitted = database.omitted
        for source in database.files:
            if source in snapshots:
                continue
            if len(snapshots) >= MAX_SYNC_FILES:
                omitted += 1
                continue
            try:
                content, truncated = self.repository._read(source)
                cost = len(content.encode("utf-8"))
                if truncated or size + cost > MAX_SYNC_BYTES:
                    omitted += 1
                    continue
                snapshots[source] = content
                size += cost
            except ForgePilotError:
                omitted += 1
            await asyncio.sleep(0)
        with tempfile.TemporaryDirectory(prefix="forgepilot-clangd-") as temporary:
            # Pass a verified copy, not a mutable repository command/config file, to clangd.
            Path(temporary, "compile_commands.json").write_text(json.dumps(database.entries, ensure_ascii=False), encoding="utf-8")
            command = [clangd, "--compile-commands-dir=" + temporary, "--background-index=false", "--enable-config=false",
                       "--pch-storage=memory", "--log=error", "-j=2", "--limit-references=101", "--limit-results=101"]
            client = self.client_factory(command, self.repository.root, request_timeout=self.config.request_timeout)
            try:
                await client.start()
                for source, content in snapshots.items():
                    await client.sync(self.repository._path(source), content)
                # Re-read after indexing so external edits cannot turn stale locations into current evidence.
                for source, content in snapshots.items():
                    current, truncated = self.repository._read(source)
                    if truncated or current != content:
                        raise ForgePilotError("clangd_source_changed", "Source changed while indexing")
                params = {"textDocument": {"uri": file.as_uri()}, "position": cursor}
                if operation == "find_references":
                    params["context"] = {"includeDeclaration": include_declaration}
                response = await client.request("textDocument/definition" if operation == "find_symbol" else "textDocument/references", params)
                if any(client.diagnostics.values()):
                    raise ForgePilotError("clangd_diagnostics", "Parse errors prevent reliable semantic inspection")
                result = self._locations(response, snapshots, max_results)
                database_label = (database.path.relative_to(self.repository.root).as_posix()
                                  if database.path.is_relative_to(self.repository.root)
                                  else "<approved-task-build>/compile_commands.json")
                result.update(source="clangd", semantic=True, compilation_database=database_label,
                    generator=database.generator, synchronized_files=list(snapshots), synchronized_file_count=len(snapshots), omitted_translation_units=omitted,
                    diagnostics_have_errors=any(client.diagnostics.values()), complete=False,
                    limitations=["Bounded dynamic index of synchronized translation units and included headers; not exhaustive project/template references.",
                        "clangd may return a declaration when no definition is indexed; inspect the target context."])
                if omitted:
                    result["limits"].append("translation_unit_coverage")
                    result["truncated"] = True
                return result
            finally:
                await client.close()

    def _locations(self, response, snapshots, maximum):
        response = [] if response is None else [response] if isinstance(response, dict) else response
        if not isinstance(response, list):
            raise ForgePilotError("clangd_protocol", "Invalid location response")
        results, seen, limits = [], set(), set()
        size = 0
        filtered = 0
        for location in response[:102]:
            if not isinstance(location, dict):
                raise ForgePilotError("clangd_protocol", "Invalid LSP location")
            uri = location.get("uri", location.get("targetUri"))
            span = location.get("range", location.get("targetSelectionRange"))
            if not isinstance(uri, str) or not isinstance(span, dict):
                raise ForgePilotError("clangd_protocol", "Invalid LSP location fields")
            try:
                parsed = urlsplit(uri)
                if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
                    raise ValueError()
                absolute = Path(url2pathname(parsed.path))
                relative = absolute.relative_to(self.repository.root).as_posix()
                self.repository._path(relative)
                content, truncated = self.repository._read(relative)
                if truncated:
                    raise ValueError()
            except (ValueError, ForgePilotError, OSError):
                filtered += 1
                limits.add("excluded_or_unreadable_location")
                continue
            if relative in snapshots and content != snapshots[relative]:
                raise ForgePilotError("clangd_source_changed", "Result source changed during the request")
            line, column = public_position(content, span.get("start"))
            end_line, end_column = public_position(content, span.get("end"))
            if (end_line, end_column) < (line, column):
                raise ForgePilotError("clangd_protocol", "Invalid LSP range order")
            key = (relative, line, column, end_line, end_column)
            if key in seen:
                continue
            seen.add(key)
            context = lines(content)[line - 1]
            item = {"path": relative, "line": line, "column": column, "end_line": end_line, "end_column": end_column,
                    "context": context[:320], "context_truncated": len(context) > 320}
            cost = len(json.dumps(item, ensure_ascii=False))
            if len(results) >= maximum or size + cost > 8000:
                limits.add("result_or_output_limit")
                break
            results.append(item)
            size += cost
        if len(response) >= 101:
            limits.add("server_result_limit")
        return {"locations": results, "filtered_locations": filtered, "truncated": bool(limits), "limits": sorted(limits)}

    async def _fallback(self, token, maximum, reason):
        search = json.loads(await self.repository.search_code(token, max_results=maximum, file_suffixes=CPP_SUFFIXES))
        candidates = []
        for row in search["matches"]:
            index = row["text"].find(token)
            candidates.append({"path": row["path"], "line": row["line"], "column": index + 1,
                "context": row["text"][:240], "context_truncated": len(row["text"]) > 240})
        while len(json.dumps(candidates, ensure_ascii=False)) > 8000:
            candidates.pop()
            search["truncated"] = True
            if "output_limit" not in search["limits"]:
                search["limits"].append("output_limit")
        return {"source": "rg", "semantic": False, "fallback_reason": reason, "locations": [],
            "text_matches": candidates, "complete": False, "truncated": search["truncated"], "limits": search["limits"],
            "limitations": ["Literal text candidates only; these are NOT reliable definitions, references or call relationships.",
                "Includes comments/strings and same-spelled symbols; repository search limits and exclusions apply."]}
