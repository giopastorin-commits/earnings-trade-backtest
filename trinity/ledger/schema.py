"""Schema identity and deterministic migration loading for Ledger V1."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from importlib.resources import files

SCHEMA_VERSION = 1
FREEZE_IDENTIFIER = "LEDGER_FOUNDATION_V1_SCHEMA_FREEZE_v1.0.0"


@dataclass(frozen=True)
class Migration:
    migration_id: str
    schema_version: int
    sql: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.sql).hexdigest()

    @property
    def text(self) -> str:
        return self.sql.decode("utf-8")


def core_migration() -> Migration:
    resource = files("trinity.ledger.migrations").joinpath("0001_ledger_core.sql")
    return Migration("0001_ledger_core", SCHEMA_VERSION, resource.read_bytes())
