"""Separate append-only SQLite store for validation observations."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from .protocol_v1 import canonical_json


class ValidationStorageConflict(RuntimeError):
    pass


class ValidationStorageIntegrityError(RuntimeError):
    pass


class ValidationStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS validation_record (
                record_kind TEXT NOT NULL,
                record_key TEXT NOT NULL,
                payload_json BLOB NOT NULL,
                payload_sha256 TEXT NOT NULL,
                appended_at TEXT NOT NULL,
                PRIMARY KEY (record_kind, record_key)
            )
        """)

    def __enter__(self) -> "ValidationStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is None:
            self.connection.commit()
        else:
            self.connection.rollback()
        self.connection.close()

    def append(self, record_kind: str, record_key: str, payload: Any) -> bool:
        raw = canonical_json(payload)
        digest = hashlib.sha256(raw).hexdigest()
        existing = self.connection.execute(
            "SELECT payload_sha256, payload_json FROM validation_record WHERE record_kind=? AND record_key=?",
            (record_kind, record_key),
        ).fetchone()
        if existing is not None:
            if existing[0] == digest and bytes(existing[1]) == raw:
                return False
            raise ValidationStorageConflict("same validation identity has a divergent payload")
        self.connection.execute(
            "INSERT INTO validation_record VALUES (?, ?, ?, ?, ?)",
            (record_kind, record_key, raw, digest, datetime.now(timezone.utc).isoformat()),
        )
        return True

    @staticmethod
    def _decode_payload(payload_json: object, payload_sha256: object) -> Any:
        try:
            raw = bytes(payload_json)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValidationStorageIntegrityError("stored validation payload is not bytes") from exc
        digest = hashlib.sha256(raw).hexdigest()
        if not isinstance(payload_sha256, str) or digest != payload_sha256:
            raise ValidationStorageIntegrityError("stored validation payload hash mismatch")
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationStorageIntegrityError("stored validation payload is not JSON") from exc

    def records(self, record_kind: str) -> list[Any]:
        rows = self.connection.execute(
            "SELECT payload_json, payload_sha256 FROM validation_record "
            "WHERE record_kind=? ORDER BY record_key",
            (record_kind,),
        ).fetchall()
        return [self._decode_payload(row[0], row[1]) for row in rows]

    def record_items(self, record_kind: str) -> list[tuple[str, Any]]:
        rows = self.connection.execute(
            "SELECT record_key, payload_json, payload_sha256 FROM validation_record "
            "WHERE record_kind=? ORDER BY record_key",
            (record_kind,),
        ).fetchall()
        return [
            (str(row[0]), self._decode_payload(row[1], row[2]))
            for row in rows
        ]

    def integrity(self) -> tuple[str, int]:
        integrity = str(self.connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(self.connection.execute("PRAGMA foreign_key_check").fetchall())
        return integrity, foreign_keys
