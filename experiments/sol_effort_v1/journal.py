"""Cross-process SQLite journal for experimental provider calls."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import uuid
from typing import Any, Mapping

from .canonical import canonical_text, sha256_bytes


class JournalError(RuntimeError):
    pass


class JournalBusy(JournalError):
    pass


class BudgetExceeded(JournalError):
    pass


TERMINAL_STATES = {"SUCCEEDED", "FAILED", "INDETERMINATE", "SKIPPED_DEPENDENCY"}
RECONCILIATION_SOURCES = {
    "OPENAI_BILLING_EXPORT",
    "OPENAI_INVOICE",
    "OPENAI_SUPPORT_CASE",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _decimal(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception as exc:  # pragma: no cover
        raise JournalError("invalid monetary value in journal") from exc


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True


class CallJournal:
    """Durable call claims, raw bytes, accounting and budget reservations.

    SQLite BEGIN IMMEDIATE serializes claims and budget reservations across
    processes. A raw response is stored as a BLOB in the same database before
    the client parses it. Unknown billing remains reserved and is never
    silently converted to zero.
    """

    def __init__(self, root: str | Path, *, max_budget_usd: Decimal | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "journal.sqlite3"
        self.owner_token = uuid.uuid4().hex
        self.owner_pid = os.getpid()
        self._initialize()
        if max_budget_usd is not None:
            self.configure_budget(max_budget_usd)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS journal_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS calls (
                    call_id TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    owner_pid INTEGER,
                    owner_token TEXT,
                    started_at TEXT NOT NULL,
                    response_received INTEGER NOT NULL DEFAULT 0,
                    raw BLOB,
                    raw_sha256 TEXT,
                    raw_byte_length INTEGER,
                    accounting_json TEXT NOT NULL DEFAULT '{}',
                    cost_usd TEXT,
                    cost_unknown INTEGER NOT NULL DEFAULT 0,
                    reserved_usd TEXT NOT NULL DEFAULT '0',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT,
                    error_type TEXT,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS cost_reconciliations (
                    reconciliation_id TEXT PRIMARY KEY,
                    call_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    previous_cost_usd TEXT,
                    reconciled_cost_usd TEXT NOT NULL,
                    released_reservation_usd TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    evidence_sha256 TEXT NOT NULL,
                    FOREIGN KEY(call_id) REFERENCES calls(call_id)
                );
                CREATE TRIGGER IF NOT EXISTS cost_reconciliations_no_update
                BEFORE UPDATE ON cost_reconciliations
                BEGIN
                    SELECT RAISE(ABORT, 'cost reconciliation history is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS cost_reconciliations_no_delete
                BEFORE DELETE ON cost_reconciliations
                BEGIN
                    SELECT RAISE(ABORT, 'cost reconciliation history is append-only');
                END;
                """
            )

    def configure_budget(self, max_budget_usd: Decimal) -> None:
        if max_budget_usd <= 0:
            raise BudgetExceeded("maximum budget must be positive")
        with self._connection() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT value FROM journal_meta WHERE key='max_budget_usd'").fetchone()
            if row is not None and _decimal(row[0]) != max_budget_usd:
                c.rollback()
                raise BudgetExceeded("journal budget conflicts with existing budget")
            c.execute(
                "INSERT OR REPLACE INTO journal_meta(key,value) VALUES('max_budget_usd',?)",
                (str(max_budget_usd),),
            )
            c.commit()

    def _budget(self, c: sqlite3.Connection) -> tuple[Decimal | None, Decimal, Decimal]:
        row = c.execute("SELECT value FROM journal_meta WHERE key='max_budget_usd'").fetchone()
        maximum = None if row is None else _decimal(row[0])
        reserved = sum((_decimal(x[0]) for x in c.execute("SELECT reserved_usd FROM calls")), Decimal("0"))
        committed = sum((_decimal(x[0]) for x in c.execute("SELECT cost_usd FROM calls WHERE cost_unknown=0")), Decimal("0"))
        return maximum, reserved, committed

    def start(self, *, call_id: str, request_hash: str, metadata: Mapping[str, Any],
              reservation_usd: Decimal = Decimal("0")) -> dict[str, Any]:
        with self._connection() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM calls WHERE call_id=?", (call_id,)).fetchone()
            if row is not None:
                if row["request_hash"] != request_hash:
                    c.rollback()
                    raise JournalError(f"request hash conflict for {call_id}")
                value = self._row(row)
                if value["status"] == "INDETERMINATE":
                    c.rollback()
                    raise JournalError(f"call {call_id} is INDETERMINATE; explicit resolution required")
                if value["status"] == "STARTED":
                    if value["owner_pid"] not in (None, self.owner_pid):
                        if _pid_alive(value["owner_pid"]):
                            c.rollback()
                            raise JournalBusy(f"call {call_id} is claimed by another process")
                        c.execute("UPDATE calls SET status='INDETERMINATE',error_type='RECOVERY',error=? WHERE call_id=?",
                                  ("owner process disappeared before response checkpoint", call_id))
                        c.commit()
                        raise JournalError(f"call {call_id} is INDETERMINATE; explicit resolution required")
                    if value["owner_token"] != self.owner_token:
                        c.execute("UPDATE calls SET status='INDETERMINATE',error_type='RECOVERY',error=? WHERE call_id=?",
                                  ("call was started by a previous owner", call_id))
                        c.commit()
                        raise JournalError(f"call {call_id} is INDETERMINATE; explicit resolution required")
                c.commit()
                return value
            maximum, reserved, committed = self._budget(c)
            if maximum is not None and committed + reserved + reservation_usd > maximum:
                c.rollback()
                raise BudgetExceeded(f"budget reservation rejected for {call_id}")
            c.execute(
                """INSERT INTO calls(call_id,request_hash,status,owner_pid,owner_token,started_at,
                   reserved_usd,metadata_json) VALUES(?,?,?,?,?,?,?,?)""",
                (call_id, request_hash, "STARTED", self.owner_pid, self.owner_token, _now(),
                 str(reservation_usd), json.dumps(dict(metadata), sort_keys=True)),
            )
            c.commit()
            return self.load(call_id) or {}

    def record_http_response(self, *, call_id: str, raw: bytes,
                             accounting: Mapping[str, Any]) -> dict[str, Any]:
        digest = sha256_bytes(raw)
        with self._connection() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM calls WHERE call_id=?", (call_id,)).fetchone()
            if row is None:
                c.rollback()
                raise JournalError(f"call was not started: {call_id}")
            if row["raw"] is not None and bytes(row["raw"]) != raw:
                c.rollback()
                raise JournalError(f"raw response conflict for {call_id}")
            previous_cost = _decimal(row["cost_usd"])
            cost_value = accounting.get("cost_usd")
            cost_unknown = cost_value is None or bool(accounting.get("cost_unknown"))
            new_cost = None if cost_unknown else str(cost_value)
            reservation = _decimal(row["reserved_usd"])
            committed_delta = Decimal("0")
            if not cost_unknown and row["cost_unknown"] == 0:
                committed_delta = _decimal(new_cost) - previous_cost
            if not cost_unknown:
                reservation = Decimal("0")
            c.execute(
                """UPDATE calls SET response_received=1,raw=?,raw_sha256=?,raw_byte_length=?,
                   accounting_json=?,cost_usd=?,cost_unknown=?,reserved_usd=? WHERE call_id=?""",
                (raw, digest, len(raw), json.dumps(dict(accounting), sort_keys=True), new_cost,
                 int(cost_unknown), str(reservation), call_id),
            )
            c.commit()
        return self.load(call_id) or {}

    def record_accounting(self, *, call_id: str,
                          accounting: Mapping[str, Any]) -> dict[str, Any]:
        """Persist billing metadata when no raw response is available.

        Unknown billing deliberately keeps the existing reservation. A later
        explicit reconciliation may replace it with a verified cost.
        """
        cost_value = accounting.get("cost_usd")
        if cost_value is not None or not bool(accounting.get("cost_unknown")):
            raise JournalError("record_accounting requires unknown billing")
        with self._connection() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM calls WHERE call_id=?", (call_id,)).fetchone()
            if row is None:
                c.rollback()
                raise JournalError(f"call was not started: {call_id}")
            if row["cost_unknown"] == 0 and row["cost_usd"] is not None:
                c.commit()
                return self.load(call_id) or {}
            c.execute(
                "UPDATE calls SET accounting_json=?,cost_usd=NULL,cost_unknown=1 WHERE call_id=?",
                (json.dumps(dict(accounting), sort_keys=True), call_id),
            )
            c.commit()
        return self.load(call_id) or {}

    @staticmethod
    def _reconciliation_evidence(evidence: Mapping[str, Any] | None) -> tuple[dict[str, str], str]:
        if not isinstance(evidence, Mapping):
            raise JournalError("structured reconciliation evidence is required")
        required = {
            "source_type", "external_reference", "document_sha256",
            "evidence_timestamp_utc", "reason", "verified_by",
        }
        if set(evidence) != required:
            raise JournalError("reconciliation evidence fields are missing or unexpected")
        normalized = {key: str(evidence[key]).strip() for key in sorted(required)}
        if normalized["source_type"] not in RECONCILIATION_SOURCES:
            raise JournalError("reconciliation source is not an accepted provider evidence type")
        if len(normalized["external_reference"]) < 6:
            raise JournalError("reconciliation external reference is not verifiable")
        checksum = normalized["document_sha256"].lower()
        if len(checksum) != 64 or any(character not in "0123456789abcdef" for character in checksum):
            raise JournalError("reconciliation document checksum must be SHA-256")
        normalized["document_sha256"] = checksum
        try:
            timestamp = datetime.fromisoformat(
                normalized["evidence_timestamp_utc"].replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise JournalError("reconciliation evidence timestamp is invalid") from exc
        if timestamp.tzinfo is None:
            raise JournalError("reconciliation evidence timestamp must include a timezone")
        if len(normalized["reason"]) < 10 or len(normalized["verified_by"]) < 3:
            raise JournalError("reconciliation reason or verifier is insufficient")
        evidence_json = canonical_text(normalized)
        return normalized, sha256_bytes(evidence_json.encode("utf-8"))

    def reconcile_cost(self, *, call_id: str, cost_usd: Decimal,
                       evidence: Mapping[str, Any] | None = None,
                       accounting: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Release an indeterminate reservation only with provider evidence."""
        if cost_usd < 0:
            raise JournalError("reconciled cost cannot be negative")
        normalized_evidence, evidence_sha256 = self._reconciliation_evidence(evidence)
        with self._connection() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM calls WHERE call_id=?", (call_id,)).fetchone()
            if row is None:
                c.rollback()
                raise JournalError(f"call was not started: {call_id}")
            previous = c.execute(
                "SELECT * FROM cost_reconciliations WHERE call_id=?", (call_id,)
            ).fetchone()
            if previous is not None:
                if (_decimal(previous["reconciled_cost_usd"]) == cost_usd
                        and previous["evidence_sha256"] == evidence_sha256):
                    c.commit()
                    return self.load(call_id) or {}
                c.rollback()
                raise JournalError(f"reconciliation already exists for {call_id}")
            if row["status"] != "INDETERMINATE":
                c.rollback()
                raise JournalError("only INDETERMINATE calls require explicit reconciliation")
            if row["cost_unknown"] == 0:
                c.rollback()
                raise JournalError("reconciliation requires an unknown provider cost")
            merged = dict(json.loads(row["accounting_json"] or "{}"))
            merged.update(dict(accounting or {}))
            merged.update({
                "cost_usd": str(cost_usd),
                "cost_unknown": False,
                "reconciliation_evidence_sha256": evidence_sha256,
            })
            released_reservation = str(row["reserved_usd"])
            c.execute(
                """INSERT INTO cost_reconciliations(
                   reconciliation_id,call_id,created_at,previous_cost_usd,
                   reconciled_cost_usd,released_reservation_usd,evidence_json,
                   evidence_sha256) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    uuid.uuid4().hex, call_id, _now(), row["cost_usd"], str(cost_usd),
                    released_reservation, canonical_text(normalized_evidence), evidence_sha256,
                ),
            )
            c.execute(
                """UPDATE calls SET accounting_json=?,cost_usd=?,cost_unknown=0,
                   reserved_usd='0' WHERE call_id=?""",
                (json.dumps(merged, sort_keys=True), str(cost_usd), call_id),
            )
            c.commit()
        return self.load(call_id) or {}

    def reconciliations(self, call_id: str | None = None) -> list[dict[str, Any]]:
        with self._connection() as c:
            if call_id is None:
                rows = list(c.execute(
                    "SELECT * FROM cost_reconciliations ORDER BY created_at,reconciliation_id"
                ))
            else:
                rows = list(c.execute(
                    "SELECT * FROM cost_reconciliations WHERE call_id=? ORDER BY created_at,reconciliation_id",
                    (call_id,),
                ))
        values = []
        for row in rows:
            value = dict(row)
            value["evidence"] = json.loads(value.pop("evidence_json"))
            values.append(value)
        return values

    def finalize(self, *, call_id: str, status: str, result: Mapping[str, Any] | None = None,
                 error_type: str | None = None, error: str | None = None) -> dict[str, Any]:
        if status not in TERMINAL_STATES:
            raise JournalError(f"invalid terminal status: {status}")
        with self._connection() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM calls WHERE call_id=?", (call_id,)).fetchone()
            if row is None:
                c.rollback()
                raise JournalError(f"call was not started: {call_id}")
            if row["status"] in TERMINAL_STATES and row["status"] != status:
                c.rollback()
                raise JournalError(f"terminal status conflict for {call_id}")
            reservation = row["reserved_usd"]
            if status != "INDETERMINATE":
                reservation = "0"
            c.execute(
                """UPDATE calls SET status=?,reserved_usd=?,result_json=?,error_type=?,error=?
                   WHERE call_id=?""",
                (status, reservation, None if result is None else json.dumps(dict(result), sort_keys=True),
                 error_type, error, call_id),
            )
            c.commit()
        return self.load(call_id) or {}

    def load(self, call_id: str) -> dict[str, Any] | None:
        with self._connection() as c:
            row = c.execute("SELECT * FROM calls WHERE call_id=?", (call_id,)).fetchone()
        if row is None:
            return None
        value = self._row(row)
        self.verify(call_id, value)
        return value

    def verify(self, call_id: str, record: Mapping[str, Any] | None = None) -> None:
        value = dict(record or self.load(call_id) or {})
        raw = value.get("raw")
        if value.get("response_received"):
            if raw is None or sha256_bytes(bytes(raw)) != value.get("raw_sha256") or len(bytes(raw)) != value.get("raw_byte_length"):
                raise JournalError(f"raw response checksum/length mismatch for {call_id}")

    def recover_interrupted(self, call_id: str) -> dict[str, Any]:
        """Explicitly convert a stale STARTED call to INDETERMINATE."""
        with self._connection() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM calls WHERE call_id=?", (call_id,)).fetchone()
            if row is None:
                c.rollback()
                raise JournalError(f"call was not started: {call_id}")
            c.execute("UPDATE calls SET status='INDETERMINATE',error_type='RECOVERY',error=? WHERE call_id=?",
                       ("explicit interruption recovery", call_id))
            c.commit()
        return self.load(call_id) or {}

    def consumed_cost(self) -> Decimal:
        with self._connection() as c:
            return self._budget(c)[2]

    def reserved_cost(self) -> Decimal:
        with self._connection() as c:
            return self._budget(c)[1]

    def records(self) -> list[dict[str, Any]]:
        with self._connection() as c:
            rows = list(c.execute("SELECT * FROM calls ORDER BY call_id"))
        return [self._row(row) for row in rows]

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        for field in ("metadata_json", "accounting_json", "result_json"):
            raw = value.pop(field, None)
            key = field.removesuffix("_json")
            value[key] = None if raw is None else json.loads(raw)
        value["response_received"] = bool(value.get("response_received"))
        value["cost_unknown"] = bool(value.get("cost_unknown"))
        return value

