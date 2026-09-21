"""
Phase 3C3D: the PROVIDER response contract.

`app/qa/prompt.py` is generated from the legacy n8n export and is never
hand-edited, so the derivation lives here instead.

THE DEFECT THIS CLOSES
----------------------
`STRUCTURED_OUTPUT_SCHEMA` has always declared

    "type": { "type": "string", "enum": ["Knowledge", "Skill", "Behaviour"] }

but the provider was called with `response_format: {"type": "json_object"}` -
plain JSON mode. The schema therefore never reached the provider as a
constraint: it was prompt text, and `validate_structured_output` was the only
thing enforcing it. The model was free to answer `"K"`, and across the two
controlled pilots it did so on four of eight first generations, exhausting all
three generations for one lecture.

Provider enforcement is the first line; the local validator is unchanged and
still runs on every response. A `"K"` that somehow arrives is still rejected,
never rewritten.

STRICT MODE IS NARROWER THAN JSON SCHEMA
----------------------------------------
OpenAI's `json_schema` + `strict: true` accepts only a subset of JSON Schema:

  * every object must set `additionalProperties: false`;
  * every declared property must appear in `required` - optionality is
    expressed as a nullable type union instead;
  * `minItems`, `maxItems`, `minimum` and `maximum` are not supported.

So the strict document is DERIVED mechanically from the base schema rather
than written by hand, and the derivation is deliberately lossy in exactly one
direction: it can only ever be WEAKER than the base schema, never stronger.
The bounds it has to drop (11 checklist rows, 3 clips, rating 1-5) are still
stated in the system message and still enforced by the local validator, which
is precisely why the validator is kept.
"""
import hashlib
import json

from app.qa.prompt import STRUCTURED_OUTPUT_SCHEMA


# The historical contract. Named, not deleted: the 2026-09-17 G2 Keith
# attempts were bought under it and must stay reproducible.
JSON_OBJECT = "json_object_v1"
# Provider-enforced strict JSON Schema.
STRICT_JSON_SCHEMA = "strict_json_schema_v1"

PROVIDER_CONTRACT_VERSIONS = (JSON_OBJECT, STRICT_JSON_SCHEMA)
DEFAULT_PROVIDER_CONTRACT = STRICT_JSON_SCHEMA

# The `name` the provider records against the schema. Stable: changing it
# would change the request without changing anything about the contract.
STRICT_SCHEMA_NAME = "legacy_qa_v8_combined_evaluation"

# Keywords strict mode rejects outright. Dropped, never silently reinterpreted.
UNSUPPORTED_KEYWORDS = ("minItems", "maxItems", "minimum", "maximum",
                        "minLength", "maxLength", "pattern", "format")


class ProviderContractError(ValueError):
    """An unknown provider response contract was requested."""


def validate_contract(version: str) -> str:
    if version not in PROVIDER_CONTRACT_VERSIONS:
        raise ProviderContractError(f"unknown provider response contract: {version}")
    return version


def _strictify(node, *, dropped: list, nullable: list, path: str = ""):
    """
    Recursively rewrite one schema node into its strict-mode equivalent.

    Two transformations, both recorded so the difference from the base schema
    is auditable rather than folklore:

      * an unsupported bound is removed and named in `dropped`;
      * a property that the base schema left optional becomes a nullable
        union and is named in `nullable`, because strict mode requires every
        property to be listed in `required`.
    """
    if not isinstance(node, dict):
        return node

    out = {}
    for key, value in node.items():
        if key in UNSUPPORTED_KEYWORDS:
            dropped.append(f"{path or '$'}.{key}")
            continue
        out[key] = value

    if out.get("type") == "object" and isinstance(out.get("properties"), dict):
        declared = list(out["properties"])
        required = list(out.get("required") or [])
        optional = [name for name in declared if name not in required]
        properties = {}
        for name in declared:
            child = _strictify(out["properties"][name], dropped=dropped,
                               nullable=nullable, path=f"{path}.{name}")
            if name in optional:
                nullable.append(f"{path}.{name}".lstrip("."))
                child = dict(child)
                child_type = child.get("type")
                if isinstance(child_type, str):
                    child["type"] = [child_type, "null"]
                elif isinstance(child_type, list) and "null" not in child_type:
                    child["type"] = list(child_type) + ["null"]
            properties[name] = child
        out["properties"] = properties
        # Strict mode: every declared property must be required.
        out["required"] = declared
        out["additionalProperties"] = False

    if isinstance(out.get("items"), dict):
        out["items"] = _strictify(out["items"], dropped=dropped, nullable=nullable,
                                  path=f"{path}[]")
    return out


def _derive():
    dropped: list[str] = []
    nullable: list[str] = []
    schema = _strictify(STRUCTURED_OUTPUT_SCHEMA, dropped=dropped, nullable=nullable)
    return schema, tuple(sorted(dropped)), tuple(sorted(set(nullable)))


STRICT_OUTPUT_SCHEMA, DROPPED_CONSTRAINTS, NULLABLE_OPTIONAL_PROPERTIES = _derive()

STRICT_OUTPUT_SCHEMA_SHA256 = hashlib.sha256(
    json.dumps(STRICT_OUTPUT_SCHEMA, sort_keys=True, separators=(",", ":"))
    .encode("utf-8")).hexdigest()

# The property names the base schema left optional. Strict mode forces the
# model to emit them, so a `null` here means "absent", and only these names
# may ever be dropped from a response.
OPTIONAL_PROPERTY_NAMES = frozenset(
    path.rsplit(".", 1)[-1].removesuffix("[]") for path in NULLABLE_OPTIONAL_PROPERTIES)


def response_format(contract: str) -> dict:
    """The `response_format` body for one contract version."""
    validate_contract(contract)
    if contract == JSON_OBJECT:
        return {"type": "json_object"}
    return {
        "type": "json_schema",
        "json_schema": {
            "name": STRICT_SCHEMA_NAME,
            "strict": True,
            "schema": STRICT_OUTPUT_SCHEMA,
        },
    }


def drop_null_optionals(payload):
    """
    Undo the strict encoding of "absent", and nothing else.

    Strict mode cannot express an optional property, so `speaker` and
    `reasoning` are declared nullable and the model returns them as `null`
    when it has nothing to say. Removing exactly those nulls restores the
    shape the base schema and the validator describe.

    This is deliberately NOT a repair pass. It only removes keys whose name
    the BASE schema marks optional and whose value is exactly `null`. No value
    is ever rewritten, no missing key is ever added, and an invalid value -
    `"K"` for a KSB type, say - passes through untouched so the validator
    still rejects it.
    """
    removed = 0

    def walk(node):
        nonlocal removed
        if isinstance(node, list):
            return [walk(item) for item in node]
        if not isinstance(node, dict):
            return node
        out = {}
        for key, value in node.items():
            if value is None and key in OPTIONAL_PROPERTY_NAMES:
                removed += 1
                continue
            out[key] = walk(value)
        return out

    return walk(payload), removed


def contract_provenance(contract: str) -> dict:
    """Printable provenance. Versions and hashes only, never the schema body."""
    validate_contract(contract)
    return {
        "provider_contract_version": contract,
        "provider_enforced_schema": contract == STRICT_JSON_SCHEMA,
        "strict_schema_name": STRICT_SCHEMA_NAME if contract == STRICT_JSON_SCHEMA else None,
        "strict_schema_sha256": (STRICT_OUTPUT_SCHEMA_SHA256
                                 if contract == STRICT_JSON_SCHEMA else None),
        "constraints_not_provider_enforced": list(DROPPED_CONSTRAINTS),
        "nullable_optional_properties": list(NULLABLE_OPTIONAL_PROPERTIES),
    }
