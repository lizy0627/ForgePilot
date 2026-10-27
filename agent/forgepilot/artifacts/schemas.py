"""Generate checked-in JSON Schemas and detect drift from Pydantic models."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path

from ..agents.definitions import AgentDefinition
from .models import ARTIFACT_ADAPTER, ENVELOPE_MODELS, SCHEMA_VERSION

SCHEMA_DIRECTORY = Path(__file__).resolve().parents[3] / "resources" / "schemas"


def _decorate(schema: dict, schema_id: str) -> dict:
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": schema_id, **schema}


def generated_schemas() -> dict[str, dict]:
    from ..agents.contracts import contract_schemas

    result = {
        "artifact-envelope.v1.schema.json": _decorate(
            ARTIFACT_ADAPTER.json_schema(), "https://forgepilot.local/schemas/artifact-envelope.v1.schema.json"),
        "agent-definition.v1.schema.json": _decorate(
            AgentDefinition.model_json_schema(), "https://forgepilot.local/schemas/agent-definition.v1.schema.json"),
    }
    for artifact_type, model in ENVELOPE_MODELS.items():
        filename = f"artifact-{artifact_type.lower()}.v1.schema.json"
        result[filename] = _decorate(
            model.model_json_schema(), f"https://forgepilot.local/schemas/{filename}")
    result.update(contract_schemas())
    return result


def _serialized(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def export_schemas(directory: Path = SCHEMA_DIRECTORY) -> tuple[Path, ...]:
    directory.mkdir(parents=True, exist_ok=True)
    schemas = generated_schemas()
    paths = []
    for filename, schema in schemas.items():
        path = directory / filename
        path.write_bytes(_serialized(schema))
        paths.append(path)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "files": {filename: sha256(_serialized(schema)).hexdigest() for filename, schema in sorted(schemas.items())},
    }
    manifest_path = directory / "manifest.json"
    manifest_path.write_bytes(_serialized(manifest))
    paths.append(manifest_path)
    return tuple(paths)


def schema_drift(directory: Path = SCHEMA_DIRECTORY) -> tuple[str, ...]:
    expected = generated_schemas()
    drift = []
    for filename, schema in expected.items():
        path = directory / filename
        if not path.exists():
            drift.append(f"missing:{filename}")
        elif path.read_bytes() != _serialized(schema):
            drift.append(f"changed:{filename}")
    expected_names = set(expected) | {"manifest.json"}
    if directory.exists():
        drift.extend(f"unexpected:{path.name}" for path in directory.glob("*.json") if path.name not in expected_names)
    manifest = directory / "manifest.json"
    if not manifest.exists():
        drift.append("missing:manifest.json")
    else:
        expected_manifest = {
            "schema_version": SCHEMA_VERSION,
            "files": {name: sha256(_serialized(schema)).hexdigest() for name, schema in sorted(expected.items())},
        }
        if manifest.read_bytes() != _serialized(expected_manifest):
            drift.append("changed:manifest.json")
    return tuple(sorted(drift))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=SCHEMA_DIRECTORY)
    args = parser.parse_args(argv)
    if args.check:
        drift = schema_drift(args.output)
        if drift:
            print("JSON Schema drift: " + ", ".join(drift))
            return 1
        print(f"JSON Schemas are current in {args.output}")
        return 0
    for path in export_schemas(args.output):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
