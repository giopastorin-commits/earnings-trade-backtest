from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from trinity.ledger import (
    ArtifactIntegrityError,
    ArtifactMetadataConflict,
    CanonicalizationError,
    LedgerStorage,
    MigrationHashDrift,
    UnsupportedSchemaVersion,
    artifact_id_for,
    canonicalize_json_document,
)
from trinity.ledger.canonical import IDENTITY
from trinity.ledger.schema import Migration, core_migration


@pytest.fixture
def storage(tmp_path: Path):
    with LedgerStorage.open(tmp_path / "ledger-test.sqlite3") as opened:
        yield opened


def test_new_database_has_only_milestone_one_tables(storage):
    tables = {
        row["name"]
        for row in storage.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert tables == {"schema_metadata", "schema_migration", "artifact"}


def test_all_ledger_tables_are_strict(storage):
    strict = {
        row["name"]: row["strict"]
        for row in storage.connection.execute("PRAGMA table_list")
        if row["name"] in {"schema_metadata", "schema_migration", "artifact"}
    }
    assert strict == {"schema_metadata": 1, "schema_migration": 1, "artifact": 1}


def test_connection_pragmas_are_configured(storage):
    assert storage.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert storage.connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert storage.connection.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_migration_applies_once_and_is_idempotent(storage):
    rows_before = storage.connection.execute("SELECT * FROM schema_migration").fetchall()
    assert len(rows_before) == 1
    assert storage.apply_migration(core_migration()) is False
    rows_after = storage.connection.execute("SELECT * FROM schema_migration").fetchall()
    assert [tuple(row) for row in rows_after] == [tuple(row) for row in rows_before]


def test_changed_applied_migration_is_rejected(storage):
    original = core_migration()
    changed = Migration(original.migration_id, original.schema_version, original.sql + b"\n")
    with pytest.raises(MigrationHashDrift):
        storage.apply_migration(changed)


def test_unsupported_migration_version_is_rejected(storage):
    migration = Migration("0002_future", 2, b"SELECT 1;")
    with pytest.raises(UnsupportedSchemaVersion):
        storage.apply_migration(migration)


@pytest.mark.parametrize("operation", ["UPDATE artifact SET media_type = 'x'", "DELETE FROM artifact"])
def test_artifact_update_and_delete_are_rejected(storage, operation):
    storage.insert_opaque_artifact(
        artifact_type="fixture", payload=b"payload", media_type="application/octet-stream"
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable record: artifact"):
        storage.connection.execute(operation)


@pytest.mark.parametrize("table", ["schema_metadata", "schema_migration"])
@pytest.mark.parametrize("verb", ["UPDATE", "DELETE"])
def test_schema_records_are_immutable(storage, table, verb):
    statement = (
        f"UPDATE {table} SET schema_version = schema_version" if verb == "UPDATE" else f"DELETE FROM {table}"
    )
    with pytest.raises(sqlite3.IntegrityError, match=f"immutable record: {table}"):
        storage.connection.execute(statement)


def test_known_golden_artifact_hash():
    assert artifact_id_for("golden.fixture", IDENTITY, b"hello ledger") == (
        "sha256:e5374457a1ab600f8b83e98469181680f66691a70678170790996d1310090231"
    )


def test_duplicate_insertion_deduplicates(storage):
    first = storage.insert_opaque_artifact(
        artifact_type="fixture", payload=b"same", media_type="application/octet-stream"
    )
    second = storage.insert_opaque_artifact(
        artifact_type="fixture", payload=b"same", media_type="application/octet-stream"
    )
    assert first == second
    assert storage.connection.execute("SELECT count(*) FROM artifact").fetchone()[0] == 1


def test_hash_domain_includes_type_version_and_every_payload_byte():
    base = artifact_id_for("type-a", "v1", b"payload")
    assert artifact_id_for("type-b", "v1", b"payload") != base
    assert artifact_id_for("type-a", "v2", b"payload") != base
    assert artifact_id_for("type-a", "v1", b"payloae") != base


def test_opaque_whitespace_changes_hash():
    assert artifact_id_for("text", IDENTITY, b"a b") != artifact_id_for(
        "text", IDENTITY, b"a  b"
    )


def test_structured_formatting_and_key_order_do_not_change_hash():
    first = canonicalize_json_document('{"b":2,"a":1}')
    second = canonicalize_json_document(' { "a" : 1, "b" : 2 } ')
    assert first == second
    assert artifact_id_for("json", "JCS-LEDGER-SUBSET-V1", first) == artifact_id_for(
        "json", "JCS-LEDGER-SUBSET-V1", second
    )


def test_structured_insertion_rejects_noncanonical_bytes(storage):
    with pytest.raises(CanonicalizationError, match="already be canonical"):
        storage.insert_artifact(
            artifact_type="json",
            canonicalization_version="JCS-LEDGER-SUBSET-V1",
            payload=b'{ "b": 2, "a": 1 }',
            media_type="application/json",
        )


def test_artifact_id_format_and_persisted_fields(storage):
    artifact = storage.insert_opaque_artifact(
        artifact_type="fixture", payload=b"payload", media_type="application/octet-stream"
    )
    assert artifact.artifact_id.startswith("sha256:")
    assert len(artifact.artifact_id) == 71
    assert artifact.byte_length == len(artifact.payload) == 7
    assert artifact.sha256 == hashlib.sha256(b"payload").hexdigest()
    assert artifact.content_hash == artifact.artifact_id[7:]


def test_invalid_claimed_artifact_id_is_rejected(storage):
    with pytest.raises(ArtifactIntegrityError, match="claimed artifact ID"):
        storage.insert_artifact(
            artifact_type="fixture",
            canonicalization_version=IDENTITY,
            payload=b"payload",
            media_type="application/octet-stream",
            claimed_artifact_id="sha256:" + "0" * 64,
        )


def test_database_rejects_incorrect_byte_length(storage):
    with pytest.raises(sqlite3.IntegrityError):
        storage.connection.execute(
            """
            INSERT INTO artifact (
              artifact_id, sha256, content_hash, byte_length, media_type,
              storage_uri, created_at, artifact_kind, canonicalization_version, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "sha256:" + "1" * 64,
                "2" * 64,
                "1" * 64,
                99,
                "application/octet-stream",
                "sqlite:artifact:sha256:" + "1" * 64,
                "2026-09-29T00:00:00.000000Z",
                "fixture",
                IDENTITY,
                b"x",
            ),
        )


def test_tampered_persisted_blob_fails_read_verification(storage):
    artifact = storage.insert_opaque_artifact(
        artifact_type="fixture", payload=b"payload", media_type="application/octet-stream"
    )
    # Simulate out-of-band file corruption by deliberately removing the SQL
    # guard. Same-length bytes keep the byte-length CHECK satisfied.
    storage.connection.execute("DROP TRIGGER artifact_no_update")
    storage.connection.execute(
        "UPDATE artifact SET payload = ? WHERE artifact_id = ?",
        (b"PAYLOAD", artifact.artifact_id),
    )
    with pytest.raises(ArtifactIntegrityError, match="SHA-256"):
        storage.get_artifact(artifact.artifact_id)


def test_incompatible_metadata_for_same_address_fails(storage):
    storage.insert_opaque_artifact(
        artifact_type="fixture", payload=b"payload", media_type="application/octet-stream"
    )
    with pytest.raises(ArtifactMetadataConflict):
        storage.insert_opaque_artifact(
            artifact_type="fixture", payload=b"payload", media_type="text/plain"
        )


def test_identical_bytes_with_different_hashed_metadata_fails_loudly(storage):
    storage.insert_opaque_artifact(
        artifact_type="fixture-a", payload=b"payload", media_type="application/octet-stream"
    )
    with pytest.raises(ArtifactMetadataConflict):
        storage.insert_opaque_artifact(
            artifact_type="fixture-b", payload=b"payload", media_type="application/octet-stream"
        )


def test_import_has_no_database_side_effect(tmp_path: Path):
    repository_root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import pathlib; before={p.name for p in pathlib.Path('.').iterdir()}; "
            "import trinity.ledger; after={p.name for p in pathlib.Path('.').iterdir()}; "
            "assert before == after, (before, after)",
        ],
        cwd=tmp_path,
        env={"PYTHONPATH": str(repository_root)},
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout == ""
