"""Small OpenAPI 3.1 response validator used by HTTP contract tests."""
from __future__ import annotations

from typing import Any


def assert_matches(payload: Any, schema: dict[str, Any], spec: dict[str, Any], path: str = "$") -> None:
    """Assert JSON payload matches the supported JSON Schema subset, naming the failing path."""
    if "$ref" in schema:
        prefix = "#/components/schemas/"
        reference = schema["$ref"]
        if not reference.startswith(prefix):
            raise AssertionError(f"{path}: unsupported reference {reference}")
        assert_matches(payload, spec["components"]["schemas"][reference[len(prefix):]], spec, path)
    if "oneOf" in schema:
        errors: list[str] = []
        for option in schema["oneOf"]:
            try:
                assert_matches(payload, option, spec, path)
                break
            except AssertionError as error:
                errors.append(str(error))
        else:
            raise AssertionError(f"{path}: did not match any oneOf schema ({'; '.join(errors)})")
    expected = schema.get("type")
    expected_types = expected if isinstance(expected, list) else [expected] if expected else []
    if expected_types and not any(_has_type(payload, item) for item in expected_types):
        raise AssertionError(f"{path}: expected {expected_types}, got {_type_name(payload)}")
    if "enum" in schema and payload not in schema["enum"]:
        raise AssertionError(f"{path}: expected one of {schema['enum']!r}, got {payload!r}")
    if "const" in schema and payload != schema["const"]:
        raise AssertionError(f"{path}: expected {schema['const']!r}, got {payload!r}")
    if isinstance(payload, dict):
        for name in schema.get("required", []):
            if name not in payload:
                raise AssertionError(f"{path}.{name}: required property is missing")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            unexpected = payload.keys() - properties.keys()
            if unexpected:
                name = sorted(unexpected)[0]
                raise AssertionError(f"{path}.{name}: additional property is not allowed")
        for name, value in payload.items():
            if name in properties:
                assert_matches(value, properties[name], spec, f"{path}.{name}")
            elif isinstance(schema.get("additionalProperties"), dict):
                assert_matches(value, schema["additionalProperties"], spec, f"{path}.{name}")
    if isinstance(payload, list) and isinstance(schema.get("items"), dict):
        for index, value in enumerate(payload):
            assert_matches(value, schema["items"], spec, f"{path}[{index}]")


def _has_type(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    return expected == "string" and isinstance(value, str)


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"
