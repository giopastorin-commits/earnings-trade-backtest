"""Schema identity and deterministic migration loading for Ledger V1."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from importlib.resources import files

from .errors import MigrationHistoryError

SCHEMA_VERSION = 1
FREEZE_IDENTIFIER = "LEDGER_FOUNDATION_V1_SCHEMA_FREEZE_v1.0.0"


@dataclass(frozen=True)
class Migration:
    """One immutable migration; schema_version is its ordered sequence number."""

    migration_id: str
    schema_version: int
    sql: bytes

    @property
    def sequence(self) -> int:
        return self.schema_version

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.sql).hexdigest()

    @property
    def text(self) -> str:
        return self.sql.decode("utf-8")


def core_migration() -> Migration:
    resource = files("trinity.ledger.migrations").joinpath("0001_ledger_core.sql")
    return Migration("0001_ledger_core", SCHEMA_VERSION, resource.read_bytes())


def execution_coordination_migration() -> Migration:
    resource = files("trinity.ledger.migrations").joinpath(
        "0002_execution_coordination.sql"
    )
    return Migration("0002_execution_coordination", 2, resource.read_bytes())


def input_observation_migration() -> Migration:
    resource = files("trinity.ledger.migrations").joinpath(
        "0003_input_observation.sql"
    )
    return Migration("0003_input_observation", 3, resource.read_bytes())


@dataclass(frozen=True)
class MigrationRegistry:
    """A complete, deterministic migration sequence understood by a writer."""

    migrations: tuple[Migration, ...]

    def __post_init__(self) -> None:
        sequences = [migration.sequence for migration in self.migrations]
        identifiers = [migration.migration_id for migration in self.migrations]
        if len(sequences) != len(set(sequences)):
            raise MigrationHistoryError("migration registry contains a duplicate sequence")
        if len(identifiers) != len(set(identifiers)):
            raise MigrationHistoryError("migration registry contains a duplicate identifier")
        expected = list(range(1, len(self.migrations) + 1))
        if sequences != expected:
            raise MigrationHistoryError(
                f"migration registry must be ordered without gaps: expected {expected}, "
                f"got {sequences}"
            )

    def __iter__(self):
        return iter(self.migrations)

    def __len__(self) -> int:
        return len(self.migrations)

    @property
    def bootstrap(self) -> Migration:
        if not self.migrations:
            raise MigrationHistoryError("migration registry must contain bootstrap migration 1")
        return self.migrations[0]


def migration_registry() -> MigrationRegistry:
    """Return the complete production migration registry."""

    return MigrationRegistry(
        (
            core_migration(),
            execution_coordination_migration(),
            input_observation_migration(),
        )
    )
