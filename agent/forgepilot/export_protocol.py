"""Regenerate committed wire schemas without credentials, runtime startup or network."""

import json
from pathlib import Path

from .api import create_app
from .api_models import EventPayload


def export(directory=None):
    directory = Path(directory) if directory else Path(__file__).resolve().parents[2] / "protocol"
    directory.mkdir(parents=True, exist_ok=True)
    app = create_app(lambda trace: None)  # Schema generation never starts the service.
    try:
        for name, content in {"openapi.json": app.openapi(), "events.schema.json": EventPayload.model_json_schema()}.items():
            (directory / name).write_text(json.dumps(content, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    finally:
        app.state.task_host.store.close()


if __name__ == "__main__":
    export()
