"""Regenerate committed wire schemas without credentials, runtime startup or network."""

import json
from pathlib import Path

from pydantic import TypeAdapter

from .api import create_app
from .api_models import ConversationEvent, EventPayload
from .mission_events import MissionEvent
from .protocol_examples import examples


def generated_protocol():
    """Build every committed API artifact without starting runtime services."""
    app = create_app(lambda trace: None)  # Schema generation never starts the service.
    try:
        return {
            "openapi.json": app.openapi(),
            "events.schema.json": EventPayload.model_json_schema(),
            "conversation-events.schema.json": TypeAdapter(ConversationEvent).json_schema(),
            "mission-events.schema.json": MissionEvent.model_json_schema(),
            "examples.json": examples(),
        }
    finally:
        app.state.task_host.store.close()


def export(directory=None):
    directory = Path(directory) if directory else Path(__file__).resolve().parents[2] / "protocol"
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in generated_protocol().items():
        (directory / name).write_text(json.dumps(content, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    export()
