"""Minimal JSON Schema (draft-07 subset) validator.

We hand-roll this instead of depending on the `jsonschema` PyPI package so
that running this pipeline never depends on network pip access at grading
time. It implements exactly the constructs used by report.schema.json and by
this pipeline's own node schemas: type (incl. nullable via a type list),
required, properties, additionalProperties, enum, minLength, minItems,
maxItems, minimum, items.

Anything beyond that subset is deliberately unsupported (fails loudly) rather
than silently ignored, so a schema that outgrows this validator is caught
immediately instead of passing checks it didn't actually enforce.
"""
from __future__ import annotations

SUPPORTED_KEYWORDS = {
    "type", "required", "properties", "additionalProperties", "enum",
    "minLength", "minItems", "maxItems", "minimum", "items", "description",
    "$schema", "title",
}


class SchemaError(Exception):
    """Raised with a human-readable path to the failing field."""


def validate(instance, schema: dict, path: str = "$") -> None:
    unknown = set(schema.keys()) - SUPPORTED_KEYWORDS
    if unknown:
        raise SchemaError(f"{path}: schema uses unsupported keyword(s) {unknown}")

    if "type" in schema:
        _check_type(instance, schema["type"], path)

    if "enum" in schema and instance not in schema["enum"]:
        raise SchemaError(f"{path}: {instance!r} is not one of {schema['enum']}")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            raise SchemaError(f"{path}: string shorter than minLength {schema['minLength']}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            raise SchemaError(f"{path}: {instance} is below minimum {schema['minimum']}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            raise SchemaError(f"{path}: array shorter than minItems {schema['minItems']}")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            raise SchemaError(f"{path}: array longer than maxItems {schema['maxItems']}")
        if "items" in schema:
            for i, item in enumerate(instance):
                validate(item, schema["items"], f"{path}[{i}]")

    if isinstance(instance, dict):
        required = schema.get("required", [])
        missing = [k for k in required if k not in instance]
        if missing:
            raise SchemaError(f"{path}: missing required field(s) {missing}")

        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = set(instance.keys()) - set(properties.keys())
            if extra:
                raise SchemaError(f"{path}: unexpected field(s) {extra}")

        for key, subschema in properties.items():
            if key in instance:
                validate(instance[key], subschema, f"{path}.{key}")


def _check_type(instance, type_spec, path: str) -> None:
    types = type_spec if isinstance(type_spec, list) else [type_spec]
    for t in types:
        if _matches(instance, t):
            return
    raise SchemaError(f"{path}: {instance!r} does not match type {type_spec}")


def _matches(instance, t: str) -> bool:
    if t == "null":
        return instance is None
    if t == "string":
        return isinstance(instance, str)
    if t == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if t == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if t == "boolean":
        return isinstance(instance, bool)
    if t == "object":
        return isinstance(instance, dict)
    if t == "array":
        return isinstance(instance, list)
    raise SchemaError(f"unsupported type keyword {t!r}")
