"""SQLite bootstrap and immutable artifact storage for Ledger Foundation V1."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .canonical import (
    CANONICAL_JSON_V1,
    IDENTITY,
    canonicalize_json,
    canonicalize_json_document,
    canonicalize_opaque,
)
from .errors import (
    ArtifactIntegrityError,
    ArtifactMetadataConflict,
    CanonicalizationError,
    MigrationHashDrift,
    MigrationHistoryError,
    MigrationIdentityDrift,
    UnknownAppliedMigration,
    UnsupportedSchemaVersion,
)
from .schema import (
    FREEZE_IDENTIFIER,
    SCHEMA_VERSION,
    Migration,
    MigrationRegistry,
    migration_registry,
)

HASH_DOMAIN = b"TRINITY-LEDGER-V1\0"
_ARTIFACT_ID_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    sha256: str
    content_hash: str
    byte_length: int
    media_type: str
    storage_uri: str
    created_at: str
    artifact_kind: str
    canonicalization_version: str
    payload: bytes


def artifact_preimage(
    artifact_type: str, canonicalization_version: str, payload_bytes: bytes
) -> bytes:
    """Build the frozen, domain-separated artifact hash preimage."""

    for name, value in (
        ("artifact_type", artifact_type),
        ("canonicalization_version", canonicalization_version),
    ):
        if not isinstance(value, str) or not value or "\0" in value:
            raise ArtifactIntegrityError(f"{name} must be non-empty UTF-8 text without NUL")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ArtifactIntegrityError(f"{name} is not valid UTF-8 text") from exc
    if not isinstance(payload_bytes, bytes):
        raise ArtifactIntegrityError("hashed payload must be canonical bytes")
    return b"".join(
        (
            HASH_DOMAIN,
            artifact_type.encode("utf-8"),
            b"\0",
            canonicalization_version.encode("utf-8"),
            b"\0",
            payload_bytes,
        )
    )


def artifact_id_for(
    artifact_type: str, canonicalization_version: str, payload_bytes: bytes
) -> str:
    digest = hashlib.sha256(
        artifact_preimage(artifact_type, canonicalization_version, payload_bytes)
    ).hexdigest()
    return f"sha256:{digest}"


class LedgerStorage:
    """An explicitly opened connection; importing this module has no side effects."""

    def __init__(self, connection: sqlite3.Connection, path: Path):
        self.connection = connection
        self.path = path

    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        registry: MigrationRegistry | None = None,
    ) -> "LedgerStorage":
        database_path = Path(path)
        connection = sqlite3.connect(database_path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            _configure_connection(connection)
            storage = cls(connection, database_path)
            selected_registry = registry if registry is not None else migration_registry()
            storage.apply_migrations(selected_registry)
            return storage
        except Exception:
            connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "LedgerStorage":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def apply_migration(self, migration: Migration) -> bool:
        """Compatibility helper for a one-entry bootstrap registry."""

        return self.apply_migrations(MigrationRegistry((migration,))) > 0

    def apply_migrations(self, registry: MigrationRegistry) -> int:
        """Validate complete history, then apply pending migrations in order."""

        applied = self._read_migration_history()
        _validate_migration_history(applied, registry)
        if applied:
            self._validate_schema(registry.bootstrap)

        applied_count = 0
        for migration in registry.migrations[len(applied) :]:
            self._apply_pending_migration(migration, bootstrap=(migration.sequence == 1))
            applied_count += 1

        final_history = self._read_migration_history()
        _validate_migration_history(final_history, registry)
        self._validate_schema(registry.bootstrap)
        return applied_count

    def current_migration_level(self) -> int:
        """Derive the current level from immutable migration history."""

        history = self._read_migration_history()
        return int(history[-1]["schema_version"]) if history else 0

    def _apply_pending_migration(self, migration: Migration, *, bootstrap: bool) -> None:
        applied_at = _utc_now()
        statements = [
            "BEGIN IMMEDIATE;",
            migration.text,
            "INSERT INTO schema_migration "
            "(migration_id, schema_version, sha256, applied_at) VALUES "
            f"({_sql_literal(migration.migration_id)}, {migration.sequence}, "
            f"{_sql_literal(migration.sha256)}, {_sql_literal(applied_at)});",
        ]
        if bootstrap:
            statements.append(
                "INSERT INTO schema_metadata "
                "(singleton_id, schema_version, freeze_identifier, created_at, "
                "applied_migration_id) VALUES "
                f"(1, {SCHEMA_VERSION}, {_sql_literal(FREEZE_IDENTIFIER)}, "
                f"{_sql_literal(applied_at)}, {_sql_literal(migration.migration_id)});"
            )
        statements.append("COMMIT;")

        try:
            self.connection.executescript("\n".join(statements))
        except sqlite3.DatabaseError:
            if self.connection.in_transaction:
                self.connection.rollback()
            raise

    def insert_artifact(
        self,
        *,
        artifact_type: str,
        canonicalization_version: str,
        payload: bytes,
        media_type: str,
        claimed_artifact_id: str | None = None,
    ) -> Artifact:
        canonical_payload = _validate_canonical_payload(canonicalization_version, payload)
        artifact_id = artifact_id_for(
            artifact_type, canonicalization_version, canonical_payload
        )
        if claimed_artifact_id is not None and claimed_artifact_id != artifact_id:
            raise ArtifactIntegrityError(
                f"claimed artifact ID {claimed_artifact_id!r} does not match {artifact_id!r}"
            )
        if not _ARTIFACT_ID_RE.fullmatch(artifact_id):  # defensive invariant
            raise ArtifactIntegrityError("computed artifact ID is malformed")

        raw_sha256 = hashlib.sha256(canonical_payload).hexdigest()
        existing = self.connection.execute(
            "SELECT * FROM artifact WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        if existing is not None:
            artifact = _artifact_from_row(existing)
            self._assert_artifact_metadata(
                artifact,
                artifact_type=artifact_type,
                canonicalization_version=canonicalization_version,
                media_type=media_type,
            )
            self.verify_artifact(artifact)
            return artifact

        same_bytes = self.connection.execute(
            "SELECT * FROM artifact WHERE sha256 = ? AND byte_length = ?",
            (raw_sha256, len(canonical_payload)),
        ).fetchone()
        if same_bytes is not None:
            raise ArtifactMetadataConflict(
                "identical bytes are already bound to different immutable artifact metadata"
            )

        created_at = _utc_now()
        storage_uri = f"sqlite:artifact:{artifact_id}"
        try:
            with self.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO artifact (
                        artifact_id, sha256, content_hash, byte_length, media_type,
                        storage_uri, created_at, artifact_kind,
                        canonicalization_version, payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact_id,
                        raw_sha256,
                        artifact_id.removeprefix("sha256:"),
                        len(canonical_payload),
                        media_type,
                        storage_uri,
                        created_at,
                        artifact_type,
                        canonicalization_version,
                        canonical_payload,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ArtifactMetadataConflict("artifact immutable metadata conflicts") from exc
        return self.get_artifact(artifact_id)

    def insert_json_artifact(
        self, *, artifact_type: str, value: Any, media_type: str = "application/json"
    ) -> Artifact:
        return self.insert_artifact(
            artifact_type=artifact_type,
            canonicalization_version=CANONICAL_JSON_V1,
            payload=canonicalize_json(value),
            media_type=media_type,
        )

    def insert_opaque_artifact(
        self, *, artifact_type: str, payload: bytes, media_type: str
    ) -> Artifact:
        return self.insert_artifact(
            artifact_type=artifact_type,
            canonicalization_version=IDENTITY,
            payload=payload,
            media_type=media_type,
        )

    def get_artifact(self, artifact_id: str, *, verify: bool = True) -> Artifact:
        if not _ARTIFACT_ID_RE.fullmatch(artifact_id):
            raise ArtifactIntegrityError("artifact ID must be sha256:<64 lowercase hex>")
        row = self.connection.execute(
            "SELECT * FROM artifact WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        if row is None:
            raise KeyError(artifact_id)
        artifact = _artifact_from_row(row)
        if verify:
            self.verify_artifact(artifact)
        return artifact

    @staticmethod
    def verify_artifact(artifact: Artifact) -> None:
        if artifact.byte_length != len(artifact.payload):
            raise ArtifactIntegrityError("artifact byte_length does not match stored payload")
        raw_sha256 = hashlib.sha256(artifact.payload).hexdigest()
        if artifact.sha256 != raw_sha256:
            raise ArtifactIntegrityError("artifact SHA-256 does not match stored payload")
        expected_id = artifact_id_for(
            artifact.artifact_kind,
            artifact.canonicalization_version,
            artifact.payload,
        )
        if artifact.artifact_id != expected_id:
            raise ArtifactIntegrityError("artifact ID does not match the frozen hash preimage")
        if artifact.content_hash != expected_id.removeprefix("sha256:"):
            raise ArtifactIntegrityError("artifact content_hash does not match artifact ID")
        if artifact.storage_uri != f"sqlite:artifact:{artifact.artifact_id}":
            raise ArtifactIntegrityError("artifact storage URI does not match content address")
        try:
            _validate_canonical_payload(artifact.canonicalization_version, artifact.payload)
        except CanonicalizationError as exc:
            raise ArtifactIntegrityError("stored artifact payload is not canonical") from exc

    def _read_migration_history(self) -> list[sqlite3.Row]:
        table_exists = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migration'"
        ).fetchone()
        if table_exists is None:
            return []
        return self.connection.execute(
            """
            SELECT migration_id, schema_version, sha256
            FROM schema_migration
            ORDER BY schema_version
            """
        ).fetchall()

    def _validate_schema(self, bootstrap: Migration) -> None:
        row = self.connection.execute(
            """
            SELECT schema_version, freeze_identifier, applied_migration_id
            FROM schema_metadata
            WHERE singleton_id = 1
            """
        ).fetchone()
        if row is None or row["schema_version"] != SCHEMA_VERSION:
            raise UnsupportedSchemaVersion("missing or unsupported schema_metadata version")
        if row["freeze_identifier"] != FREEZE_IDENTIFIER:
            raise UnsupportedSchemaVersion("database freeze identifier is unsupported")
        if row["applied_migration_id"] != bootstrap.migration_id:
            raise MigrationIdentityDrift(
                "schema_metadata bootstrap migration identity does not match registry"
            )

    @staticmethod
    def _assert_artifact_metadata(
        artifact: Artifact,
        *,
        artifact_type: str,
        canonicalization_version: str,
        media_type: str,
    ) -> None:
        expected = (artifact_type, canonicalization_version, media_type)
        actual = (
            artifact.artifact_kind,
            artifact.canonicalization_version,
            artifact.media_type,
        )
        if actual != expected:
            raise ArtifactMetadataConflict(
                f"artifact metadata conflict: stored={actual!r}, requested={expected!r}"
            )


def _configure_connection(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise sqlite3.DatabaseError("SQLite foreign key enforcement could not be enabled")
    journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if str(journal_mode).lower() != "wal":
        raise sqlite3.DatabaseError("SQLite WAL journal mode could not be enabled")
    connection.execute("PRAGMA synchronous = FULL")


def _validate_canonical_payload(version: str, payload: bytes) -> bytes:
    if version == IDENTITY:
        return canonicalize_opaque(payload)
    if version == CANONICAL_JSON_V1:
        supplied = canonicalize_opaque(payload)
        canonical = canonicalize_json_document(supplied)
        if canonical != supplied:
            raise CanonicalizationError(
                "structured artifact payload must already be canonical UTF-8 JSON"
            )
        return canonical
    raise CanonicalizationError(f"unsupported canonicalization version: {version!r}")


def _validate_migration_history(
    rows: list[sqlite3.Row], registry: MigrationRegistry
) -> None:
    sequences = [int(row["schema_version"]) for row in rows]
    expected_sequences = list(range(1, len(rows) + 1))
    if sequences != expected_sequences:
        raise MigrationHistoryError(
            f"applied migration history must start at 1 without gaps: got {sequences}"
        )
    if len(rows) > len(registry):
        unknown = rows[len(registry)]["migration_id"]
        raise UnknownAppliedMigration(f"unknown applied migration: {unknown!r}")

    registered_positions = {
        migration.migration_id: migration.sequence for migration in registry
    }
    for row, expected in zip(rows, registry.migrations):
        actual_id = row["migration_id"]
        if actual_id != expected.migration_id:
            actual_position = registered_positions.get(actual_id)
            if actual_position is not None:
                raise MigrationHistoryError(
                    f"reordered migration history: {actual_id!r} is at sequence "
                    f"{row['schema_version']}, expected {actual_position}"
                )
            raise MigrationIdentityDrift(
                f"migration sequence {expected.sequence} has identity {actual_id!r}; "
                f"expected {expected.migration_id!r}"
            )
        if row["sha256"] != expected.sha256:
            raise MigrationHashDrift(
                f"migration {expected.migration_id!r} hash differs from registry"
            )


def _artifact_from_row(row: sqlite3.Row) -> Artifact:
    return Artifact(**{field: row[field] for field in Artifact.__dataclass_fields__})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
