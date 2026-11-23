"""Single source of truth for SQLite schema version ownership."""
from enum import IntEnum, unique


@unique
class SchemaVersion(IntEnum):
    TASK_HISTORY = 1
    CONVERSATIONS = 2
    MEMORY = 3
    MCP_SERVERS = 4
    MCP_CALL_RECORDS = 5
    RAG_FILES = 6
    RAG_INDEX_LIFECYCLE = 7
    GRAPH_ARTIFACT_STORE = 8
    GRAPH_ORCHESTRATOR = 9
    GRAPH_CHECKPOINT_RECOVERY = 10
    WRITER_SCOPE_EXPANSION = 11
    LOCAL_SKILL_LIBRARY = 12
    SKILL_GENERATION_ATTEMPTS = 13


CURRENT_SCHEMA_VERSION = max(SchemaVersion)


def requires_upgrade_backup(version: int) -> bool:
    """Return whether an existing supported database will be migrated."""
    return 1 <= version < int(CURRENT_SCHEMA_VERSION)
