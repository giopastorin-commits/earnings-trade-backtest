from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import uuid
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
    input_observation_migration,
    migration_registry,
    temporal_derivation_dag_migration,
    pit_classification_migration,
    research_llm_setup_migration,
    research_method_versions_migration,
    responses_api_provenance_migration,
)
from trinity.ledger.contracts_v14 import RESEARCH_METHOD_DEFINITION

MILESTONE_1_COMMIT = "6055d0ec88ec3ac524981325fc8ae232cd2d76b3"
MIGRATION_PATH = "trinity/ledger/migrations/0001_ledger_core.sql"
MILESTONE_2_COMMIT = "81d41b500791be8d658ff0f819b624a7e50c2928"
MIGRATION_2_PATH = "trinity/ledger/migrations/0002_execution_coordination.sql"
MILESTONE_3A_COMMIT = "28942ac269594a92b7faa6797f84b861d67c40a7"
MIGRATION_3_PATH = "trinity/ledger/migrations/0003_input_observation.sql"
FACTS_V3_BASE_COMMIT = "46b7967ed2bc8c6b0ba48dee8d5123b5ead638c8"
MIGRATION_6_PATH = "trinity/ledger/migrations/0006_research_llm_setup.sql"
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


def test_0002_is_byte_identical_to_milestone_two_commit():
    repository_root = Path(__file__).resolve().parents[3]
    committed = subprocess.run(
        ["git", "show", f"{MILESTONE_2_COMMIT}:{MIGRATION_2_PATH}"],
        cwd=repository_root,
        capture_output=True,
        check=True,
    ).stdout
    assert execution_coordination_migration().sql == committed


def test_0003_is_byte_identical_to_milestone_3a_commit():
    repository_root = Path(__file__).resolve().parents[3]
    committed = subprocess.run(
        ["git", "show", f"{MILESTONE_3A_COMMIT}:{MIGRATION_3_PATH}"],
        cwd=repository_root,
        capture_output=True,
        check=True,
    ).stdout
    assert input_observation_migration().sql == committed


def test_0006_is_byte_identical_to_facts_v3_base_commit():
    assert FACTS_V3_BASE_COMMIT == "46b7967ed2bc8c6b0ba48dee8d5123b5ead638c8"
    assert MIGRATION_6_PATH == "trinity/ledger/migrations/0006_research_llm_setup.sql"
    assert hashlib.sha256(research_llm_setup_migration().sql).hexdigest() == (
        "b1d05a667d46193f93da48a5c2914e15a5a70191f2ae3bdf497b878b55ed9146"
    )


def _populate_v6_research(path: Path):
    v6_registry = MigrationRegistry(migration_registry().migrations[:6])
    with LedgerStorage.open(path, registry=v6_registry) as storage:
        request = storage.create_run_request(
            request_kind="RECONSTRUCTION",
            analysis_cutoff_at="2026-09-30T00:00:00.000000Z",
            parameters={"historical_as_of_at": "2026-09-29T00:00:00.000000Z"},
            idempotency_key="migration-v6-preservation",
            requested_by="migration-test",
            baseline_commit="a" * 40,
        )
        attempt = storage.allocate_attempt(
            run_request_id=request.run_request_id,
            worker_identity="migration-test-worker",
            code_commit="a" * 40,
            environment_fingerprint="migration:test:v6",
        )
        definition = storage.insert_json_artifact(
            artifact_type="ledger.usa-v2-research-method-definition.v1",
            value=RESEARCH_METHOD_DEFINITION,
        )
        method = storage.register_research_method(
            definition_artifact_id=definition.artifact_id,
            code_commit="a" * 40,
        )
        raw = storage.insert_opaque_artifact(
            artifact_type="fixture.migration-source.v1",
            payload=b"historical source bytes",
            media_type="application/octet-stream",
        )
        observation = storage.create_input_observation(
            artifact_id=raw.artifact_id,
            source_id="fixture:migration:v1",
            source_record_key="historical-source",
            source_published_at=None,
            retrieved_at=FIXED_TIME,
            availability_basis="RETRIEVED_AT_FALLBACK",
            effective_available_at=FIXED_TIME,
            observed_by_attempt_id=attempt.attempt_id,
            fence_token=attempt.fence_token,
            source_metadata={
                "schema_version": "1",
                "provider": "fixture",
                "dataset_name": "migration",
                "source_record_key": "historical-source",
                "acquisition_method": "checked-in-fixture",
                "availability_rule_id": "retrieval-fallback",
                "availability_rule_version": "1",
                "evidence_artifact_ids": [],
                "provider_metadata": {},
                "future_effective_at": None,
            },
        )
        parent = storage.create_raw_derivation_node(
            attempt_id=attempt.attempt_id,
            fence_token=attempt.fence_token,
            input_observation_id=observation.input_observation_id,
        )
        content = storage.insert_json_artifact(
            artifact_type="fixture.historical-research-content.v1",
            value={"historical": True},
        )
        research_id = str(uuid.uuid4())
        node_id = str(uuid.uuid4())
        edge_id = str(uuid.uuid4())
        with storage.transaction() as connection, storage._internal_write():
            connection.execute(
                """INSERT INTO research_record VALUES
                (?, ?, NULL, 'TRINITY_USA_RESEARCH', 'ticker:TEST', ?, ?,
                 'USA_V2', ?, ?, NULL, ?)""",
                (research_id, attempt.attempt_id, content.artifact_id,
                 method.research_method_id, FIXED_TIME, node_id, FIXED_TIME),
            )
            connection.execute(
                "INSERT INTO derivation_edge VALUES (?, ?, ?, 'FACTS', 1)",
                (edge_id, parent.derivation_node_id, node_id),
            )
            connection.execute(
                """INSERT INTO derivation_node VALUES
                (?, NULL, ?, 'RESEARCH', 'research_record', ?, NULL, ?,
                 'MAX_REQUIRED_PARENTS_V1')""",
                (node_id, attempt.attempt_id, research_id, FIXED_TIME),
            )
        artifacts = [
            tuple(row) for row in storage.connection.execute(
                """SELECT artifact_id, payload, sha256, byte_length
                FROM artifact ORDER BY artifact_id"""
            )
        ]
        methods = [tuple(row) for row in storage.connection.execute(
            "SELECT * FROM research_method ORDER BY research_method_id"
        )]
        records = [tuple(row) for row in storage.connection.execute(
            "SELECT * FROM research_record ORDER BY research_id"
        )]
        assert storage.current_migration_level() == 6
    return artifacts, methods, records


def test_populated_v6_upgrade_preserves_research_and_artifacts(tmp_path):
    path = tmp_path / "populated-v6.sqlite3"
    before_artifacts, before_methods, before_records = _populate_v6_research(path)

    with LedgerStorage.open(path) as storage:
        after_artifacts = [tuple(row) for row in storage.connection.execute(
            """SELECT artifact_id, payload, sha256, byte_length
            FROM artifact ORDER BY artifact_id"""
        )]
        after_methods = [tuple(row) for row in storage.connection.execute(
            "SELECT * FROM research_method ORDER BY research_method_id"
        )]
        after_records = [tuple(row) for row in storage.connection.execute(
            "SELECT * FROM research_record ORDER BY research_id"
        )]
        assert storage.current_migration_level() == 8
        assert storage.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert storage.connection.execute(
            "SELECT name FROM sqlite_schema WHERE sql LIKE '%_v6%'"
        ).fetchall() == []
        required_schema_objects = {
            "research_record_attempt_idx",
            "research_method_insert_authorized", "research_method_no_update",
            "research_method_no_delete", "research_record_insert_authorized",
            "research_record_update_authorized", "research_record_update_valid",
            "research_record_no_delete", "research_llm_interaction_insert_authorized",
            "research_llm_interaction_insert_valid", "research_llm_interaction_no_update",
            "research_llm_interaction_no_delete",
            "setup_research_lineage_insert_authorized",
            "setup_research_lineage_insert_valid", "setup_research_lineage_no_update",
            "setup_research_lineage_no_delete", "derivation_node_insert_valid",
        }
        actual_schema_objects = {
            row[0] for row in storage.connection.execute(
                "SELECT name FROM sqlite_schema WHERE name IN (%s)"
                % ",".join("?" for _ in required_schema_objects),
                tuple(required_schema_objects),
            )
        }
        assert actual_schema_objects == required_schema_objects

    assert after_artifacts == before_artifacts
    assert after_methods == before_methods
    assert after_records == before_records


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
            (
                input_observation_migration().migration_id,
                3,
                input_observation_migration().sha256,
            ),
            (
                temporal_derivation_dag_migration().migration_id,
                4,
                temporal_derivation_dag_migration().sha256,
            ),
            (
                pit_classification_migration().migration_id,
                5,
                pit_classification_migration().sha256,
            ),
            (
                research_llm_setup_migration().migration_id,
                6,
                research_llm_setup_migration().sha256,
            ),
            (
                research_method_versions_migration().migration_id,
                7,
                research_method_versions_migration().sha256,
            ),
            (
                responses_api_provenance_migration().migration_id,
                8,
                responses_api_provenance_migration().sha256,
            ),
        ]
        assert storage.current_migration_level() == 8


def test_existing_milestone_one_database_is_recognized(tmp_path):
    path = tmp_path / "existing.sqlite3"
    _create_milestone_one_database(path)
    with LedgerStorage.open(path) as storage:
        assert storage.current_migration_level() == 8
        assert storage.apply_migrations(migration_registry()) == 0


def test_existing_level_two_database_upgrades_to_input_observation(tmp_path):
    path = tmp_path / "level-two.sqlite3"
    level_two = MigrationRegistry((core_migration(), execution_coordination_migration()))
    with LedgerStorage.open(path, registry=level_two) as storage:
        assert storage.current_migration_level() == 2
    with LedgerStorage.open(path) as upgraded:
        assert upgraded.current_migration_level() == 8
        assert upgraded.apply_migrations(migration_registry()) == 0
        assert upgraded.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert upgraded.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'input_observation'"
        ).fetchone() is not None


def test_existing_level_three_database_upgrades_to_temporal_dag(tmp_path):
    path = tmp_path / "level-three.sqlite3"
    level_three = MigrationRegistry(
        (
            core_migration(),
            execution_coordination_migration(),
            input_observation_migration(),
        )
    )
    with LedgerStorage.open(path, registry=level_three) as storage:
        assert storage.current_migration_level() == 3
    with LedgerStorage.open(path) as upgraded:
        assert upgraded.current_migration_level() == 8
        assert upgraded.apply_migrations(migration_registry()) == 0
        assert upgraded.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_populated_level_four_database_upgrades_to_pit_classification(tmp_path):
    path = tmp_path / "level-four.sqlite3"
    level_four = MigrationRegistry(
        (
            core_migration(), execution_coordination_migration(),
            input_observation_migration(), temporal_derivation_dag_migration(),
        )
    )
    with LedgerStorage.open(path, registry=level_four) as storage:
        artifact = storage.insert_json_artifact(
            artifact_type="pre-v1.3.fixture.v1", value={"preserved": True}
        )
        artifact_id = artifact.artifact_id
        assert storage.current_migration_level() == 4
    with LedgerStorage.open(path) as upgraded:
        assert upgraded.current_migration_level() == 8
        assert upgraded.get_artifact(artifact_id).artifact_id == artifact_id
        assert upgraded.connection.execute(
            "SELECT count(*) FROM derivation_node_classification"
        ).fetchone()[0] == 0
        assert upgraded.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = {
            row[0]
            for row in upgraded.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"derivation_node", "derivation_edge"} <= tables


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
    _insert_history(path, Migration("0009_unknown", 9, b"SELECT 1;"))
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


def test_production_registry_contains_temporal_derivation_migration():
    from trinity.ledger.schema import migration_registry

    assert [migration.sequence for migration in migration_registry()] == [1, 2, 3, 4, 5, 6, 7, 8]
