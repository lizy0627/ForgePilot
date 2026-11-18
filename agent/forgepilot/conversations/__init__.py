"""Public conversations boundary for composition and cross-module callers.

Exports are lazy so SQLite and v1 background rules do not load model/tool
implementations. Importing this package does not open resources.
"""
from importlib import import_module

_EXPORTS = {
    "ConversationHost": "application",
    "ChatService": "service",
    "ReadOnlyRegistry": "service",
    "ChatToolRoute": "service",
    "chat_tool_route": "service",
    "web_mcp_snapshots": "service",
    "READ_TOOLS": "service",
    "ChatAnswerEvent": "events",
    "AnswerEventCallback": "events",
    "ConversationStreamBroker": "stream",
    "ConversationStreamEvent": "stream",
    "ConversationStreamSubscription": "stream",
    "ReplayBatch": "stream",
    # SQLite integration extensions share the owner's connection and transaction.
    "ConversationStore": "store",
    "MemoryStore": "memory_store",
    "MEMORY_RULES": "context",
    "SUMMARY_SYSTEM": "context",
    "with_background": "context",
    "project_identity": "context",
}

__all__ = list(_EXPORTS)


def __getattr__(name):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value
