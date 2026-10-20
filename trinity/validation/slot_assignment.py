"""Authoritative weekly slot closure and exact-set cycle assignment.

This layer closes one frozen observation slot against the append-only cycle
inventory.  A closure is terminal: cycles registered after closure cannot
retroactively alter it.  Only a cycle matching the complete mature,
unassigned expectation set can produce assignments.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
from typing import Any, Iterable, Mapping

from .contracts import ValidationContractError
from .cycle_registry import ObservationCycleRegistration, load_registered_cycles
from .market_calendar import parse_utc
from .observation_cycle import expectation_references, expectation_set_sha256
from .observation_slots import ObservationSlot, weekly_observation_slot
from .outcome_expectations import OUTCOME_EXPECTATION, OutcomeExpectation
from .protocol_v1 import PROTOCOL_EPOCH, PROTOCOL_HASH, PROTOCOL_ID, canonical_json
from .stock_observation import expected_ticker_set_sha256
from .storage import ValidationStore


OBSERVATION_SLOT_CLOSURE = "OBSERVATION_SLOT_CLOSURE"
EXPECTATION_CYCLE_ASSIGNMENT = "EXPECTATION_CYCLE_ASSIGNMENT"

EMPTY_NO_EXPECTATIONS = "EMPTY_NO_EXPECTATIONS"
ASSIGNED_EXACT_CYCLE = "ASSIGNED_EXACT_CYCLE"
OPERATIONAL_OBSERVATION_MISSING = "OPERATIONAL_OBSERVATION_MISSING"
AMBIGUOUS_OPERATIONAL_ERROR = "AMBIGUOUS_OPERATIONAL_ERROR"
ASSIGNMENT_POLICY = "EXACT_SLOT_SET_V1"

_CLOSURE_STATES = frozenset({
    EMPTY_NO_EXPECTATIONS,
    ASSIGNED_EXACT_CYCLE,
    OPERATIONAL_OBSERVATION_MISSING,
    AMBIGUOUS_OPERATIONAL_ERROR,
})


class SlotAssignmentError(ValidationContractError):
    """A slot closure or assignment violates frozen V1 governance."""


def _sha(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise SlotAssignmentError(f"{field} must be a lowercase SHA-256")
    return value


def _payload_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(value))).hexdigest()


def _empty_set_sha256() -> str:
    return hashlib.sha256(canonical_json([])).hexdigest()


def _utc_string(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _protocol_identity(payload: Mapping[str, Any], label: str) -> bool:
    """Return whether payload is current, rejecting malformed foreign identity."""

    protocol_id = payload.get("protocol_id")
    protocol_hash = payload.get("protocol_hash")
    protocol_epoch = payload.get("protocol_epoch")
    if not isinstance(protocol_id, str) or type(protocol_epoch) is not int:
        raise SlotAssignmentError(f"stored {label} protocol identity is malformed")
    _sha(protocol_hash, f"{label} protocol_hash")
    if protocol_hash == PROTOCOL_HASH and (protocol_id, protocol_epoch) != (
        PROTOCOL_ID, PROTOCOL_EPOCH,
    ):
        raise SlotAssignmentError(f"stored {label} current protocol identity is inconsistent")
    return (protocol_id, protocol_hash, protocol_epoch) == (
        PROTOCOL_ID, PROTOCOL_HASH, PROTOCOL_EPOCH,
    )


def _canonical_strings(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise SlotAssignmentError(f"{field} must be a list")
    result = tuple(value)
    if any(not isinstance(item, str) or not item for item in result):
        raise SlotAssignmentError(f"{field} contains an invalid value")
    if result != tuple(sorted(result)) or len(result) != len(set(result)):
        raise SlotAssignmentError(f"{field} must be unique and ordered")
    return result


@dataclass(frozen=True)
class ExpectationCycleAssignment:
    protocol_id: str
    protocol_hash: str
    protocol_epoch: int
    expectation_key: str
    expectation_payload_sha256: str
    observation_slot_id: str
    iso_year: int
    iso_week: int
    slot_target_session: str
    observation_cycle_id: str
    observation_cycle_sha256: str
    cycle_manifest_sha256: str
    expected_expectation_set_sha256: str
    expected_ticker_set_sha256: str
    assignment_policy: str

    def __post_init__(self) -> None:
        if not isinstance(self.protocol_id, str) or not isinstance(self.protocol_hash, str) \
                or type(self.protocol_epoch) is not int:
            raise SlotAssignmentError("assignment protocol identity types are invalid")
        if (self.protocol_id, self.protocol_hash, self.protocol_epoch) != (
            PROTOCOL_ID, PROTOCOL_HASH, PROTOCOL_EPOCH,
        ):
            raise SlotAssignmentError("assignment protocol identity mismatch")
        if not isinstance(self.expectation_key, str) \
                or not self.expectation_key.startswith(f"{PROTOCOL_HASH}:"):
            raise SlotAssignmentError("assignment expectation key is invalid")
        for field in (
            "expectation_payload_sha256", "observation_slot_id",
            "observation_cycle_id", "observation_cycle_sha256",
            "cycle_manifest_sha256", "expected_expectation_set_sha256",
            "expected_ticker_set_sha256",
        ):
            _sha(getattr(self, field), field)
        if self.assignment_policy != ASSIGNMENT_POLICY:
            raise SlotAssignmentError("assignment policy mismatch")
        try:
            slot = weekly_observation_slot(self.iso_year, self.iso_week)
        except ValidationContractError as exc:
            raise SlotAssignmentError("assignment slot is invalid") from exc
        if self.observation_slot_id != slot.observation_slot_id \
                or self.slot_target_session != slot.slot_target_session:
            raise SlotAssignmentError("assignment slot identity mismatch")

    @property
    def key(self) -> str:
        return self.expectation_key

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExpectationCycleAssignment":
        if not isinstance(value, Mapping):
            raise SlotAssignmentError("assignment payload must be an object")
        try:
            return cls(**dict(value))
        except TypeError as exc:
            raise SlotAssignmentError("assignment payload shape is invalid") from exc


@dataclass(frozen=True)
class ObservationSlotClosure:
    protocol_id: str
    protocol_hash: str
    protocol_epoch: int
    observation_slot_id: str
    iso_year: int
    iso_week: int
    slot_target_session: str
    finality_eligible_at_utc: str
    closure_state: str
    closed_at_utc: str
    expected_expectation_keys: tuple[str, ...]
    expected_expectation_set_sha256: str
    expected_tickers: tuple[str, ...]
    expected_ticker_set_sha256: str
    same_target_cycle_ids: tuple[str, ...]
    exact_candidate_cycle_ids: tuple[str, ...]
    nonexact_candidate_cycle_ids: tuple[str, ...]
    authoritative_cycle_id: str | None
    assignment_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.protocol_id, str) or not isinstance(self.protocol_hash, str) \
                or type(self.protocol_epoch) is not int:
            raise SlotAssignmentError("slot closure protocol identity types are invalid")
        if (self.protocol_id, self.protocol_hash, self.protocol_epoch) != (
            PROTOCOL_ID, PROTOCOL_HASH, PROTOCOL_EPOCH,
        ):
            raise SlotAssignmentError("slot closure protocol identity mismatch")
        try:
            slot = weekly_observation_slot(self.iso_year, self.iso_week)
        except ValidationContractError as exc:
            raise SlotAssignmentError("slot closure ISO week is invalid") from exc
        if self.observation_slot_id != slot.observation_slot_id \
                or self.slot_target_session != slot.slot_target_session \
                or self.finality_eligible_at_utc != slot.finality_eligible_at_utc:
            raise SlotAssignmentError("slot closure frozen slot identity mismatch")
        try:
            closed = parse_utc(self.closed_at_utc, "closed_at_utc")
            finality = parse_utc(self.finality_eligible_at_utc, "finality_eligible_at_utc")
        except ValidationContractError as exc:
            raise SlotAssignmentError("slot closure timestamp is invalid") from exc
        if closed < finality:
            raise SlotAssignmentError("slot cannot close before finality")
        if self.closure_state not in _CLOSURE_STATES:
            raise SlotAssignmentError("slot closure state is invalid")
        expected_keys = _canonical_strings(
            self.expected_expectation_keys, "expected expectation keys",
        )
        if any(not key.startswith(f"{PROTOCOL_HASH}:") for key in expected_keys):
            raise SlotAssignmentError("expected expectation key protocol mismatch")
        expected_tickers = _canonical_strings(self.expected_tickers, "expected tickers")
        same = _canonical_strings(self.same_target_cycle_ids, "same-target cycle IDs")
        exact = _canonical_strings(self.exact_candidate_cycle_ids, "exact cycle IDs")
        nonexact = _canonical_strings(
            self.nonexact_candidate_cycle_ids, "nonexact cycle IDs",
        )
        for field in (
            "observation_slot_id", "expected_expectation_set_sha256",
            "expected_ticker_set_sha256",
        ):
            _sha(getattr(self, field), field)
        for cycle_id in same + exact + nonexact:
            _sha(cycle_id, "candidate cycle ID")
        if set(exact) & set(nonexact) or tuple(sorted(exact + nonexact)) != same:
            raise SlotAssignmentError("slot candidate diagnostics are inconsistent")
        if self.authoritative_cycle_id is not None:
            _sha(self.authoritative_cycle_id, "authoritative_cycle_id")
        if type(self.assignment_count) is not int or self.assignment_count < 0:
            raise SlotAssignmentError("assignment_count is invalid")
        if expected_tickers:
            if expected_ticker_set_sha256(expected_tickers) \
                    != self.expected_ticker_set_sha256:
                raise SlotAssignmentError("expected ticker-set SHA-256 mismatch")
        elif self.expected_ticker_set_sha256 != _empty_set_sha256():
            raise SlotAssignmentError("empty expected ticker-set SHA-256 mismatch")
        if self.closure_state == EMPTY_NO_EXPECTATIONS:
            valid = not expected_keys and not exact and self.authoritative_cycle_id is None \
                and self.assignment_count == 0
            if self.expected_expectation_set_sha256 != _empty_set_sha256():
                raise SlotAssignmentError("empty expectation-set SHA-256 mismatch")
        elif self.closure_state == ASSIGNED_EXACT_CYCLE:
            valid = bool(expected_keys) and len(exact) == 1 \
                and self.authoritative_cycle_id == exact[0] \
                and self.assignment_count == len(expected_keys)
        elif self.closure_state == OPERATIONAL_OBSERVATION_MISSING:
            valid = bool(expected_keys) and not exact \
                and self.authoritative_cycle_id is None and self.assignment_count == 0
        else:
            valid = bool(expected_keys) and len(exact) > 1 \
                and self.authoritative_cycle_id is None and self.assignment_count == 0
        if not valid:
            raise SlotAssignmentError("slot closure state invariants are inconsistent")
        object.__setattr__(self, "expected_expectation_keys", expected_keys)
        object.__setattr__(self, "expected_tickers", expected_tickers)
        object.__setattr__(self, "same_target_cycle_ids", same)
        object.__setattr__(self, "exact_candidate_cycle_ids", exact)
        object.__setattr__(self, "nonexact_candidate_cycle_ids", nonexact)

    @property
    def key(self) -> str:
        return self.observation_slot_id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ObservationSlotClosure":
        if not isinstance(value, Mapping):
            raise SlotAssignmentError("slot closure payload must be an object")
        try:
            return cls(**dict(value))
        except TypeError as exc:
            raise SlotAssignmentError("slot closure payload shape is invalid") from exc


def load_current_expectations(store: ValidationStore) -> tuple[OutcomeExpectation, ...]:
    """Load current-protocol expectations while preserving retired records."""

    result: list[OutcomeExpectation] = []
    for record_key, payload in store.record_items(OUTCOME_EXPECTATION):
        if not isinstance(payload, Mapping):
            raise SlotAssignmentError("stored outcome expectation is not an object")
        if not _protocol_identity(payload, "outcome expectation"):
            continue
        try:
            expectation = OutcomeExpectation.from_dict(payload)
        except (TypeError, ValueError, ValidationContractError) as exc:
            raise SlotAssignmentError("stored current-protocol expectation is invalid") from exc
        if record_key != expectation.key:
            raise SlotAssignmentError("expectation record key and identity differ")
        result.append(expectation)
    keys = [item.key for item in result]
    if len(keys) != len(set(keys)):
        raise SlotAssignmentError("duplicate current-protocol expectation identity")
    return tuple(sorted(result, key=lambda item: item.key))


def _expectation_map(
    expectations: Iterable[OutcomeExpectation],
) -> dict[str, OutcomeExpectation]:
    result: dict[str, OutcomeExpectation] = {}
    for supplied in expectations:
        try:
            item = OutcomeExpectation.from_dict(supplied.to_dict())
        except (AttributeError, TypeError, ValueError, ValidationContractError) as exc:
            raise SlotAssignmentError("assignment expectation source is invalid") from exc
        if item.key in result:
            raise SlotAssignmentError("duplicate assignment expectation identity")
        result[item.key] = item
    return result


def load_expectation_assignments(
    store: ValidationStore, expectations: Iterable[OutcomeExpectation],
) -> tuple[ExpectationCycleAssignment, ...]:
    expectation_by_key = _expectation_map(expectations)
    result: list[ExpectationCycleAssignment] = []
    for record_key, payload in store.record_items(EXPECTATION_CYCLE_ASSIGNMENT):
        if not isinstance(payload, Mapping):
            raise SlotAssignmentError("stored expectation assignment is not an object")
        if not _protocol_identity(payload, "expectation assignment"):
            continue
        try:
            assignment = ExpectationCycleAssignment.from_dict(payload)
        except (TypeError, ValueError, ValidationContractError) as exc:
            raise SlotAssignmentError("stored expectation assignment is invalid") from exc
        if record_key != assignment.expectation_key:
            raise SlotAssignmentError("assignment record key and expectation differ")
        expectation = expectation_by_key.get(assignment.expectation_key)
        if expectation is None:
            raise SlotAssignmentError("assignment references no current expectation")
        if _payload_sha256(expectation.to_dict()) != assignment.expectation_payload_sha256:
            raise SlotAssignmentError("assignment expectation payload binding mismatch")
        result.append(assignment)
    keys = [item.expectation_key for item in result]
    if len(keys) != len(set(keys)):
        raise SlotAssignmentError("duplicate current-protocol expectation assignment")
    return tuple(sorted(result, key=lambda item: item.expectation_key))


def load_slot_closures(store: ValidationStore) -> tuple[ObservationSlotClosure, ...]:
    result: list[ObservationSlotClosure] = []
    for record_key, payload in store.record_items(OBSERVATION_SLOT_CLOSURE):
        if not isinstance(payload, Mapping):
            raise SlotAssignmentError("stored slot closure is not an object")
        if not _protocol_identity(payload, "slot closure"):
            continue
        try:
            closure = ObservationSlotClosure.from_dict(payload)
        except (TypeError, ValueError, ValidationContractError) as exc:
            raise SlotAssignmentError("stored slot closure is invalid") from exc
        if record_key != closure.observation_slot_id:
            raise SlotAssignmentError("closure record key and slot identity differ")
        result.append(closure)
    identities = [item.observation_slot_id for item in result]
    if len(identities) != len(set(identities)):
        raise SlotAssignmentError("duplicate current-protocol slot closure")
    return tuple(sorted(result, key=lambda item: (item.iso_year, item.iso_week)))


def _expected_sets(
    expectations: Iterable[OutcomeExpectation], slot: ObservationSlot,
    assignments: Iterable[ExpectationCycleAssignment],
) -> tuple[
    tuple[OutcomeExpectation, ...], tuple[str, ...], str, tuple[str, ...], str,
]:
    assigned = {item.expectation_key for item in assignments}
    expected = tuple(sorted((
        item for item in expectations
        if item.maturity_session <= slot.slot_target_session and item.key not in assigned
    ), key=lambda item: item.key))
    if expected:
        refs = expectation_references(expected)
        expectation_sha = expectation_set_sha256(refs)
        tickers = tuple(sorted({item.ticker for item in expected}))
        ticker_sha = expected_ticker_set_sha256(tickers)
    else:
        expectation_sha = _empty_set_sha256()
        tickers = ()
        ticker_sha = _empty_set_sha256()
    return expected, tuple(item.key for item in expected), expectation_sha, tickers, ticker_sha


def _is_exact_candidate(
    registration: ObservationCycleRegistration, *, expectation_keys: tuple[str, ...],
    expectation_sha: str, tickers: tuple[str, ...], ticker_sha: str,
) -> bool:
    return (
        (registration.protocol_id, registration.protocol_hash, registration.protocol_epoch)
        == (PROTOCOL_ID, PROTOCOL_HASH, PROTOCOL_EPOCH)
        and registration.expectation_keys == expectation_keys
        and registration.expectation_set_sha256 == expectation_sha
        and registration.expected_tickers == tickers
        and registration.expected_ticker_set_sha256 == ticker_sha
    )


def _assignment(
    expectation: OutcomeExpectation, slot: ObservationSlot,
    cycle: ObservationCycleRegistration, expectation_sha: str, ticker_sha: str,
) -> ExpectationCycleAssignment:
    return ExpectationCycleAssignment(
        protocol_id=PROTOCOL_ID, protocol_hash=PROTOCOL_HASH,
        protocol_epoch=PROTOCOL_EPOCH, expectation_key=expectation.key,
        expectation_payload_sha256=_payload_sha256(expectation.to_dict()),
        observation_slot_id=slot.observation_slot_id,
        iso_year=slot.iso_year, iso_week=slot.iso_week,
        slot_target_session=slot.slot_target_session,
        observation_cycle_id=cycle.observation_cycle_id,
        observation_cycle_sha256=cycle.observation_cycle_sha256,
        cycle_manifest_sha256=cycle.cycle_manifest_sha256,
        expected_expectation_set_sha256=expectation_sha,
        expected_ticker_set_sha256=ticker_sha,
        assignment_policy=ASSIGNMENT_POLICY,
    )


def _validate_existing_closure_assignments(
    closure: ObservationSlotClosure,
    assignments: Iterable[ExpectationCycleAssignment],
) -> None:
    bound = tuple(item for item in assignments
                  if item.observation_slot_id == closure.observation_slot_id)
    if closure.closure_state == ASSIGNED_EXACT_CYCLE:
        if len(bound) != closure.assignment_count \
                or tuple(item.expectation_key for item in bound) \
                != closure.expected_expectation_keys \
                or any(item.observation_cycle_id != closure.authoritative_cycle_id
                       or item.expected_expectation_set_sha256
                       != closure.expected_expectation_set_sha256
                       or item.expected_ticker_set_sha256
                       != closure.expected_ticker_set_sha256
                       for item in bound):
            raise SlotAssignmentError("terminal slot closure assignments are inconsistent")
    elif bound:
        raise SlotAssignmentError("non-assigned terminal slot has assignment records")


def close_observation_slot(
    store: ValidationStore, *, iso_year: int, iso_week: int, closed_at_utc: str,
) -> ObservationSlotClosure:
    """Terminally close one slot and atomically append exact-set assignments."""

    try:
        slot = weekly_observation_slot(iso_year, iso_week)
        closed = parse_utc(closed_at_utc, "closed_at_utc")
        finality = parse_utc(slot.finality_eligible_at_utc, "slot finality")
    except ValidationContractError as exc:
        raise SlotAssignmentError("slot closure input is invalid") from exc
    if closed < finality:
        raise SlotAssignmentError("slot cannot close before finality")

    closures = load_slot_closures(store)
    existing = next((item for item in closures
                     if item.observation_slot_id == slot.observation_slot_id), None)
    expectations = load_current_expectations(store)
    assignments = load_expectation_assignments(store, expectations)
    if existing is not None:
        _validate_existing_closure_assignments(existing, assignments)
        return existing

    expected, keys, expectation_sha, tickers, ticker_sha = _expected_sets(
        expectations, slot, assignments,
    )
    registrations = load_registered_cycles(store)
    same_target = tuple(item for item in registrations
                        if item.observation_target_session == slot.slot_target_session)
    exact = tuple(item for item in same_target if _is_exact_candidate(
        item, expectation_keys=keys, expectation_sha=expectation_sha,
        tickers=tickers, ticker_sha=ticker_sha,
    )) if expected else ()
    exact_ids = tuple(sorted(item.observation_cycle_id for item in exact))
    same_ids = tuple(sorted(item.observation_cycle_id for item in same_target))
    nonexact_ids = tuple(sorted(set(same_ids) - set(exact_ids)))

    if not expected:
        state = EMPTY_NO_EXPECTATIONS
        authoritative = None
        new_assignments: tuple[ExpectationCycleAssignment, ...] = ()
    elif len(exact) == 1:
        state = ASSIGNED_EXACT_CYCLE
        authoritative = exact[0].observation_cycle_id
        new_assignments = tuple(_assignment(
            item, slot, exact[0], expectation_sha, ticker_sha,
        ) for item in expected)
    elif not exact:
        state = OPERATIONAL_OBSERVATION_MISSING
        authoritative = None
        new_assignments = ()
    else:
        state = AMBIGUOUS_OPERATIONAL_ERROR
        authoritative = None
        new_assignments = ()

    closure = ObservationSlotClosure(
        protocol_id=PROTOCOL_ID, protocol_hash=PROTOCOL_HASH,
        protocol_epoch=PROTOCOL_EPOCH, observation_slot_id=slot.observation_slot_id,
        iso_year=slot.iso_year, iso_week=slot.iso_week,
        slot_target_session=slot.slot_target_session,
        finality_eligible_at_utc=slot.finality_eligible_at_utc,
        closure_state=state, closed_at_utc=_utc_string(closed),
        expected_expectation_keys=keys,
        expected_expectation_set_sha256=expectation_sha,
        expected_tickers=tickers, expected_ticker_set_sha256=ticker_sha,
        same_target_cycle_ids=same_ids, exact_candidate_cycle_ids=exact_ids,
        nonexact_candidate_cycle_ids=nonexact_ids,
        authoritative_cycle_id=authoritative,
        assignment_count=len(new_assignments),
    )

    # Preflight every identity before opening the atomic write scope.
    if store.connection.execute(
        "SELECT 1 FROM validation_record WHERE record_kind=? AND record_key=?",
        (OBSERVATION_SLOT_CLOSURE, closure.key),
    ).fetchone() is not None:
        raise SlotAssignmentError("slot closure appeared concurrently")
    for item in new_assignments:
        if store.connection.execute(
            "SELECT 1 FROM validation_record WHERE record_kind=? AND record_key=?",
            (EXPECTATION_CYCLE_ASSIGNMENT, item.key),
        ).fetchone() is not None:
            raise SlotAssignmentError("expectation assignment appeared concurrently")

    store.connection.execute("SAVEPOINT authoritative_slot_close")
    try:
        if not store.append(OBSERVATION_SLOT_CLOSURE, closure.key, closure.to_dict()):
            raise SlotAssignmentError("slot closure append was unexpectedly idempotent")
        for item in new_assignments:
            if not store.append(
                EXPECTATION_CYCLE_ASSIGNMENT, item.key, item.to_dict(),
            ):
                raise SlotAssignmentError("assignment append was unexpectedly idempotent")
        store.connection.execute("RELEASE SAVEPOINT authoritative_slot_close")
    except Exception:
        store.connection.execute("ROLLBACK TO SAVEPOINT authoritative_slot_close")
        store.connection.execute("RELEASE SAVEPOINT authoritative_slot_close")
        raise
    return closure
