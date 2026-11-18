"""Load the repository's x64 build environment for offline validation children."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _batch_environment(repo: Path, batch: Path, args: str, env: dict[str, str]) -> dict[str, str]:
    # Each batch runs in its own cmd process. Carry the result in memory only:
    # the inherited environment can contain credentials and must never be logged.
    cmd = Path(env["SYSTEMROOT"]) / "System32/cmd.exe"
    command = (
        f'call "%FP_VALIDATION_BATCH%" {args} >nul && '
        '"%FP_VALIDATION_PYTHON%" -I -c "import json,os; print(json.dumps(dict(os.environ)))"'
    )
    child = subprocess.run(
        f'"{cmd}" /d /v:off /c {command}',
        executable=str(cmd), cwd=repo,
        env={**env, "FP_VALIDATION_BATCH": str(batch), "FP_VALIDATION_PYTHON": sys.executable},
        stdout=subprocess.PIPE, check=False,
    )
    if child.returncode:
        raise RuntimeError(f"{batch} {args} failed with exit code {child.returncode}")
    captured = {key.upper(): value for key, value in json.loads(child.stdout).items()}
    del captured["FP_VALIDATION_BATCH"], captured["FP_VALIDATION_PYTHON"]
    return captured


def _file_versions(paths: dict[str, Path], env: dict[str, str]) -> dict[str, str]:
    # Read the same Windows version resources for MSVC and SDK tools, regardless
    # of their console language and without running rc/mt on dummy inputs.
    powershell = Path(env["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    command = """
        $ErrorActionPreference = 'Stop'
        [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
        $paths = ConvertFrom-Json $env:FP_VALIDATION_TOOL_PATHS
        $versions = @{}
        foreach ($tool in $paths.PSObject.Properties) {
            $versions[$tool.Name] = [Diagnostics.FileVersionInfo]::GetVersionInfo($tool.Value).FileVersion
        }
        ConvertTo-Json -Compress $versions
    """
    encoded_paths = json.dumps({name: str(path) for name, path in paths.items()})
    result = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-Command", command],
        env={**env, "FP_VALIDATION_TOOL_PATHS": encoded_paths},
        stdout=subprocess.PIPE, check=True,
    )
    return json.loads(result.stdout)


def load_toolchain(repo: Path) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """Return a child environment plus path, version, and SHA-256 for each tool."""
    if os.name != "nt":
        raise RuntimeError("The offline isolation tests require Windows and x64 Visual Studio")
    repo = repo.resolve(strict=True)
    base = dict(os.environ)
    base["VSCMD_SKIP_SENDTELEMETRY"] = "1"
    configured = _batch_environment(repo, repo / "scripts/Check-Environment.cmd", "runtime", base)
    for key in ("FORGEPILOT_VS_ROOT", "FORGEPILOT_CMAKE"):
        if not configured.get(key) or not Path(configured[key]).is_absolute():
            raise RuntimeError(f"Configure an absolute {key} in scripts/local-env.cmd or the environment")
    vs = Path(configured["FORGEPILOT_VS_ROOT"]).resolve(strict=True)
    cmake = Path(configured["FORGEPILOT_CMAKE"]).resolve(strict=True)
    if cmake.name.lower() != "cmake.exe":
        raise RuntimeError(f"Expected a cmake.exe installation, got {cmake}")
    devcmd = vs / "Common7/Tools/VsDevCmd.bat"
    env = _batch_environment(repo, devcmd, "-arch=x64 -host_arch=x64", configured)
    if Path(env.get("VSINSTALLDIR", "")).resolve() != vs or env.get("VSCMD_ARG_TGT_ARCH") != "x64":
        raise RuntimeError(f"{devcmd} did not select the configured x64 environment")
    env["PATH"] = str(cmake.parent) + os.pathsep + env["PATH"]
    paths = {"cmake": cmake, "ctest": cmake.with_name("ctest.exe").resolve(strict=True)}
    for name in ("cl", "nmake", "rc", "mt"):
        found = shutil.which(name + ".exe", path=env["PATH"])
        if not found:
            raise RuntimeError(f"{name}.exe is missing from the selected VS/SDK PATH")
        paths[name] = Path(found).resolve(strict=True)
    versions = _file_versions(paths, env)
    tools = {}
    for name, path in paths.items():
        if not versions.get(name):
            raise RuntimeError(f"Missing version resource in {path}")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        tools[name] = {"path": str(path), "version": versions[name], "sha256": digest}
    return env, tools
