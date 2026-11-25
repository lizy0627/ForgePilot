"""Reviewable distribution of an immutable reviewed Mission skill version."""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path, PureWindowsPath
import re
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .artifacts.canonical import content_sha256
from .artifacts.models import Sha256Text
from .security import Redactor
from .skill_models import SkillContent, SkillVersion
from .skill_prose import contains_private_fragment

if TYPE_CHECKING:
    from .skill_store import SkillRepository


# A new model field needs a release-format review before it may leave the machine.
_MATCH_FIELDS = {
    "schema_version", "stages", "triggers", "boundaries", "exclusions", "usage_scope",
    "keyword_groups", "project_tags", "task_types",
}
_MANIFEST_FIELDS = {"package_format", "slug", "version", "content_sha256", "markdown_sha256", "metadata"}
_UUID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", re.I)
_LOG = re.compile(r"Traceback \(most recent call last\)|\[(?:debug|info|warn(?:ing)?|error|fatal)\]|"
                  r"^\s*(?:\d{4}-\d\d-\d\d[T ]\d\d:\d\d|(?:DEBUG|INFO|WARN|ERROR|FATAL):|File \".*\", line \d+)",
                  re.IGNORECASE)
_SECTION = "\n\n---\n\n"


def validate_slug(value: str) -> str:
    if (not isinstance(value, str) or len(value) > 64 or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value)
            or PureWindowsPath(value).is_reserved() or _UUID.search(value)):
        raise ValueError("Invalid public package slug")
    return value


def _check_public_text(value, redactor: Redactor) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _check_public_text(item, redactor)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_public_text(item, redactor)
    elif isinstance(value, str):
        for line in value.replace("\r\n", "\n").split("\n"):
            if line and (not line.isprintable() or contains_private_fragment(line) or _UUID.search(line)
                         or _LOG.search(line) or redactor.text(line) != line):
                raise ValueError("Review public prose: remove private paths, identifiers, code, logs or credentials")


class SkillPackage(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)

    slug: str
    version: int = Field(ge=1, le=2**63 - 2)
    name: str = Field(min_length=1, max_length=128)
    content: SkillContent
    content_sha256: Sha256Text

    @model_validator(mode="after")
    def verify(self):
        validate_slug(self.slug)
        if content_sha256(self.content) != self.content_sha256:
            raise ValueError("Normalized skill content hash mismatch")
        if self.name != self.name.strip() or not self.name.isprintable() or _SECTION in self.content.summary:
            raise ValueError("Expected a single display title and an unambiguous summary")
        redactor = Redactor()
        _check_public_text(self.name, redactor)
        _check_public_text(self.content.model_dump(mode="json"), redactor)
        return self


def _read_bytes(path: Path) -> bytes:
    if path.is_symlink() or path.is_junction() or not path.is_file():
        raise ValueError("Expected a regular skill resource file")
    with path.open("rb") as stream:
        data = stream.read(256 * 1024 + 1)
    if len(data) > 256 * 1024:
        raise ValueError("Skill resource exceeds the format size limit")
    return data


def read_package_json(path: Path) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate skill resource key")
            result[key] = value
        return result

    value = json.loads(_read_bytes(path).decode("utf-8"), object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("Expected a skill resource JSON object")
    return value


def read_skill_package(directory: Path) -> SkillPackage:
    """Verify both file integrity and the canonical execution payload used by Missions."""
    if (directory.is_symlink() or directory.is_junction()
            or {p.name for p in directory.iterdir()} != {"SKILL.md", "manifest.json"}):
        raise ValueError("Expected exactly SKILL.md and manifest.json")
    manifest = read_package_json(directory / "manifest.json")
    if (manifest.keys() != _MANIFEST_FIELDS or type(manifest["package_format"]) is not int
            or manifest["package_format"] != 1 or validate_slug(manifest["slug"]) != directory.name):
        raise ValueError("Invalid skill package manifest or directory identity")
    metadata = manifest["metadata"]
    if not isinstance(metadata, dict) or metadata.keys() - _MATCH_FIELDS:
        raise ValueError("Unknown skill package metadata")
    document = _read_bytes(directory / "SKILL.md")
    if sha256(document).hexdigest() != manifest["markdown_sha256"]:
        raise ValueError("SKILL.md hash mismatch")
    title, separator, guidance = document.decode("utf-8").partition(_SECTION)
    if not separator or not title.startswith("# ") or not guidance.endswith("\n"):
        raise ValueError("Invalid SKILL.md title or guidance section")
    name, _, summary = title[2:].partition("\n\n")
    content = SkillContent.model_validate({**metadata, "body": guidance[:-1], "summary": summary})
    return SkillPackage(slug=manifest["slug"], version=manifest["version"], name=name,
                        content=content, content_sha256=manifest["content_sha256"])


def _export_reviewed(reviewed: SkillVersion, destination: Path, *, slug: str, release_version: int) -> SkillPackage:
    package = SkillPackage(slug=slug, version=release_version, name=reviewed.name,
                           content=reviewed.content, content_sha256=reviewed.content_sha256)
    title = "# " + package.name
    if package.content.summary:
        title += "\n\n" + package.content.summary
    document = (title + _SECTION + package.content.body + "\n").encode("utf-8")
    manifest = {
        "package_format": 1, "slug": package.slug, "version": package.version,
        "content_sha256": package.content_sha256, "markdown_sha256": sha256(document).hexdigest(),
        "metadata": package.content.model_dump(mode="json", include=_MATCH_FIELDS),
    }
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / slug
    if target.exists():
        raise FileExistsError("Export into a new directory for review")
    with TemporaryDirectory(dir=destination) as staging:
        staged = Path(staging) / slug
        staged.mkdir()
        (staged / "SKILL.md").write_bytes(document)
        (staged / "manifest.json").write_bytes((json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode())
        if read_skill_package(staged) != package:
            raise ValueError("Export differs from the reviewed version")
        staged.rename(target)
    return package


def export_skill_package(repository: SkillRepository, skill_id: str, version: int, destination: Path,
                         *, slug: str, release_version: int) -> SkillPackage:
    return _export_reviewed(repository.get_version(skill_id, version), destination,
                            slug=slug, release_version=release_version)


def main() -> None:
    import argparse
    from contextlib import closing
    import sqlite3

    from .skill_store import read_skill_version

    parser = argparse.ArgumentParser(description="Export an immutable reviewed version for public content review")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--skill-id", required=True)
    parser.add_argument("--version", required=True, type=int)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--release-version", required=True, type=int)
    args = parser.parse_args()
    # Opening a normal event store runs migration and recovery. An export only
    # needs one SELECT from an existing database (including its committed WAL).
    with closing(sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        reviewed = read_skill_version(db, args.skill_id, args.version)
    package = _export_reviewed(reviewed, args.output_directory, slug=args.slug, release_version=args.release_version)
    print(f"{package.slug} v{package.version} {package.content_sha256}")


if __name__ == "__main__":
    main()
