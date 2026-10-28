"""Minimal fail-closed validation for structured experiment responses."""

from __future__ import annotations

from typing import Any, Mapping


class ResponseValidationError(ValueError):
    pass


def _validate(value: Any, schema: Mapping[str, Any], path: str) -> None:
    if "enum" in schema and value not in schema["enum"]:
        raise ResponseValidationError(f"{path} is outside enum")
    expected = schema.get("type")
    if expected == "string" and not isinstance(value, str):
        raise ResponseValidationError(f"{path} must be a string")
    if expected == "number" and (
        isinstance(value, bool) or not isinstance(value, (int, float))
    ):
        raise ResponseValidationError(f"{path} must be a number")
    if expected == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
        raise ResponseValidationError(f"{path} must be an integer")
    if expected == "boolean" and not isinstance(value, bool):
        raise ResponseValidationError(f"{path} must be a boolean")
    if expected == "array":
        if not isinstance(value, list):
            raise ResponseValidationError(f"{path} must be an array")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                _validate(item, item_schema, f"{path}[{index}]")
    if expected == "object":
        if not isinstance(value, dict):
            raise ResponseValidationError(f"{path} must be an object")
        properties = schema.get("properties") or {}
        for field in schema.get("required") or []:
            if field not in value:
                raise ResponseValidationError(f"missing required response field: {path}.{field}")
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                raise ResponseValidationError(f"unknown response fields at {path}: {unknown}")
        for field, field_schema in properties.items():
            if field in value and isinstance(field_schema, Mapping):
                _validate(value[field], field_schema, f"{path}.{field}")


def validate_object(value: Any, schema: Mapping[str, Any]) -> dict[str, Any]:
    _validate(value, schema, "$")
    if not isinstance(value, dict):
        raise ResponseValidationError("structured response must be an object")
    return value

