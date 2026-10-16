"""Frozen machine-readable contract for TRINITY Live Validation V1."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


_PATH = Path(__file__).with_name("protocol_v1.json")
_EXPECTED_ID = "TRINITY_LIVE_VALIDATION_V1"
_EXPECTED_EPOCH = 1


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _load() -> tuple[Mapping[str, Any], str]:
    value = json.loads(_PATH.read_text(encoding="utf-8"))
    if value.get("protocol_id") != _EXPECTED_ID or value.get("protocol_epoch") != _EXPECTED_EPOCH:
        raise RuntimeError("TRINITY Live Validation protocol identity drift")
    if value.get("primary_horizon_sessions") != 20 or value.get("benchmark") != "SPY_TOTAL_RETURN":
        raise RuntimeError("TRINITY Live Validation primary contract drift")
    return _freeze(value), hashlib.sha256(canonical_json(value)).hexdigest()


PROTOCOL, PROTOCOL_HASH = _load()
PROTOCOL_ID = str(PROTOCOL["protocol_id"])
PROTOCOL_EPOCH = int(PROTOCOL["protocol_epoch"])
HORIZONS = (5, 20, 60)


def protocol_dict() -> dict[str, Any]:
    """Return a detached JSON-compatible copy of the frozen protocol."""

    return json.loads(_PATH.read_text(encoding="utf-8"))
