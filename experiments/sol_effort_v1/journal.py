"""Crash-safe, append-only-ish journal for experimental provider calls.

The journal is deliberately local to an experiment output directory. Raw HTTP
responses are written before parsing, while accounting is committed even when
the response cannot be used as a financial evaluation. A call whose billing
state cannot be resolved is marked INDETERMINATE and is never retried by the
runner automatically.
"""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Mapping

from .canonical import canonical_bytes, sha256_bytes, write_json


class JournalError(RuntimeError):
    pass


TERMINAL_STATES = {"SUCCEEDED", "FAILED", "INDETERMINATE"}


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except Exception as exc:  # pragma: no cover - defensive boundary
        raise JournalError("invalid monetary value in journal") from exc


class CallJournal:
    """Persist one immutable raw response and one verified call record."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.calls_dir = self.root / "calls"
        self.raw_dir = self.root / "raw"
        self.calls_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, call_id: str) -> Path:
        return self.calls_dir / f"{call_id}.json"

    def _raw_path(self, call_id: str) -> Path:
        return self.raw_dir / f"{call_id}.response"

    def _read(self, call_id: str) -> dict[str, Any] | None:
        path = self._path(call_id)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise JournalError(f"corrupt journal record: {call_id}") from exc
        if not isinstance(value, dict):
            raise JournalError(f"journal record is not an object: {call_id}")
        return value

    def start(self, *, call_id: str, request_hash: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
        existing = self._read(call_id)
        if existing is not None:
            if existing.get("request_hash") != request_hash:
                raise JournalError(f"request hash conflict for {call_id}")
            self.verify(call_id, existing)
            if existing.get("status") == "INDETERMINATE":
                raise JournalError(f"call {call_id} is INDETERMINATE; explicit resolution required")
            return existing
        record = {
            "call_id": call_id,
            "request_hash": request_hash,
            "status": "STARTED",
            "response_received": False,
            "raw_sha256": None,
            "raw_byte_length": None,
            "cost_usd": "0",
            "accounting": {},
            **dict(metadata),
        }
        write_json(self._path(call_id), record)
        return record

    def record_http_response(
        self,
        *,
        call_id: str,
        raw: bytes,
        accounting: Mapping[str, Any],
    ) -> dict[str, Any]:
        record = self._read(call_id)
        if record is None:
            raise JournalError(f"call was not started: {call_id}")
        digest = sha256_bytes(raw)
        raw_path = self._raw_path(call_id)
        if raw_path.exists():
            existing_raw = raw_path.read_bytes()
            if existing_raw != raw:
                raise JournalError(f"raw response conflict for {call_id}")
        else:
            temporary = raw_path.with_suffix(raw_path.suffix + ".tmp")
            temporary.write_bytes(raw)
            temporary.replace(raw_path)
        record.update({
            "response_received": True,
            "raw_sha256": digest,
            "raw_byte_length": len(raw),
            "accounting": dict(accounting),
            "cost_usd": str(accounting.get("cost_usd", "0")),
        })
        write_json(self._path(call_id), record)
        return record

    def finalize(
        self,
        *,
        call_id: str,
        status: str,
        result: Mapping[str, Any] | None = None,
        error_type: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        if status not in TERMINAL_STATES:
            raise JournalError(f"invalid terminal status: {status}")
        record = self._read(call_id)
        if record is None:
            raise JournalError(f"call was not started: {call_id}")
        if record.get("status") == "INDETERMINATE" and status != "INDETERMINATE":
            raise JournalError(f"cannot overwrite INDETERMINATE call: {call_id}")
        if record.get("status") in TERMINAL_STATES and record.get("status") != status:
            raise JournalError(f"terminal status conflict for {call_id}")
        record.update({
            "status": status,
            "error_type": error_type,
            "error": error,
        })
        if result is not None:
            record["result"] = dict(result)
        write_json(self._path(call_id), record)
        return record

    def load(self, call_id: str) -> dict[str, Any] | None:
        record = self._read(call_id)
        if record is not None:
            self.verify(call_id, record)
        return record

    def verify(self, call_id: str, record: Mapping[str, Any] | None = None) -> None:
        value = dict(record or self._read(call_id) or {})
        if value.get("response_received"):
            raw_path = self._raw_path(call_id)
            if not raw_path.is_file():
                raise JournalError(f"missing raw response for {call_id}")
            raw = raw_path.read_bytes()
            if sha256_bytes(raw) != value.get("raw_sha256"):
                raise JournalError(f"raw response checksum mismatch for {call_id}")
            if len(raw) != value.get("raw_byte_length"):
                raise JournalError(f"raw response length mismatch for {call_id}")

    def consumed_cost(self) -> Decimal:
        total = Decimal("0")
        for path in self.calls_dir.glob("*.json"):
            value = json.loads(path.read_text(encoding="utf-8"))
            total += _decimal(value.get("cost_usd"))
        return total

    def records(self) -> list[dict[str, Any]]:
        values = []
        for path in sorted(self.calls_dir.glob("*.json")):
            call_id = path.stem
            values.append(self.load(call_id) or {})
        return values

