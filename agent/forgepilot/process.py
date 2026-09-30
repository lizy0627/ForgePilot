"""Private subprocess execution and cleanup helpers; not LLM-callable tools."""

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path

from .errors import ForgePilotError


async def wait_for_cleanup(cleanup: asyncio.Task) -> None:
    """Join owned cleanup despite repeated caller cancellation, then propagate it."""
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    # Retrieve errors, including cancellation of cleanup itself, without retrying it.
    await cleanup
    if cancelled:
        raise asyncio.CancelledError()


def executable(configured: str | None, name: str) -> str:
    if configured:
        candidate = Path(configured)
        if not candidate.is_absolute() or not candidate.is_file():
            raise ForgePilotError("tool_missing", f"Configure an absolute existing {name} executable")
        return str(candidate.resolve())
    # Never search the target repository or a relative/current-directory PATH entry.
    filename = name + ".exe" if os.name == "nt" else name
    for directory in os.get_exec_path():
        if Path(directory).is_absolute():
            candidate = Path(directory) / filename
            if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return str(candidate.resolve())
    raise ForgePilotError("tool_missing", f"{name} not found; configure its absolute executable path")


@dataclass
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    truncated: bool


async def run_process(argv: list[str], cwd: Path, *, data: bytes = b"",
                      timeout: float = 10, output_limit: int = 300_000) -> ProcessResult:
    # No inherited API credentials, Git overrides, rg config, startup hooks or pager.
    env = {k: v for k, v in os.environ.items() if k.upper() in
           {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP"}}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    proc = None
    tasks = []
    truncated = False

    async def read(stream):
        nonlocal truncated
        result = bytearray()
        while chunk := await stream.read(16_384):
            remaining = output_limit - len(result)
            result.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
                if proc.returncode is None:
                    proc.kill()
                break
        return bytes(result)

    async def write():
        try:
            proc.stdin.write(data)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.stdin.close()

    try:
        async with asyncio.timeout(timeout):
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            tasks = [asyncio.create_task(read(proc.stdout)), asyncio.create_task(read(proc.stderr)),
                     asyncio.create_task(write())]
            stdout, stderr, _ = await asyncio.gather(*tasks)
            await proc.wait()
            return ProcessResult(proc.returncode, stdout, stderr, truncated)
    except TimeoutError:
        raise ForgePilotError("tool_timeout", "Read-only subprocess timed out") from None
    except OSError:
        raise ForgePilotError("tool_process", "Unable to run configured executable") from None
    finally:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
