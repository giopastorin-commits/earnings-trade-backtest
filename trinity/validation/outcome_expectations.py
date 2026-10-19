"""Immutable, append-only outcome obligations derived from collected runs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Mapping

from .contracts import (
    CollectedRun,
    OFFICIAL_ELIGIBLE,
    SMOKE_ONLY,
    SelectionObservation,
    ValidationContractError,
)
from .market_calendar import (
    CALENDAR_NAME,
    CALENDAR_VERSION,
    outcome_schedule_evidence,
    parse_utc,
    schedule_evidence_sha256,
)
from .protocol_v1 import (
    HORIZONS,
    PROTOCOL_EPOCH,
    PROTOCOL_HASH,
    PROTOCOL_ID,
    canonical_json,
)
from .storage import ValidationStore


OUTCOME_EXPECTATION = "OUTCOME_EXPECTATION"
_COHORTS = frozenset({"SCANNER", "LUNA", "SOL"})


class OutcomeExpectationError(ValidationContractError):
    """An immutable outcome expectation violates the frozen V1 contract."""


def _sha(value: object, field: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise OutcomeExpectationError(f"{field} must be a lowercase SHA-256")


def _git_oid(value: object, field: str) -> None:
    if not isinstance(value, str) or len(value) not in {40, 64} or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise OutcomeExpectationError(f"{field} must be a full lowercase Git object ID")


def outcome_identity(run_id: str, ticker: str, cohort: str, horizon_sessions: int) -> str:
    if not isinstance(run_id, str) or not run_id \
            or not isinstance(ticker, str) or not ticker \
            or not isinstance(cohort, str) or cohort not in _COHORTS \
            or not isinstance(horizon_sessions, int) or isinstance(horizon_sessions, bool) \
            or horizon_sessions not in HORIZONS:
        raise OutcomeExpectationError("outcome expectation identity is invalid")
    return f"{PROTOCOL_HASH}:{run_id}:{ticker}:{cohort}:{horizon_sessions}"


def _validate_stage_lineage(
    *, luna_evaluated: bool, luna_status: str, luna_decision: str,
    sol_evaluated: bool, sol_status: str, sol_decision: str,
) -> None:
    if type(luna_evaluated) is not bool or type(sol_evaluated) is not bool \
            or not all(isinstance(value, str) for value in (
                luna_status, luna_decision, sol_status, sol_decision,
            )):
        raise OutcomeExpectationError("outcome expectation stage lineage types are invalid")
    if luna_evaluated:
        luna_valid = (
            luna_status == "COMPLETED" and luna_decision in {"DROP", "WATCH", "ESCALATE"}
        ) or (
            luna_status == "EVALUATED_FAILED" and luna_decision == "EVALUATED_FAILED"
        )
    else:
        luna_valid = luna_status == "NOT_EVALUATED" and luna_decision == "NOT_EVALUATED"
    if not luna_valid:
        raise OutcomeExpectationError("outcome expectation Luna lineage is invalid")
    luna_routed = luna_evaluated and luna_status == "COMPLETED" \
        and luna_decision == "ESCALATE"
    if sol_evaluated:
        sol_valid = (
            sol_status == "PROMOTED" and sol_decision == "INVESTIGATE"
        ) or (
            sol_status == "REJECTED" and sol_decision in {"PASS", "WATCH"}
        ) or (
            sol_status == "EVALUATED_FAILED" and sol_decision == "EVALUATED_FAILED"
        )
    else:
        sol_valid = sol_status == "NOT_EVALUATED" and sol_decision == "NOT_EVALUATED"
    if not sol_valid or sol_evaluated != luna_routed:
        raise OutcomeExpectationError("outcome expectation Sol routing lineage is invalid")


def _validate_schedule(
    evidence: object, *, decision_timestamp_utc: str, horizon_sessions: int,
    first_session: str, maturity_session: str, calendar: str,
    calendar_version: str, expected_sha: str,
) -> dict[str, Any]:
    if not isinstance(evidence, Mapping):
        raise OutcomeExpectationError("schedule evidence must be an object")
    detached = dict(evidence)
    if set(detached) != {
        "calendar", "calendar_version", "decision_timestamp_utc", "horizon_sessions",
        "first_tradable_session", "maturity_session", "sessions", "schedule_sha256",
    }:
        raise OutcomeExpectationError("schedule evidence shape is invalid")
    _sha(expected_sha, "schedule_sha256")
    if detached.get("schedule_sha256") != expected_sha \
            or schedule_evidence_sha256(detached) != expected_sha:
        raise OutcomeExpectationError("schedule evidence SHA-256 mismatch")
    if detached.get("calendar") != calendar or calendar != CALENDAR_NAME \
            or detached.get("calendar_version") != calendar_version \
            or not isinstance(calendar_version, str) or not calendar_version:
        raise OutcomeExpectationError("schedule evidence calendar provenance mismatch")
    if detached.get("horizon_sessions") != horizon_sessions:
        raise OutcomeExpectationError("schedule evidence horizon mismatch")
    try:
        if parse_utc(str(detached.get("decision_timestamp_utc") or ""), "schedule decision") \
                != parse_utc(decision_timestamp_utc, "decision_timestamp_utc"):
            raise OutcomeExpectationError("schedule evidence decision timestamp mismatch")
    except ValidationContractError as exc:
        raise OutcomeExpectationError(str(exc)) from exc
    sessions = detached.get("sessions")
    if not isinstance(sessions, list) or len(sessions) != horizon_sessions:
        raise OutcomeExpectationError("schedule evidence session count mismatch")
    labels: list[str] = []
    previous_open = None
    for row in sessions:
        if not isinstance(row, Mapping) or set(row) != {"session", "open_utc", "close_utc"}:
            raise OutcomeExpectationError("schedule evidence session is malformed")
        label = str(row.get("session") or "")
        try:
            date.fromisoformat(label)
            opened = parse_utc(str(row.get("open_utc") or ""), "schedule open")
            closed = parse_utc(str(row.get("close_utc") or ""), "schedule close")
        except (ValueError, ValidationContractError) as exc:
            raise OutcomeExpectationError("schedule evidence session timestamp is invalid") from exc
        if opened >= closed or (previous_open is not None and opened <= previous_open):
            raise OutcomeExpectationError("schedule evidence sessions are not ordered")
        labels.append(label)
        previous_open = opened
    if labels != sorted(labels) or len(labels) != len(set(labels)):
        raise OutcomeExpectationError("schedule evidence session labels are not unique and ordered")
    if labels[0] != first_session or labels[-1] != maturity_session \
            or detached.get("first_tradable_session") != first_session \
            or detached.get("maturity_session") != maturity_session:
        raise OutcomeExpectationError("schedule evidence D1 or DN mismatch")
    if calendar_version == CALENDAR_VERSION:
        runtime = outcome_schedule_evidence(decision_timestamp_utc, horizon_sessions)
        if canonical_json(detached) != canonical_json(runtime):
            raise OutcomeExpectationError("schedule evidence differs from the recorded runtime calendar")
    return detached


@dataclass(frozen=True)
class OutcomeExpectation:
    protocol_id: str
    protocol_hash: str
    protocol_epoch: int
    run_id: str
    eligibility: str
    ticker: str
    cohort: str
    horizon_sessions: int
    decision_timestamp_utc: str
    decision_target_session: str
    expected_first_tradable_session: str
    maturity_session: str
    analytical_commit_sha: str
    decision_snapshot_id: str
    decision_snapshot_sha256: str
    source_run_manifest_sha256: str
    universe_version: str
    universe_sha256: str
    calendar: str
    calendar_version: str
    schedule_evidence: Mapping[str, Any]
    schedule_sha256: str
    scanner_selected: bool
    scanner_state: str
    luna_evaluated: bool
    luna_status: str
    luna_decision: str
    sol_evaluated: bool
    sol_status: str
    sol_decision: str
    selection_data_quality_flags: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.protocol_id, str) or not isinstance(self.protocol_hash, str) \
                or type(self.protocol_epoch) is not int:
            raise OutcomeExpectationError("outcome expectation protocol identity types are invalid")
        if (self.protocol_id, self.protocol_hash, self.protocol_epoch) != (
            PROTOCOL_ID, PROTOCOL_HASH, PROTOCOL_EPOCH,
        ):
            raise OutcomeExpectationError("outcome expectation protocol identity mismatch")
        if not isinstance(self.eligibility, str) \
                or self.eligibility not in {SMOKE_ONLY, OFFICIAL_ELIGIBLE}:
            raise OutcomeExpectationError("outcome expectation eligibility is invalid")
        outcome_identity(self.run_id, self.ticker, self.cohort, self.horizon_sessions)
        if self.scanner_selected is not True or not isinstance(self.scanner_state, str) \
                or self.scanner_state != "READY_TECHNICALLY":
            raise OutcomeExpectationError("outcome expectation Scanner lineage is invalid")
        _validate_stage_lineage(
            luna_evaluated=self.luna_evaluated, luna_status=self.luna_status,
            luna_decision=self.luna_decision, sol_evaluated=self.sol_evaluated,
            sol_status=self.sol_status, sol_decision=self.sol_decision,
        )
        if self.cohort in {"LUNA", "SOL"} and not (
            self.luna_evaluated and self.luna_status == "COMPLETED"
            and self.luna_decision == "ESCALATE"
        ):
            raise OutcomeExpectationError("Luna cohort lineage is invalid")
        if self.cohort == "SOL" and not (
            self.sol_evaluated and self.sol_status == "PROMOTED"
            and self.sol_decision == "INVESTIGATE"
        ):
            raise OutcomeExpectationError("Sol cohort lineage is invalid")
        parse_utc(self.decision_timestamp_utc, "decision_timestamp_utc")
        try:
            date.fromisoformat(self.decision_target_session)
        except (TypeError, ValueError) as exc:
            raise OutcomeExpectationError("invalid decision_target_session") from exc
        _git_oid(self.analytical_commit_sha, "analytical_commit_sha")
        if not isinstance(self.decision_snapshot_id, str) or not self.decision_snapshot_id \
                or not isinstance(self.universe_version, str) or not self.universe_version:
            raise OutcomeExpectationError("decision snapshot or universe identity is missing")
        _sha(self.decision_snapshot_sha256, "decision_snapshot_sha256")
        _sha(self.source_run_manifest_sha256, "source_run_manifest_sha256")
        _sha(self.universe_sha256, "universe_sha256")
        evidence = _validate_schedule(
            self.schedule_evidence, decision_timestamp_utc=self.decision_timestamp_utc,
            horizon_sessions=self.horizon_sessions,
            first_session=self.expected_first_tradable_session,
            maturity_session=self.maturity_session, calendar=self.calendar,
            calendar_version=self.calendar_version, expected_sha=self.schedule_sha256,
        )
        object.__setattr__(self, "schedule_evidence", evidence)
        if not isinstance(self.selection_data_quality_flags, (list, tuple)) \
                or not all(isinstance(flag, str) for flag in self.selection_data_quality_flags):
            raise OutcomeExpectationError("selection_data_quality_flags must be a list of strings")
        object.__setattr__(self, "selection_data_quality_flags", tuple(
            self.selection_data_quality_flags,
        ))

    @property
    def key(self) -> str:
        return outcome_identity(self.run_id, self.ticker, self.cohort, self.horizon_sessions)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OutcomeExpectation":
        if not isinstance(value, Mapping):
            raise OutcomeExpectationError("outcome expectation payload must be an object")
        try:
            return cls(**dict(value))
        except TypeError as exc:
            raise OutcomeExpectationError("outcome expectation payload shape is invalid") from exc


def _validate_selection(selection: SelectionObservation, collected: CollectedRun) -> None:
    if selection.run_id != collected.run_id or selection.eligibility != collected.eligibility:
        raise OutcomeExpectationError("selection and collected run identity mismatch")
    if (selection.protocol_version, selection.protocol_epoch) != (PROTOCOL_ID, PROTOCOL_EPOCH):
        raise OutcomeExpectationError("selection protocol identity mismatch")
    if not selection.snapshot_id:
        raise OutcomeExpectationError("decision snapshot identity is missing")
    _sha(selection.snapshot_sha256, "decision_snapshot_sha256")
    _sha(selection.universe_sha256, "universe_sha256")
    _git_oid(selection.analytical_commit_sha, "analytical_commit_sha")
    _validate_stage_lineage(
        luna_evaluated=selection.luna_evaluated, luna_status=selection.luna_status,
        luna_decision=selection.luna_decision, sol_evaluated=selection.sol_evaluated,
        sol_status=selection.sol_status, sol_decision=selection.sol_decision,
    )


def build_outcome_expectations(collected: CollectedRun) -> tuple[OutcomeExpectation, ...]:
    """Freeze deterministic D5/D20/D60 obligations from one collected run."""

    if not collected.run_id or collected.eligibility not in {SMOKE_ONLY, OFFICIAL_ELIGIBLE}:
        raise OutcomeExpectationError("collected run identity or eligibility is invalid")
    _sha(collected.source_manifest_sha256, "source_run_manifest_sha256")
    tickers = [selection.ticker for selection in collected.selections]
    if len(tickers) != len(set(tickers)):
        raise OutcomeExpectationError("collected run contains duplicate selection tickers")
    result: list[OutcomeExpectation] = []
    for selection in collected.selections:
        _validate_selection(selection, collected)
        cohorts = ["SCANNER"]
        if selection.luna_evaluated and selection.luna_status == "COMPLETED" \
                and selection.luna_decision == "ESCALATE":
            cohorts.append("LUNA")
        if selection.sol_evaluated and selection.sol_status == "PROMOTED" \
                and selection.sol_decision == "INVESTIGATE":
            cohorts.append("SOL")
        for horizon in HORIZONS:
            schedule = outcome_schedule_evidence(selection.run_timestamp_utc, horizon)
            for cohort in cohorts:
                result.append(OutcomeExpectation(
                    protocol_id=PROTOCOL_ID, protocol_hash=PROTOCOL_HASH,
                    protocol_epoch=PROTOCOL_EPOCH, run_id=selection.run_id,
                    eligibility=selection.eligibility, ticker=selection.ticker,
                    cohort=cohort, horizon_sessions=horizon,
                    decision_timestamp_utc=selection.run_timestamp_utc,
                    decision_target_session=selection.intended_session,
                    expected_first_tradable_session=str(schedule["first_tradable_session"]),
                    maturity_session=str(schedule["maturity_session"]),
                    analytical_commit_sha=selection.analytical_commit_sha,
                    decision_snapshot_id=selection.snapshot_id,
                    decision_snapshot_sha256=selection.snapshot_sha256,
                    source_run_manifest_sha256=collected.source_manifest_sha256,
                    universe_version=selection.universe_version,
                    universe_sha256=selection.universe_sha256,
                    calendar=str(schedule["calendar"]),
                    calendar_version=str(schedule["calendar_version"]),
                    schedule_evidence=dict(schedule),
                    schedule_sha256=str(schedule["schedule_sha256"]),
                    scanner_selected=selection.scanner_selected,
                    scanner_state=selection.scanner_state,
                    luna_evaluated=selection.luna_evaluated,
                    luna_status=selection.luna_status,
                    luna_decision=selection.luna_decision,
                    sol_evaluated=selection.sol_evaluated,
                    sol_status=selection.sol_status,
                    sol_decision=selection.sol_decision,
                    selection_data_quality_flags=tuple(selection.data_quality_flags),
                ))
    keys = [item.key for item in result]
    if len(keys) != len(set(keys)):
        raise OutcomeExpectationError("duplicate OUTCOME_EXPECTATION identity")
    return tuple(result)


def append_outcome_expectations(store: ValidationStore, collected: CollectedRun) -> int:
    expectations = build_outcome_expectations(collected)
    if len({item.key for item in expectations}) != len(expectations):
        raise OutcomeExpectationError("duplicate OUTCOME_EXPECTATION identity")
    return sum(
        store.append(OUTCOME_EXPECTATION, item.key, item.to_dict())
        for item in expectations
    )
