"""SQLite storage for Ledger artifacts and V1.1 execution coordination."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
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
    FinalizationConflict,
    InvalidAttemptTransition,
    InvalidObservationProvenance,
    RequestIdempotencyConflict,
    StaleAttemptError,
    UnsupportedAvailabilityBasis,
    UnsupportedRunInput,
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
_SOURCE_ID_RE = re.compile(
    r"[a-z0-9][a-z0-9._-]*:[a-z0-9][a-z0-9._-]*(?::[a-z0-9][a-z0-9._-]*)?\Z"
)
_SOURCE_METADATA_KEYS = frozenset(
    {
        "schema_version",
        "provider",
        "dataset_name",
        "source_record_key",
        "acquisition_method",
        "availability_rule_id",
        "availability_rule_version",
        "evidence_artifact_ids",
        "provider_metadata",
        "future_effective_at",
    }
)


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


@dataclass(frozen=True)
class RunRequest:
    run_request_id: str
    request_kind: str
    requested_at: str
    analysis_cutoff_at: str
    parameters_json: str
    idempotency_key: str
    requested_by: str
    baseline_commit: str


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    run_request_id: str
    attempt_ordinal: int
    fence_token: int
    started_at: str
    finished_at: str | None
    status: str
    worker_identity: str
    code_commit: str
    environment_fingerprint: str
    failure_class: str | None
    failure_message: str | None


@dataclass(frozen=True)
class AttemptEvent:
    attempt_event_id: str
    attempt_id: str
    sequence_number: int
    event_type: str
    event_at: str
    from_status: str | None
    to_status: str
    reason_code: str | None
    failure_class: str | None
    failure_message: str | None
    actor_identity: str
    details_artifact_id: str | None
    created_at: str


@dataclass(frozen=True)
class Run:
    run_id: str
    run_request_id: str
    attempt_id: str
    committed_at: str
    status: str
    result_manifest_artifact_id: str


@dataclass(frozen=True)
class InputObservation:
    input_observation_id: str
    artifact_id: str
    source_id: str
    source_record_key: str
    source_published_at: str | None
    retrieved_at: str
    availability_basis: str
    effective_available_at: str
    observed_by_attempt_id: str
    source_metadata_json: str


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
        self._internal_write_depth = 0
        self.connection.create_function(
            "ledger_internal_write_authorized",
            0,
            lambda: int(self._internal_write_depth > 0),
        )

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

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        if self.connection.in_transaction:
            yield self.connection
        else:
            with self.transaction() as connection:
                yield connection

    @contextmanager
    def _internal_write(self) -> Iterator[None]:
        self._internal_write_depth += 1
        try:
            yield
        finally:
            self._internal_write_depth -= 1

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
            with self._write_transaction() as connection:
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

    def create_input_observation(
        self,
        *,
        artifact_id: str,
        source_id: str,
        source_record_key: str,
        source_published_at: str | None,
        retrieved_at: str,
        availability_basis: str,
        effective_available_at: str,
        observed_by_attempt_id: str,
        fence_token: int,
        source_metadata: Any,
    ) -> InputObservation:
        """Insert one immutable, conservatively available observation."""

        if availability_basis != "RETRIEVED_AT_FALLBACK":
            if availability_basis in {"SOURCE_PUBLISHED_AT", "LEGACY_ASSERTED_AT"}:
                raise UnsupportedAvailabilityBasis(
                    f"{availability_basis} is frozen but deferred in Milestone 3A"
                )
            raise InvalidObservationProvenance(
                f"unknown availability basis: {availability_basis!r}"
            )
        _validate_source_id(source_id)
        _require_text("source_record_key", source_record_key)
        _require_timestamp("retrieved_at", retrieved_at)
        _require_timestamp("effective_available_at", effective_available_at)
        if source_published_at is not None:
            raise InvalidObservationProvenance(
                "RETRIEVED_AT_FALLBACK requires source_published_at to be null"
            )
        if effective_available_at != retrieved_at:
            raise InvalidObservationProvenance(
                "RETRIEVED_AT_FALLBACK requires effective_available_at = retrieved_at exactly"
            )
        metadata_json, evidence_ids = _validate_source_metadata(
            source_metadata,
            source_id=source_id,
            source_record_key=source_record_key,
        )
        observation_id = _new_id()
        with self.transaction() as connection, self._internal_write():
            self._require_attempt_authority(observed_by_attempt_id, fence_token)
            self.get_artifact(artifact_id)
            if evidence_ids:
                placeholders = ",".join("?" for _ in evidence_ids)
                attached = {
                    row[0]
                    for row in connection.execute(
                        f"""
                        SELECT artifact_id FROM attempt_artifact
                        WHERE attempt_id = ?
                          AND role IN ('DIAGNOSTIC', 'OUTPUT')
                          AND artifact_id IN ({placeholders})
                        """,
                        (observed_by_attempt_id, *evidence_ids),
                    )
                }
                if attached != set(evidence_ids):
                    raise InvalidObservationProvenance(
                        "every evidence artifact must be attached to the observing attempt "
                        "as DIAGNOSTIC or OUTPUT"
                    )
            connection.execute(
                """
                INSERT INTO input_observation (
                    input_observation_id, artifact_id, source_id, source_record_key,
                    source_published_at, retrieved_at, availability_basis,
                    effective_available_at, observed_by_attempt_id, source_metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation_id,
                    artifact_id,
                    source_id,
                    source_record_key,
                    None,
                    retrieved_at,
                    availability_basis,
                    effective_available_at,
                    observed_by_attempt_id,
                    metadata_json,
                ),
            )
        return self.get_input_observation(observation_id)

    def get_input_observation(self, input_observation_id: str) -> InputObservation:
        row = self.connection.execute(
            "SELECT * FROM input_observation WHERE input_observation_id = ?",
            (input_observation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(input_observation_id)
        observation = _input_observation_from_row(row)
        canonical = canonicalize_json_document(observation.source_metadata_json).decode(
            "utf-8"
        )
        if canonical != observation.source_metadata_json:
            raise ArtifactIntegrityError("observation source metadata is not canonical JSON")
        return observation

    def list_input_observations(
        self, *, artifact_id: str | None = None
    ) -> list[InputObservation]:
        if artifact_id is None:
            rows = self.connection.execute(
                "SELECT * FROM input_observation ORDER BY input_observation_id"
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT * FROM input_observation
                WHERE artifact_id = ? ORDER BY input_observation_id
                """,
                (artifact_id,),
            ).fetchall()
        return [_input_observation_from_row(row) for row in rows]

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

    def create_run_request(
        self,
        *,
        request_kind: str,
        analysis_cutoff_at: str,
        parameters: Any,
        idempotency_key: str,
        requested_by: str,
        baseline_commit: str,
    ) -> RunRequest:
        """Create or replay one caller-scoped immutable logical request."""

        _require_text("request_kind", request_kind)
        _require_timestamp("analysis_cutoff_at", analysis_cutoff_at)
        _require_text("idempotency_key", idempotency_key)
        _require_text("requested_by", requested_by)
        _require_text("baseline_commit", baseline_commit)
        parameters_json = canonicalize_json(parameters).decode("utf-8")
        fingerprint = _request_fingerprint(
            request_kind=request_kind,
            analysis_cutoff_at=analysis_cutoff_at,
            parameters_json=parameters_json,
            baseline_commit=baseline_commit,
        )

        with self.transaction() as connection, self._internal_write():
            existing = connection.execute(
                "SELECT * FROM run_request WHERE requested_by = ? AND idempotency_key = ?",
                (requested_by, idempotency_key),
            ).fetchone()
            if existing is not None:
                request = _run_request_from_row(existing)
                if _request_fingerprint_from_request(request) != fingerprint:
                    raise RequestIdempotencyConflict(
                        "caller-scoped idempotency key has incompatible request semantics"
                    )
                return request

            request_id = _new_id()
            requested_at = _utc_now()
            connection.execute(
                """
                INSERT INTO run_request (
                    run_request_id, request_kind, requested_at, analysis_cutoff_at,
                    parameters_json, idempotency_key, requested_by, baseline_commit
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    request_kind,
                    requested_at,
                    analysis_cutoff_at,
                    parameters_json,
                    idempotency_key,
                    requested_by,
                    baseline_commit,
                ),
            )
            connection.execute(
                """
                INSERT INTO run_request_control (
                    run_request_id, current_attempt_id, current_fence_token,
                    request_state, committed_run_id, updated_at
                ) VALUES (?, NULL, 0, 'ACTIVE', NULL, ?)
                """,
                (request_id, requested_at),
            )
        return self.get_run_request(request_id)

    def get_run_request(self, run_request_id: str) -> RunRequest:
        row = self.connection.execute(
            "SELECT * FROM run_request WHERE run_request_id = ?", (run_request_id,)
        ).fetchone()
        if row is None:
            raise KeyError(run_request_id)
        return _run_request_from_row(row)

    def allocate_attempt(
        self,
        *,
        run_request_id: str,
        worker_identity: str,
        code_commit: str,
        environment_fingerprint: str,
    ) -> Attempt:
        """Atomically allocate the next ordinal and fencing token for a request."""

        _require_text("worker_identity", worker_identity)
        _require_text("code_commit", code_commit)
        _require_text("environment_fingerprint", environment_fingerprint)
        now = _utc_now()
        with self.transaction() as connection, self._internal_write():
            control = connection.execute(
                "SELECT * FROM run_request_control WHERE run_request_id = ?",
                (run_request_id,),
            ).fetchone()
            if control is None:
                raise KeyError(run_request_id)
            if control["request_state"] != "ACTIVE":
                raise InvalidAttemptTransition("a committed request cannot allocate attempts")

            current_id = control["current_attempt_id"]
            if current_id is not None:
                current = self._get_attempt_row(current_id)
                self._append_attempt_event(
                    attempt_id=current_id,
                    event_type="AUTHORITY_REVOKED",
                    event_at=now,
                    from_status=current["status"],
                    to_status=current["status"],
                    reason_code="SUPERSEDED_BY_RETRY",
                    failure_class=None,
                    failure_message=None,
                    actor_identity=worker_identity,
                    details_artifact_id=None,
                )
                if current["status"] == "RUNNING":
                    self._append_attempt_event(
                        attempt_id=current_id,
                        event_type="ABORTED",
                        event_at=now,
                        from_status="RUNNING",
                        to_status="ABORTED",
                        reason_code="SUPERSEDED_BY_RETRY",
                        failure_class=None,
                        failure_message=None,
                        actor_identity=worker_identity,
                        details_artifact_id=None,
                    )
                    connection.execute(
                        """
                        UPDATE attempt
                        SET status = 'ABORTED', finished_at = ?,
                            failure_class = NULL, failure_message = NULL
                        WHERE attempt_id = ?
                        """,
                        (now, current_id),
                    )

            attempt_ordinal = int(
                connection.execute(
                    "SELECT COALESCE(MAX(attempt_ordinal), 0) + 1 FROM attempt WHERE run_request_id = ?",
                    (run_request_id,),
                ).fetchone()[0]
            )
            fence_token = int(control["current_fence_token"]) + 1
            attempt_id = _new_id(excluding={run_request_id})
            connection.execute(
                """
                INSERT INTO attempt (
                    attempt_id, run_request_id, attempt_ordinal, fence_token,
                    started_at, finished_at, status, worker_identity, code_commit,
                    environment_fingerprint, failure_class, failure_message
                ) VALUES (?, ?, ?, ?, ?, NULL, 'RUNNING', ?, ?, ?, NULL, NULL)
                """,
                (
                    attempt_id,
                    run_request_id,
                    attempt_ordinal,
                    fence_token,
                    now,
                    worker_identity,
                    code_commit,
                    environment_fingerprint,
                ),
            )
            self._append_attempt_event(
                attempt_id=attempt_id,
                event_type="ALLOCATED",
                event_at=now,
                from_status=None,
                to_status="RUNNING",
                reason_code=None,
                failure_class=None,
                failure_message=None,
                actor_identity=worker_identity,
                details_artifact_id=None,
            )
            connection.execute(
                """
                UPDATE run_request_control
                SET current_attempt_id = ?, current_fence_token = ?, updated_at = ?
                WHERE run_request_id = ?
                """,
                (attempt_id, fence_token, now, run_request_id),
            )
        return self.get_attempt(attempt_id)

    def get_attempt(self, attempt_id: str) -> Attempt:
        return _attempt_from_row(self._get_attempt_row(attempt_id))

    def list_attempt_events(self, attempt_id: str) -> list[AttemptEvent]:
        rows = self.connection.execute(
            """
            SELECT * FROM attempt_event
            WHERE attempt_id = ? ORDER BY sequence_number
            """,
            (attempt_id,),
        ).fetchall()
        return [_attempt_event_from_row(row) for row in rows]

    def is_attempt_authorized(self, attempt_id: str, fence_token: int) -> bool:
        row = self.connection.execute(
            """
            SELECT 1
            FROM attempt
            JOIN run_request_control USING (run_request_id)
            WHERE attempt.attempt_id = ?
              AND attempt.fence_token = ?
              AND attempt.status = 'RUNNING'
              AND run_request_control.request_state = 'ACTIVE'
              AND run_request_control.current_attempt_id = attempt.attempt_id
              AND run_request_control.current_fence_token = attempt.fence_token
            """,
            (attempt_id, fence_token),
        ).fetchone()
        return row is not None

    def require_attempt_authority(self, attempt_id: str, fence_token: int) -> Attempt:
        row = self._require_attempt_authority(attempt_id, fence_token)
        return _attempt_from_row(row)

    def terminalize_attempt(
        self,
        *,
        attempt_id: str,
        fence_token: int,
        status: str,
        actor_identity: str,
        reason_code: str | None = None,
        failure_class: str | None = None,
        failure_message: str | None = None,
        details_artifact_id: str | None = None,
    ) -> Attempt:
        """Atomically record FAILED or ABORTED; success is commit_run-only."""

        if status not in {"FAILED", "ABORTED"}:
            raise InvalidAttemptTransition(
                "only FAILED or ABORTED may terminalize outside committed-run transaction"
            )
        _require_text("actor_identity", actor_identity)
        if status == "FAILED":
            _require_text("failure_class", failure_class)
        elif failure_class is not None or failure_message is not None:
            raise InvalidAttemptTransition("ABORTED forbids failure fields")
        if status == "ABORTED":
            _require_text("reason_code", reason_code)

        with self.transaction() as connection, self._internal_write():
            row = self._get_attempt_row(attempt_id)
            if row["status"] != "RUNNING":
                if self._terminalization_matches(
                    row,
                    status=status,
                    reason_code=reason_code,
                    failure_class=failure_class,
                    failure_message=failure_message,
                    actor_identity=actor_identity,
                    details_artifact_id=details_artifact_id,
                ):
                    return _attempt_from_row(row)
                raise InvalidAttemptTransition(
                    f"attempt is already terminal with status {row['status']}"
                )
            self._require_attempt_authority(attempt_id, fence_token)
            if details_artifact_id is not None:
                self.get_artifact(details_artifact_id)
            finished_at = _utc_now()
            self._append_attempt_event(
                attempt_id=attempt_id,
                event_type=status,
                event_at=finished_at,
                from_status="RUNNING",
                to_status=status,
                reason_code=reason_code,
                failure_class=failure_class,
                failure_message=failure_message,
                actor_identity=actor_identity,
                details_artifact_id=details_artifact_id,
            )
            connection.execute(
                """
                UPDATE attempt
                SET status = ?, finished_at = ?, failure_class = ?, failure_message = ?
                WHERE attempt_id = ?
                """,
                (status, finished_at, failure_class, failure_message, attempt_id),
            )
        return self.get_attempt(attempt_id)

    def attach_attempt_artifact(
        self,
        *,
        attempt_id: str,
        fence_token: int,
        artifact_id: str,
        role: str,
    ) -> str:
        if role == "RESULT_MANIFEST":
            raise InvalidAttemptTransition("result manifests attach only during run commit")
        if role not in {
            "OUTPUT",
            "FAILED_ATTEMPT_OUTPUT",
            "LOG",
            "DIAGNOSTIC",
            "PARTIAL_OUTPUT",
        }:
            raise ValueError(f"unsupported attempt artifact role: {role!r}")
        with self.transaction(), self._internal_write():
            self._require_attempt_authority(attempt_id, fence_token)
            self.get_artifact(artifact_id)
            return self._attach_attempt_artifact(attempt_id, artifact_id, role, _utc_now())

    def commit_run(
        self,
        *,
        attempt_id: str,
        fence_token: int,
        run_id: str | None = None,
        output_artifact_ids: tuple[str, ...] = (),
        run_inputs: tuple[object, ...] = (),
    ) -> Run:
        """Atomically commit a minimal successful run and canonical manifest."""

        if run_inputs:
            raise UnsupportedRunInput(
                "non-empty run inputs require the deferred observation and derivation schema"
            )
        if len(output_artifact_ids) != len(set(output_artifact_ids)):
            raise FinalizationConflict("output artifact IDs must be unique")
        sorted_outputs = tuple(sorted(output_artifact_ids))
        attempt = self.get_attempt(attempt_id)
        request = self.get_run_request(attempt.run_request_id)
        existing_row = self.connection.execute(
            "SELECT * FROM run WHERE attempt_id = ? OR run_request_id = ?",
            (attempt_id, attempt.run_request_id),
        ).fetchone()
        effective_run_id = run_id or (existing_row["run_id"] if existing_row else _new_id())
        if effective_run_id in {attempt_id, attempt.run_request_id}:
            raise FinalizationConflict("request, attempt, and run IDs must be pairwise unequal")
        manifest_value = _result_manifest_value(
            run_id=effective_run_id,
            request=request,
            attempt=attempt,
            output_artifact_ids=sorted_outputs,
        )
        manifest_payload = canonicalize_json(manifest_value)
        manifest_id = artifact_id_for(
            "ledger.run-result-manifest.v1", CANONICAL_JSON_V1, manifest_payload
        )
        if existing_row is not None:
            existing = _run_from_row(existing_row)
            if (
                existing.run_id == effective_run_id
                and existing.attempt_id == attempt_id
                and existing.result_manifest_artifact_id == manifest_id
            ):
                return self.get_run(existing.run_id)
            raise FinalizationConflict("request or attempt already has incompatible run closure")

        with self.transaction() as connection, self._internal_write():
            attempt = _attempt_from_row(self._require_attempt_authority(attempt_id, fence_token))
            attached_outputs = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT artifact_id FROM attempt_artifact
                    WHERE attempt_id = ? AND role = 'OUTPUT'
                    """,
                    (attempt_id,),
                )
            }
            if attached_outputs != set(sorted_outputs):
                raise FinalizationConflict(
                    "manifest output IDs must equal the attempt's attached OUTPUT closure"
                )
            for artifact_id in sorted_outputs:
                self.get_artifact(artifact_id)
            manifest = self.insert_artifact(
                artifact_type="ledger.run-result-manifest.v1",
                canonicalization_version=CANONICAL_JSON_V1,
                payload=manifest_payload,
                media_type="application/json",
                claimed_artifact_id=manifest_id,
            )
            committed_at = _utc_now()
            self._attach_attempt_artifact(
                attempt_id, manifest.artifact_id, "RESULT_MANIFEST", committed_at
            )
            connection.execute(
                """
                INSERT INTO run (
                    run_id, run_request_id, attempt_id, committed_at, status,
                    result_manifest_artifact_id
                ) VALUES (?, ?, ?, ?, 'COMMITTED', ?)
                """,
                (
                    effective_run_id,
                    attempt.run_request_id,
                    attempt_id,
                    committed_at,
                    manifest.artifact_id,
                ),
            )
            self._append_attempt_event(
                attempt_id=attempt_id,
                event_type="SUCCEEDED",
                event_at=committed_at,
                from_status="RUNNING",
                to_status="SUCCEEDED",
                reason_code=None,
                failure_class=None,
                failure_message=None,
                actor_identity=attempt.worker_identity,
                details_artifact_id=None,
            )
            connection.execute(
                """
                UPDATE attempt
                SET status = 'SUCCEEDED', finished_at = ?,
                    failure_class = NULL, failure_message = NULL
                WHERE attempt_id = ?
                """,
                (committed_at, attempt_id),
            )
            connection.execute(
                """
                UPDATE run_request_control
                SET request_state = 'COMMITTED', committed_run_id = ?, updated_at = ?
                WHERE run_request_id = ?
                """,
                (effective_run_id, committed_at, attempt.run_request_id),
            )
        return self.get_run(effective_run_id)

    def get_run(self, run_id: str) -> Run:
        row = self.connection.execute(
            "SELECT * FROM run WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        run = _run_from_row(row)
        manifest = self.get_artifact(run.result_manifest_artifact_id)
        if manifest.artifact_kind != "ledger.run-result-manifest.v1":
            raise ArtifactIntegrityError("run manifest has incorrect artifact kind")
        value = json.loads(manifest.payload)
        output_ids = value.get("output_artifact_ids")
        if not isinstance(output_ids, list) or not all(
            isinstance(item, str) for item in output_ids
        ):
            raise ArtifactIntegrityError("run manifest output IDs are malformed")
        expected = _result_manifest_value(
            run_id=run.run_id,
            request=self.get_run_request(run.run_request_id),
            attempt=self.get_attempt(run.attempt_id),
            output_artifact_ids=tuple(output_ids),
        )
        if value != expected or output_ids != sorted(set(output_ids)):
            raise ArtifactIntegrityError("run manifest does not match committed closure")
        manifest_link = self.connection.execute(
            """
            SELECT 1 FROM attempt_artifact
            WHERE attempt_id = ? AND artifact_id = ? AND role = 'RESULT_MANIFEST'
            """,
            (run.attempt_id, run.result_manifest_artifact_id),
        ).fetchone()
        if manifest_link is None:
            raise ArtifactIntegrityError("run manifest is not attached to its attempt")
        attached_outputs = {
            row[0]
            for row in self.connection.execute(
                """
                SELECT artifact_id FROM attempt_artifact
                WHERE attempt_id = ? AND role = 'OUTPUT'
                """,
                (run.attempt_id,),
            )
        }
        if set(output_ids) != attached_outputs:
            raise ArtifactIntegrityError("run manifest output closure is incomplete")
        return run

    def _get_attempt_row(self, attempt_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM attempt WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise KeyError(attempt_id)
        return row

    def _require_attempt_authority(
        self, attempt_id: str, fence_token: int
    ) -> sqlite3.Row:
        row = self.connection.execute(
            """
            SELECT attempt.*
            FROM attempt
            JOIN run_request_control USING (run_request_id)
            WHERE attempt.attempt_id = ?
              AND attempt.fence_token = ?
              AND attempt.status = 'RUNNING'
              AND run_request_control.request_state = 'ACTIVE'
              AND run_request_control.current_attempt_id = attempt.attempt_id
              AND run_request_control.current_fence_token = attempt.fence_token
            """,
            (attempt_id, fence_token),
        ).fetchone()
        if row is None:
            raise StaleAttemptError("attempt does not hold current write authority")
        return row

    def _append_attempt_event(
        self,
        *,
        attempt_id: str,
        event_type: str,
        event_at: str,
        from_status: str | None,
        to_status: str,
        reason_code: str | None,
        failure_class: str | None,
        failure_message: str | None,
        actor_identity: str,
        details_artifact_id: str | None,
    ) -> str:
        sequence = int(
            self.connection.execute(
                "SELECT COALESCE(MAX(sequence_number), 0) + 1 FROM attempt_event WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()[0]
        )
        event_id = _new_id()
        self.connection.execute(
            """
            INSERT INTO attempt_event (
                attempt_event_id, attempt_id, sequence_number, event_type,
                event_at, from_status, to_status, reason_code, failure_class,
                failure_message, actor_identity, details_artifact_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                attempt_id,
                sequence,
                event_type,
                event_at,
                from_status,
                to_status,
                reason_code,
                failure_class,
                failure_message,
                actor_identity,
                details_artifact_id,
                _utc_now(),
            ),
        )
        return event_id

    def _attach_attempt_artifact(
        self, attempt_id: str, artifact_id: str, role: str, attached_at: str
    ) -> str:
        existing = self.connection.execute(
            """
            SELECT attempt_artifact_id FROM attempt_artifact
            WHERE attempt_id = ? AND artifact_id = ? AND role = ?
            """,
            (attempt_id, artifact_id, role),
        ).fetchone()
        if existing is not None:
            return str(existing[0])
        ordinal = int(
            self.connection.execute(
                """
                SELECT COALESCE(MAX(ordinal), 0) + 1 FROM attempt_artifact
                WHERE attempt_id = ? AND role = ?
                """,
                (attempt_id, role),
            ).fetchone()[0]
        )
        link_id = _new_id()
        self.connection.execute(
            """
            INSERT INTO attempt_artifact (
                attempt_artifact_id, attempt_id, artifact_id, role, attached_at, ordinal
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (link_id, attempt_id, artifact_id, role, attached_at, ordinal),
        )
        return link_id

    def _terminalization_matches(
        self,
        row: sqlite3.Row,
        *,
        status: str,
        reason_code: str | None,
        failure_class: str | None,
        failure_message: str | None,
        actor_identity: str,
        details_artifact_id: str | None,
    ) -> bool:
        if row["status"] != status:
            return False
        event = self.connection.execute(
            """
            SELECT * FROM attempt_event
            WHERE attempt_id = ? AND event_type = ?
            ORDER BY sequence_number DESC LIMIT 1
            """,
            (row["attempt_id"], status),
        ).fetchone()
        return event is not None and (
            event["reason_code"],
            event["failure_class"],
            event["failure_message"],
            event["actor_identity"],
            event["details_artifact_id"],
        ) == (
            reason_code,
            failure_class,
            failure_message,
            actor_identity,
            details_artifact_id,
        )

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


def _run_request_from_row(row: sqlite3.Row) -> RunRequest:
    return RunRequest(**{field: row[field] for field in RunRequest.__dataclass_fields__})


def _attempt_from_row(row: sqlite3.Row) -> Attempt:
    return Attempt(**{field: row[field] for field in Attempt.__dataclass_fields__})


def _attempt_event_from_row(row: sqlite3.Row) -> AttemptEvent:
    return AttemptEvent(
        **{field: row[field] for field in AttemptEvent.__dataclass_fields__}
    )


def _run_from_row(row: sqlite3.Row) -> Run:
    return Run(**{field: row[field] for field in Run.__dataclass_fields__})


def _input_observation_from_row(row: sqlite3.Row) -> InputObservation:
    return InputObservation(
        **{field: row[field] for field in InputObservation.__dataclass_fields__}
    )


def _request_fingerprint(
    *,
    request_kind: str,
    analysis_cutoff_at: str,
    parameters_json: str,
    baseline_commit: str,
) -> str:
    value = {
        "request_kind": request_kind,
        "analysis_cutoff_at": analysis_cutoff_at,
        "parameters_json": json.loads(parameters_json),
        "baseline_commit": baseline_commit,
    }
    return hashlib.sha256(canonicalize_json(value)).hexdigest()


def _request_fingerprint_from_request(request: RunRequest) -> str:
    return _request_fingerprint(
        request_kind=request.request_kind,
        analysis_cutoff_at=request.analysis_cutoff_at,
        parameters_json=request.parameters_json,
        baseline_commit=request.baseline_commit,
    )


def _result_manifest_value(
    *,
    run_id: str,
    request: RunRequest,
    attempt: Attempt,
    output_artifact_ids: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "manifest_version": "1",
        "run_id": run_id,
        "run_request_id": request.run_request_id,
        "attempt_id": attempt.attempt_id,
        "analysis_cutoff_at": request.analysis_cutoff_at,
        "request_kind": request.request_kind,
        "baseline_commit": request.baseline_commit,
        "attempt_code_commit": attempt.code_commit,
        "environment_fingerprint": attempt.environment_fingerprint,
        "policy_references": [],
        "input_observation_ids": [],
        "derivation_node_ids": [],
        "output_artifact_ids": list(output_artifact_ids),
        "research_ids": [],
        "setup_ids": [],
        "eligibility_ids": [],
        "trade_ids": [],
        "outcome_ids": [],
    }


def _new_id(*, excluding: set[str] | None = None) -> str:
    excluded = excluding or set()
    while True:
        value = str(uuid.uuid4())
        if value not in excluded:
            return value


def _require_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be non-empty text")


def _require_timestamp(name: str, value: object) -> None:
    _require_text(name, value)
    assert isinstance(value, str)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", value):
        raise ValueError(f"{name} must be an RFC 3339 UTC timestamp with microseconds")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as exc:
        raise ValueError(f"{name} is not a valid UTC timestamp") from exc


def _validate_source_id(source_id: object) -> None:
    if not isinstance(source_id, str) or not _SOURCE_ID_RE.fullmatch(source_id):
        raise InvalidObservationProvenance(
            "source_id must be provider:dataset[:contract-version] using lowercase "
            "ASCII components"
        )


def _validate_source_metadata(
    value: Any, *, source_id: str, source_record_key: str
) -> tuple[str, tuple[str, ...]]:
    if not isinstance(value, dict) or set(value) != _SOURCE_METADATA_KEYS:
        raise InvalidObservationProvenance(
            "source_metadata must contain exactly the frozen V1.1 fields"
        )
    if value["schema_version"] != "1":
        raise InvalidObservationProvenance("source metadata schema_version must be '1'")
    for field in (
        "provider",
        "dataset_name",
        "source_record_key",
        "acquisition_method",
        "availability_rule_id",
        "availability_rule_version",
    ):
        try:
            _require_text(f"source_metadata.{field}", value[field])
        except ValueError as exc:
            raise InvalidObservationProvenance(str(exc)) from exc
    source_parts = source_id.split(":")
    if value["provider"] != source_parts[0] or value["dataset_name"] != source_parts[1]:
        raise InvalidObservationProvenance(
            "source metadata provider and dataset_name must match source_id"
        )
    if value["source_record_key"] != source_record_key:
        raise InvalidObservationProvenance(
            "source metadata source_record_key must match the observation"
        )
    evidence = value["evidence_artifact_ids"]
    if (
        not isinstance(evidence, list)
        or not all(
            isinstance(item, str) and _ARTIFACT_ID_RE.fullmatch(item)
            for item in evidence
        )
        or evidence != sorted(set(evidence))
    ):
        raise InvalidObservationProvenance(
            "evidence_artifact_ids must be a sorted unique artifact-ID array"
        )
    if not isinstance(value["provider_metadata"], dict):
        raise InvalidObservationProvenance("provider_metadata must be a canonical object")
    _reject_operational_source_metadata(value["provider_metadata"])
    if value["future_effective_at"] is not None:
        raise InvalidObservationProvenance(
            "RETRIEVED_AT_FALLBACK requires future_effective_at to be null"
        )
    try:
        metadata_json = canonicalize_json(value).decode("utf-8")
    except CanonicalizationError as exc:
        raise InvalidObservationProvenance(
            "source metadata must use deterministic canonical Ledger JSON"
        ) from exc
    return metadata_json, tuple(evidence)


def _reject_operational_source_metadata(value: Any) -> None:
    prohibited_keys = {
        "api_key",
        "credential",
        "credentials",
        "file_path",
        "filesystem_path",
        "local_path",
        "password",
        "secret",
        "token",
    }
    for key, item in value.items():
        normalized = key.lower().replace("-", "_")
        if normalized in prohibited_keys:
            raise InvalidObservationProvenance(
                "provider_metadata must not contain secrets or mutable local locators"
            )
        if isinstance(item, dict):
            _reject_operational_source_metadata(item)
        elif _contains_local_path(item):
            raise InvalidObservationProvenance(
                "provider_metadata must not contain secrets or mutable local locators"
            )


def _contains_local_path(value: Any) -> bool:
    if isinstance(value, str):
        return bool(re.match(r"^[A-Za-z]:[\\/]", value)) or value.startswith(
            ("/", "\\\\", "file:")
        )
    if isinstance(value, list):
        return any(_contains_local_path(item) for item in value)
    if isinstance(value, dict):
        _reject_operational_source_metadata(value)
    return False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
