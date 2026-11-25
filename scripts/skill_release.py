"""Host-side data collection and acceptance for the Windows skill distribution."""
from __future__ import annotations

from contextlib import closing
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
from forgepilot.builtin_skills import builtin_resource_directory, read_catalog  # noqa: E402


def catalog_files(directory):
    files = [Path("catalog.json")]
    for package in read_catalog(directory):
        files.extend(Path(package.slug) / name for name in ("SKILL.md", "manifest.json"))
    return files


def stage(destination, *, source=None):
    source = source if source is not None else builtin_resource_directory()
    destination.mkdir(parents=True, exist_ok=False)
    for relative in catalog_files(source):
        target = destination / relative
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(source / relative, target)
    if set(catalog_files(destination)) != {
            path.relative_to(destination) for path in destination.rglob("*") if path.is_file()}:
        raise ValueError("Skill catalog changed during staging")


def check_resources(directory):
    source = builtin_resource_directory()
    expected = set(catalog_files(source))
    actual = {p.relative_to(directory) for p in directory.rglob("*") if p.is_file()}
    if actual != expected:
        raise ValueError("Extracted skill files differ from the reviewed allowlist")
    for relative in expected:
        if (source / relative).read_bytes() != (directory / relative).read_bytes():
            raise ValueError(f"Extracted skill resource differs: {relative}")
    print(f"Reviewed resource files: {len(expected)}; "
          f"catalog SHA-256: {hashlib.sha256((directory / 'catalog.json').read_bytes()).hexdigest()}")


def server_inventory(command, profile, *, agent_path=None):
    """Start the actual server against a fresh default database in this profile."""
    from forgepilot.artifacts.canonical import content_sha256
    from forgepilot.skill_models import SkillContent

    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("FORGEPILOT_", "PYTHON"))}
    environment.update(USERPROFILE=str(profile), LOCALAPPDATA=str(profile / "local"),
                       APPDATA=str(profile / "roaming"), PYTHONNOUSERSITE="1",
                       FORGEPILOT_API_KEY="offline-skill-verification",
                       HTTP_PROXY="http://127.0.0.1:9", HTTPS_PROXY="http://127.0.0.1:9",
                       NO_PROXY="127.0.0.1,localhost,::1")
    if agent_path is not None:
        environment["PYTHONPATH"] = str(agent_path)
    profile.mkdir()
    database = profile / "local/ForgePilot/tasks.sqlite3"
    database.parent.mkdir(parents=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    args = [*command, "--host", "127.0.0.1", "--port", str(port), "--disable-mcp",
            "--base-url", "http://127.0.0.1:9/v1", "--model", "offline-release"]
    process = subprocess.Popen(args, cwd=profile, env=environment, stdin=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"Skill verification server exited before startup ({process.returncode})")
            try:
                with urlopen(f"http://127.0.0.1:{port}/openapi.json", timeout=1) as response:
                    if "/api/v2/missions" in json.load(response)["paths"]:
                        break
            except (URLError, TimeoutError):
                pass
            time.sleep(0.1)
        else:
            raise TimeoutError("Skill verification server failed to become ready")
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            if db.execute("SELECT count(*) FROM skill_drafts").fetchone()[0]:
                raise ValueError("Private drafts found in clean release profile")
            rows = db.execute("""SELECT s.source,s.skill_id,v.version,v.content_sha256,v.content_json
                                 FROM skills s JOIN skill_versions v ON s.skill_id=v.skill_id
                                 AND s.current_version=v.version ORDER BY s.skill_id""")
            inventory = []
            for source, skill_id, version, digest, raw_content in rows:
                if source != "builtin" or content_sha256(SkillContent.model_validate_json(raw_content)) != digest:
                    raise ValueError("Invalid skill source or content hash in release profile")
                inventory.append((skill_id, version, digest))
            return inventory
    finally:
        process.terminate()
        process.wait(timeout=10)


def add_fixture(directory):
    from forgepilot.skill_package import export_skill_package
    from forgepilot.sqlite_store import SqliteEventStore
    sys.path.insert(0, str(ROOT / "eval"))
    from forgepilot_eval.reviewed_qt_skill import publish_qt_skill

    # Exercise the reviewed-version export. Only the two public package files
    # and its temporary catalog entry are placed in the extracted resource tree.
    store = SqliteEventStore()
    try:
        reviewed = publish_qt_skill(store.skills)
        package = export_skill_package(store.skills, reviewed.skill_id, reviewed.version,
                                       directory, slug="release-test-qt", release_version=1)
    finally:
        store.close()
    catalog = directory / "catalog.json"
    data = json.loads(catalog.read_bytes())
    data["packages"].append({"slug": package.slug, "version": package.version,
                             "content_sha256": package.content_sha256})
    catalog.write_text(json.dumps(data), encoding="utf-8")


def verify(extracted):
    frozen_skills = extracted / "bin/_internal/resources/skills"
    check_resources(frozen_skills)
    with tempfile.TemporaryDirectory(prefix="fps-") as temporary:
        work = Path(temporary).resolve()
        dev = work / "dev"
        dev_agent = dev / "agent"
        shutil.copytree(ROOT / "agent/forgepilot", dev_agent / "forgepilot",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        dev_skills = dev / "resources/skills"
        stage(dev_skills)
        frozen_bin = extracted / "bin"
        for scenario in ("official", "fixture"):
            if scenario == "fixture":
                add_fixture(dev_skills)
                # Stage the fixture with the same allowlist used for PyInstaller.
                # Probe a disposable copy; the extracted distribution stays pristine.
                frozen_bin = work / "fixture-bin"
                shutil.copytree(extracted / "bin", frozen_bin,
                                ignore=lambda path, names: {"skills"} if Path(path) == frozen_skills.parent else set())
                stage(frozen_bin / "_internal/resources/skills", source=dev_skills)
            expected = sorted(("builtin:" + p.slug, p.version, p.content_sha256)
                              for p in read_catalog(dev_skills))
            programs = (
                ("development", [sys.executable, "-m", "forgepilot.server"], dev_agent),
                ("frozen", [str(frozen_bin / "ForgePilotAgent.exe"), "server"], None),
            )
            for name, command, agent_path in programs:
                actual = server_inventory(command, work / f"{scenario}-{name}", agent_path=agent_path)
                if actual != expected:
                    raise ValueError(f"{scenario} {name} imported inventory differs from the catalog")
                print(f"{scenario} {name}: {json.dumps(actual)}; private drafts=0")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("stage", "verify"))
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.command == "stage":
        stage(args.directory.resolve())
    else:
        verify(args.directory.resolve())
