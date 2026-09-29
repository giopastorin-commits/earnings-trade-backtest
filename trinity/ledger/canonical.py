"""Canonical byte encodings used at the Ledger artifact hashing boundary."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

from .errors import CanonicalizationError, UnsupportedCanonicalValue

CANONICAL_JSON_V1 = "JCS-LEDGER-SUBSET-V1"
IDENTITY = "IDENTITY"
_MAX_SAFE_INTEGER = 9_007_199_254_740_991


def canonicalize_opaque(payload: bytes | bytearray | memoryview) -> bytes:
    """Return opaque payload bytes unchanged in value."""

    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise UnsupportedCanonicalValue("opaque payload must be bytes-like")
    return bytes(payload)


def canonicalize_json(value: Any) -> bytes:
    """Encode the restricted Ledger JSON subset using JCS-compatible rules.

    Domain layers must sort schema-declared unordered collections before this
    function is called. Arrays here are always semantically ordered.
    """

    try:
        return _encode(value).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise UnsupportedCanonicalValue("lone Unicode surrogates are unsupported") from exc


def canonicalize_json_document(document: str | bytes | bytearray) -> bytes:
    """Parse JSON with duplicate/non-finite/float checks, then canonicalize it."""

    if isinstance(document, (bytes, bytearray)):
        try:
            text = bytes(document).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CanonicalizationError("structured JSON must be valid UTF-8") from exc
    elif isinstance(document, str):
        text = document
    else:
        raise UnsupportedCanonicalValue("JSON document must be str or UTF-8 bytes")

    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_float=_parse_restricted_float,
            parse_constant=_reject_non_finite,
        )
    except CanonicalizationError:
        raise
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise CanonicalizationError("invalid structured JSON") from exc
    return canonicalize_json(value)


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CanonicalizationError(f"duplicate object key: {key!r}")
        result[key] = value
    return result


def _parse_restricted_float(token: str) -> int:
    # JCS normalizes every spelling of signed zero to the integer token 0. All
    # other binary floating-point values are deliberately outside this subset;
    # decision-relevant decimals must arrive as strings.
    try:
        if float(token) == 0.0:
            return 0
    except ValueError as exc:  # pragma: no cover - json validates first
        raise CanonicalizationError("invalid JSON number") from exc
    raise UnsupportedCanonicalValue(
        "binary floating-point values are unsupported; use a canonical decimal string"
    )


def _reject_non_finite(token: str) -> None:
    raise UnsupportedCanonicalValue(f"non-finite number is unsupported: {token}")


def _utf16_sort_key(value: str) -> bytes:
    try:
        return value.encode("utf-16-be")
    except UnicodeEncodeError as exc:
        raise UnsupportedCanonicalValue("lone Unicode surrogates are unsupported") from exc


def _encode(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_INTEGER:
            raise UnsupportedCanonicalValue("integer exceeds the JCS interoperable range")
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise UnsupportedCanonicalValue("NaN and Infinity are unsupported")
        if value == 0.0:
            return "0"
        raise UnsupportedCanonicalValue(
            "binary floating-point values are unsupported; use a canonical decimal string"
        )
    if isinstance(value, str):
        # json.dumps supplies the JSON escaping rules. ensure_ascii=False keeps
        # valid Unicode as UTF-8 and separators are irrelevant for a string.
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise UnsupportedCanonicalValue("JSON object keys must be strings")
        keys = sorted(value, key=_utf16_sort_key)
        return "{" + ",".join(f"{_encode(key)}:{_encode(value[key])}" for key in keys) + "}"
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray, memoryview)
    ):
        return "[" + ",".join(_encode(item) for item in value) + "]"
    raise UnsupportedCanonicalValue(f"unsupported canonical value: {type(value).__name__}")
