from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from trinity.ledger import (
    LedgerStorage,
    MigrationHashDrift,
    MigrationHistoryError,
    MigrationIdentityDrift,
    UnknownAppliedMigration,
)
from trinity.ledger.schema import (
    Migration,
    MigrationRegistry,
    core_migration,
    execution_coordination_migration,
)

MILESTONE_1_COMMIT = "6055d0ec88ec3ac524981325fc8ae232cd2d76b3"
MIGRATION_PATH = "trinity/ledger/migrations/0001_ledger_core.sql"
FIXED_TIME = "2026-09-29T00:00:00.000000Z"


def _fixture_migration(sequence: int) -> Migration:
    path = Path(__file__).with_name("fixtures") / f"000{sequence}_synthetic.sql"
    return Migration(f"000{sequence}_synthetic", sequence, path.read_bytes())


def _registry(*migrations: Migration) -> MigrationRegistry:
    return MigrationRegistry((core_migration(), *migrations))


def _create_milestone_one_database(path: Path) -> None:
    migration = core_migration()
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.executescript("BEGIN IMMEDIATE;\n" + migration.text)
        connection.execute(
            """
            INSERT INTO schema_migration
                (migration_id, schema_version, sha256, applied_at)
            VALUES (?, ?, ?, ?)
            """,
            (migration.migration_id, 1, migration.sha256, FIXED_TIME),
        )
        connection.execute(
            """
            INSERT INTO schema_metadata
                (singleton_id, schema_version, freeze_identifier, created_at,
                 applied_migration_id)
            VALUES (1, 1, ?, ?, ?)
            """,
            (
                "LEDGER_FOUNDATION_V1_SCHEMA_FREEZE_v1.0.0",
                FIXED_TIME,
                migration.migration_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _insert_history(path: Path, migration: Migration, *, sequence: int | None = None) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            INSERT INTO schema_migration
                (migration_id, schema_version, sha256, applied_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                migration.migration_id,
                migration.sequence if sequence is None else sequence,
                migration.sha256,
                FIXED_TIME,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def test_0001_is_byte_identical_to_milestone_one_commit():
    repository_root = Path(__file__).resolve().parents[3]
    committed = subprocess.run(
        ["git", "show", f"{MILESTONE_1_COMMIT}:{MIGRATION_PATH}"],
        cwd=repository_root,
        capture_output=True,
        check=True,
    ).stdout
    assert core_migration().sql == committed


def test_fresh_database_applies_registered_production_migrations(tmp_path):
    path = tmp_path / "fresh.sqlite3"
    with LedgerStorage.open(path) as storage:
        history = storage.connection.execute(
            "SELECT migration_id, schema_version, sha256 FROM schema_migration"
        ).fetchall()
        assert [tuple(row) for row in history] == [
            (core_migration().migration_id, 1, core_migration().sha256),
            (
                execution_coordination_migration().migration_id,
                2,
                execution_coordination_migration().sha256,
            ),
        ]
        assert storage.current_migration_level() == 2


def test_existing_milestone_one_database_is_recognized(tmp_path):
    path = tmp_path / "existing.sqlite3"
    _create_milestone_one_database(path)
    with LedgerStorage.open(path) as storage:
        assert storage.current_migration_level() == 2
        assert storage.apply_migrations(
            MigrationRegistry((core_migration(), execution_coordination_migration()))
        ) == 0


def test_registry_accepts_ordered_sequences_one_two_three():
    migration_two = _fixture_migration(2)
    migration_three = _fixture_migration(3)
    registry = _registry(migration_two, migration_three)
    assert [migration.sequence for migration in registry] == [1, 2, 3]


def test_synthetic_migrations_apply_in_order_once_and_rerun_is_idempotent(tmp_path):
    path = tmp_path / "forward.sqlite3"
    registry = _registry(_fixture_migration(2), _fixture_migration(3))
    with LedgerStorage.open(path, registry=registry) as storage:
        assert storage.current_migration_level() == 3
        assert storage.apply_migrations(registry) == 0
        history = storage.connection.execute(
            "SELECT migration_id, schema_version FROM schema_migration ORDER BY schema_version"
        ).fetchall()
        assert [tuple(row) for row in history] == [
            ("0001_ledger_core", 1),
            ("0002_synthetic", 2),
            ("0003_synthetic", 3),
        ]
        tables = {
            row[0]
            for row in storage.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"synthetic_migration_two", "synthetic_migration_three"} <= tables


def test_existing_milestone_one_database_migrates_forward(tmp_path):
    path = tmp_path / "upgrade.sqlite3"
    _create_milestone_one_database(path)
    registry = _registry(_fixture_migration(2), _fixture_migration(3))
    with LedgerStorage.open(path, registry=registry) as storage:
        assert storage.current_migration_level() == 3


def test_migration_hash_drift_is_rejected(tmp_path):
    path = tmp_path / "hash-drift.sqlite3"
    migration_two = _fixture_migration(2)
    with LedgerStorage.open(path, registry=_registry(migration_two)):
        pass
    changed = Migration(migration_two.migration_id, 2, migration_two.sql + b"\n")
    with pytest.raises(MigrationHashDrift):
        LedgerStorage.open(path, registry=_registry(changed))


def test_migration_identity_drift_is_rejected(tmp_path):
    path = tmp_path / "identity-drift.sqlite3"
    _create_milestone_one_database(path)
    renamed = Migration("0002_renamed", 2, _fixture_migration(2).sql)
    _insert_history(path, renamed)
    with pytest.raises(MigrationIdentityDrift):
        LedgerStorage.open(path, registry=_registry(_fixture_migration(2)))


def test_unknown_applied_migration_is_rejected(tmp_path):
    path = tmp_path / "unknown.sqlite3"
    _create_milestone_one_database(path)
    with LedgerStorage.open(path):
        pass
    _insert_history(path, Migration("0003_unknown", 3, b"SELECT 1;"))
    with pytest.raises(UnknownAppliedMigration):
        LedgerStorage.open(path)


def test_applied_sequence_gap_is_rejected(tmp_path):
    path = tmp_path / "gap.sqlite3"
    _create_milestone_one_database(path)
    _insert_history(path, _fixture_migration(3))
    registry = _registry(_fixture_migration(2), _fixture_migration(3))
    with pytest.raises(MigrationHistoryError, match="without gaps"):
        LedgerStorage.open(path, registry=registry)


def test_reordered_applied_history_is_rejected(tmp_path):
    path = tmp_path / "reordered.sqlite3"
    _create_milestone_one_database(path)
    _insert_history(path, _fixture_migration(3), sequence=2)
    _insert_history(path, _fixture_migration(2), sequence=3)
    registry = _registry(_fixture_migration(2), _fixture_migration(3))
    with pytest.raises(MigrationHistoryError, match="reordered"):
        LedgerStorage.open(path, registry=registry)


def test_duplicate_registry_sequence_is_rejected():
    migration_two = _fixture_migration(2)
    duplicate = Migration("0002_duplicate", 2, b"SELECT 2;")
    with pytest.raises(MigrationHistoryError, match="duplicate sequence"):
        MigrationRegistry((core_migration(), migration_two, duplicate))


def test_registry_gap_is_rejected():
    with pytest.raises(MigrationHistoryError, match="without gaps"):
        MigrationRegistry((core_migration(), _fixture_migration(3)))


def test_failed_migration_rolls_back_only_itself_and_is_not_recorded(tmp_path):
    path = tmp_path / "failure.sqlite3"
    failing = Migration(
        "0002_failing",
        2,
        b"CREATE TABLE should_rollback (value TEXT) STRICT;\n"
        b"INSERT INTO table_that_does_not_exist VALUES (1);\n",
    )
    with pytest.raises(sqlite3.DatabaseError):
        LedgerStorage.open(path, registry=_registry(failing))

    connection = sqlite3.connect(path)
    try:
        history = connection.execute(
            "SELECT migration_id, schema_version FROM schema_migration"
        ).fetchall()
        assert history == [("0001_ledger_core", 1)]
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'should_rollback'"
        ).fetchone() is None
    finally:
        connection.close()


def test_later_migrations_do_not_recreate_or_mutate_schema_metadata(tmp_path):
    path = tmp_path / "metadata.sqlite3"
    registry = _registry(_fixture_migration(2), _fixture_migration(3))
    with LedgerStorage.open(path, registry=registry) as storage:
        rows = storage.connection.execute("SELECT * FROM schema_metadata").fetchall()
        assert len(rows) == 1
        metadata = rows[0]
        assert metadata["schema_version"] == 1
        assert metadata["applied_migration_id"] == "0001_ledger_core"
        assert storage.current_migration_level() == 3
        with pytest.raises(sqlite3.IntegrityError, match="immutable record"):
            storage.connection.execute(
                "UPDATE schema_metadata SET schema_version = schema_version"
            )


def test_production_registry_contains_execution_coordination_migration():
    from trinity.ledger.schema import migration_registry

    assert [migration.sequence for migration in migration_registry()] == [1, 2]
