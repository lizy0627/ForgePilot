"""Verify the release catalog and install its immutable versions on database open."""
from contextlib import AbstractContextManager
from pathlib import Path
import sqlite3
import sys
from typing import Protocol

from .artifacts.canonical import canonical_json_bytes
from .errors import ForgePilotError
from .recording import now
from .skill_package import SkillPackage, read_package_json, read_skill_package, validate_slug


class _BuiltinSkillStore(Protocol):
    def transaction(self) -> AbstractContextManager[sqlite3.Connection]: ...


def builtin_resource_directory() -> Path:
    root = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]
    return root / "resources" / "skills"


def read_catalog(directory: Path) -> tuple[SkillPackage, ...]:
    """Only explicitly listed, pinned packages may enter a product release."""
    try:
        if directory.is_symlink() or directory.is_junction():
            raise ValueError
        catalog = read_package_json(directory / "catalog.json")
        if (set(catalog) != {"schema_version", "packages"} or type(catalog["schema_version"]) is not int
                or catalog["schema_version"] != 1 or not isinstance(catalog["packages"], list)):
            raise ValueError
        packages = []
        slugs = set()
        for entry in catalog["packages"]:
            if not isinstance(entry, dict) or set(entry) != {"slug", "version", "content_sha256"}:
                raise ValueError
            slug = validate_slug(entry["slug"])
            if slug in slugs or type(entry["version"]) is not int:
                raise ValueError
            package = read_skill_package(directory / slug)
            if (package.slug, package.version, package.content_sha256) != (
                    slug, entry["version"], entry["content_sha256"]):
                raise ValueError
            slugs.add(slug)
            packages.append(package)
        return tuple(packages)
    except (OSError, ValueError, TypeError, RecursionError):
        raise ForgePilotError("invalid_skill_catalog", "Cannot load the verified built-in skill catalog") from None


def import_builtin_skills(store: _BuiltinSkillStore) -> None:
    packages = read_catalog(builtin_resource_directory())
    with store.transaction() as db:
        installed = {row["skill_id"]: row for row in db.execute(
            "SELECT skill_id,current_version,builtin_withdrawn FROM skills WHERE source='builtin'")}
        for package in packages:
            skill_id = "builtin:" + package.slug
            prior = installed.pop(skill_id, None)
            encoded = canonical_json_bytes(package.content).decode("utf-8")
            if prior is None:
                db.execute("INSERT INTO skills(skill_id,source,current_version) VALUES(?,'builtin',?)",
                           (skill_id, package.version))
            elif package.version < prior["current_version"]:
                raise ForgePilotError("skill_package_downgrade", "Built-in skill version is older than installed")
            elif package.version == prior["current_version"]:
                row = db.execute("""SELECT name,content_json,content_sha256 FROM skill_versions
                                    WHERE skill_id=? AND version=?""", (skill_id, package.version)).fetchone()
                if not row or tuple(row) != (package.name, encoded, package.content_sha256):
                    raise ForgePilotError("skill_package_conflict", "Built-in version already has different content")
                if prior["builtin_withdrawn"]:
                    db.execute("UPDATE skills SET enabled=1,builtin_withdrawn=0,revision=revision+1 WHERE skill_id=?",
                               (skill_id,))
                continue
            else:
                # Restore a catalog withdrawal while respecting a developer's explicit disable.
                db.execute("""UPDATE skills SET current_version=?,enabled=max(enabled,builtin_withdrawn),
                              builtin_withdrawn=0,revision=revision+1 WHERE skill_id=?""",
                           (package.version, skill_id))
            db.execute("""INSERT INTO skill_versions(skill_id,version,name,content_json,content_sha256,created_at)
                          VALUES(?,?,?,?,?,?)""",
                       (skill_id, package.version, package.name, encoded, package.content_sha256, now()))
        db.executemany("""UPDATE skills SET enabled=0,builtin_withdrawn=1,revision=revision+1
                          WHERE skill_id=? AND enabled=1""", ((skill_id,) for skill_id in installed))
