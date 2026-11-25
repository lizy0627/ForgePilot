"""Copy a consistent SQLite snapshot into a new directory without running migrations."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile


def copy_database(source: Path, destination: Path) -> Path:
    source = source.resolve(strict=True)
    destination.mkdir()  # Never replace an existing database or pair it with stale WAL files.
    result = destination / "history.sqlite3"
    with tempfile.TemporaryDirectory(dir=destination) as staging:
        snapshot = Path(staging) / result.name
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(snapshot)) as copied:
                original.backup(copied)
                if copied.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("Database copy failed integrity check")
        snapshot.rename(result)
    return result
