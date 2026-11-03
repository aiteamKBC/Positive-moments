"""
A small OpenAPI 3.0 checker for the LMS delivery document (docs/api/publisher_lms_v1.openapi.json).

`problems(spec, value, schema)` lists every way a JSON value departs from a schema of the document:
type, nullable, enum, pattern, the date / date-time / uuid formats, required properties, properties
the document does not describe, and array items. Enough to hold the hand-written document to the
real response bodies (tests/integration/test_publisher_lms_openapi.py) and its own examples
(backend/operations/tests/test_api_docs.py) - not a general validator.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime
from pathlib import Path

SPEC_FILE = Path(__file__).resolve().parents[1] / "docs" / "api" / "publisher_lms_v1.openapi.json"
TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "integer": int, "number": (int, float)}


def load(path: Path = SPEC_FILE) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def resolve(spec: dict, node):
    """Follow local `$ref`s; a dangling or external reference raises."""
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if not ref.startswith("#/"):
            raise ValueError(f"external reference {ref}")
        target = spec
        for key in ref[2:].split("/"):
            target = target[key]
        node = target
    return node


def references(node) -> list[str]:
    if isinstance(node, dict):
        return ([node["$ref"]] if "$ref" in node else []) + [r for v in node.values() for r in references(v)]
    if isinstance(node, list):
        return [r for v in node for r in references(v)]
    return []


def schema(name: str) -> dict:
    return {"$ref": f"#/components/schemas/{name}"}


def problems(spec: dict, value, node, where: str = "$") -> list[str]:
    node = resolve(spec, node)
    if value is None:
        return [] if node.get("nullable") else [f"{where}: null is not allowed"]
    if "enum" in node and value not in node["enum"]:
        return [f"{where}: {value!r} is not one of {node['enum']}"]
    kind = node.get("type")
    if kind and (not isinstance(value, TYPES[kind]) or (kind in ("integer", "number") and isinstance(value, bool))):
        return [f"{where}: expected {kind}, got {type(value).__name__}"]
    found = []
    if kind == "string":
        found += _string(node, value, where)
    elif kind == "object":
        described = node.get("properties", {})
        found += [f"{where}: missing {key}" for key in node.get("required", []) if key not in value]
        found += [f"{where}: undocumented {key}" for key in value if described and key not in described]
        for key, item in value.items():
            if key in described:
                found += problems(spec, item, described[key], f"{where}.{key}")
    elif kind == "array" and "items" in node:
        for index, item in enumerate(value):
            found += problems(spec, item, node["items"], f"{where}[{index}]")
    return found


def _string(node, value, where) -> list[str]:
    if "pattern" in node and not re.search(node["pattern"], value):
        return [f"{where}: {value!r} does not match {node['pattern']}"]
    try:
        if node.get("format") == "date-time":
            if datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
                return [f"{where}: date-time without a timezone"]
        elif node.get("format") == "date":
            date.fromisoformat(value)
        elif node.get("format") == "uuid":
            uuid.UUID(value)
    except ValueError:
        return [f"{where}: {value!r} is not a {node['format']}"]
    return []
