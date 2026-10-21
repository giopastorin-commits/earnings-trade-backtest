"""Frozen independent specification for the simple quantitative comparator."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .protocol_v1 import PROTOCOL_HASH, PROTOCOL_ID, canonical_json


_PATH = Path(__file__).with_name("baseline_v1.json")
_EXPECTED_ID = "TRINITY_SIMPLE_QUANT_BASELINE_V1"
_EXPECTED_VERSION = 1


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _load() -> tuple[Mapping[str, Any], str]:
    value = json.loads(_PATH.read_text(encoding="utf-8"))
    if value.get("baseline_id") != _EXPECTED_ID \
            or value.get("baseline_version") != _EXPECTED_VERSION:
        raise RuntimeError("simple quant baseline identity drift")
    if value.get("upstream_protocol_id") != PROTOCOL_ID \
            or value.get("upstream_protocol_hash") != PROTOCOL_HASH:
        raise RuntimeError("simple quant baseline upstream protocol drift")
    if value.get("ranking") != [
        "ORIGINAL_RR1_DESC", "ORIGINAL_RR2_DESC", "TICKER_ASC",
    ] or value.get("selection_count") != "SOL_COUNT":
        raise RuntimeError("simple quant baseline methodology drift")
    return _freeze(value), hashlib.sha256(canonical_json(value)).hexdigest()


BASELINE, BASELINE_HASH = _load()
BASELINE_ID = str(BASELINE["baseline_id"])
BASELINE_VERSION = int(BASELINE["baseline_version"])


def baseline_dict() -> dict[str, Any]:
    return json.loads(_PATH.read_text(encoding="utf-8"))
