"""Fail-closed finalization of authoritatively assigned maturity outcomes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import math
from pathlib import Path
from typing import Any, Iterable, Mapping

from .contracts import OFFICIAL_ELIGIBLE, SMOKE_ONLY, ValidationContractError
from .cycle_registry import ObservationCycleRegistration, load_registered_cycles
from .market_calendar import parse_utc
from .observation_cycle import MANIFEST_NAME, ObservationCycleError, verify_observation_cycle
from .outcome_expectations import OutcomeExpectation, outcome_identity
from .protocol_v1 import (
    HORIZONS,
    PROTOCOL,
    PROTOCOL_EPOCH,
    PROTOCOL_HASH,
    PROTOCOL_ID,
    canonical_json,
)
from .slot_assignment import (
    ASSIGNED_EXACT_CYCLE,
    ExpectationCycleAssignment,
    ObservationSlotClosure,
    load_current_expectations,
    load_expectation_assignments,
    load_slot_closures,
)
from .spy_benchmark import BenchmarkSnapshotError, load_spy_benchmark_snapshot
from .stock_observation import StockObservationError, load_stock_observation_snapshot
from .storage import ValidationStore


OUTCOME_FINAL = "OUTCOME_FINAL"
COMPLETE = "COMPLETE"
DATA_QUALITY_FAILURE = "DATA_QUALITY_FAILURE"

ALREADY_FINAL = "ALREADY_FINAL"
PENDING_UNASSIGNED = "PENDING_UNASSIGNED"
PENDING_SOURCE_NOT_AVAILABLE = "PENDING_SOURCE_NOT_AVAILABLE"
FINALIZED_COMPLETE = "FINALIZED_COMPLETE"
FINALIZED_DATA_QUALITY_FAILURE = "FINALIZED_DATA_QUALITY_FAILURE"

TERMINAL_STOCK_D1_UNAVAILABLE = "TERMINAL_STOCK_D1_UNAVAILABLE"
TERMINAL_STOCK_DN_UNAVAILABLE = "TERMINAL_STOCK_DN_UNAVAILABLE"
_DQF_FLAGS = frozenset({TERMINAL_STOCK_D1_UNAVAILABLE, TERMINAL_STOCK_DN_UNAVAILABLE})
_STOCK_CLASSIFICATIONS = frozenset({
    "ACTIVE_COMPLETE", "CORPORATE_ACTION_NO_LONGER_TRADING",
})

_EXPECTED_POLICY = {
    "pending_is_terminal": False,
    "pending_persisted_as_outcome_final": False,
    "unassigned_expectation": "PENDING",
    "assigned_source_not_available": "PENDING",
    "supplied_source_verification_failure": "OPERATIONAL_ERROR_NO_FINAL",
    "dqf_requires_authoritative_assignment": True,
    "dqf_requires_positive_verified_evidence": True,
    "terminal_stock_missing_required_session": "DATA_QUALITY_FAILURE",
    "active_stock_missing_required_session": "CONTRACT_ERROR_NO_FINAL",
    "benchmark_missing_required_session": "CONTRACT_ERROR_NO_FINAL",
    "forward_fill_missing_session": False,
    "complete_return": "D1_OPEN_TO_DN_CLOSE",
    "benchmark_return": "D1_OPEN_TO_DN_CLOSE",
    "active_return": "STOCK_RETURN_MINUS_BENCHMARK_RETURN",
    "outcome_final_is_terminal": True,
    "outcome_restatement": False,
}
if dict(PROTOCOL.get("outcome_finalization_policy", {})) != _EXPECTED_POLICY:
    raise RuntimeError("TRINITY V1 outcome-finalization policy drift")


class OutcomeFinalizationError(ValidationContractError):
    """Outcome finalization cannot prove its authoritative evidence chain."""


def _sha(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise OutcomeFinalizationError(f"{field} must be a lowercase SHA-256")
    return value


def _payload_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(value))).hexdigest()


def _portable_identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or "/" in value or "\\" in value \
            or any(ord(character) < 32 for character in value):
        raise OutcomeFinalizationError(f"{field} must be a portable identifier")
    return value


def _session(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise OutcomeFinalizationError(f"{field} must be an ISO session date")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise OutcomeFinalizationError(f"{field} must be an ISO session date") from exc
    return value


def _finite(value: object, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OutcomeFinalizationError(f"{field} must be numeric")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        raise OutcomeFinalizationError(f"{field} is not a valid finite value")
    return number


@dataclass(frozen=True)
class CycleSourceLocator:
    observation_cycle_id: str
    cycle_root: str | Path
    stock_root: str | Path
    spy_root: str | Path

    def __post_init__(self) -> None:
        _sha(self.observation_cycle_id, "observation_cycle_id")
        for field in ("cycle_root", "stock_root", "spy_root"):
            value = getattr(self, field)
            if not isinstance(value, (str, Path)) or not str(value):
                raise OutcomeFinalizationError(f"{field} must identify a runtime source")


@dataclass(frozen=True)
class OutcomeFinal:
    protocol_id: str
    protocol_hash: str
    protocol_epoch: int
    expectation_key: str
    expectation_payload_sha256: str
    run_id: str
    eligibility: str
    ticker: str
    cohort: str
    horizon_sessions: int
    decision_timestamp_utc: str
    decision_target_session: str
    expected_first_tradable_session: str
    maturity_session: str
    status: str
    stock_classification: str
    first_tradable_price: float | None
    future_price: float | None
    benchmark_first_tradable_price: float | None
    benchmark_future_price: float | None
    stock_return_pct: float | None
    benchmark_return_pct: float | None
    active_return_pct: float | None
    observation_slot_id: str
    slot_target_session: str
    observation_cycle_id: str
    observation_cycle_sha256: str
    cycle_manifest_sha256: str
    stock_snapshot_id: str
    stock_snapshot_sha256: str
    stock_manifest_sha256: str
    spy_snapshot_id: str
    spy_snapshot_sha256: str
    spy_manifest_sha256: str
    quality_flags: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.protocol_id, str) or not isinstance(self.protocol_hash, str) \
                or type(self.protocol_epoch) is not int:
            raise OutcomeFinalizationError("outcome final protocol identity types are invalid")
        if (self.protocol_id, self.protocol_hash, self.protocol_epoch) != (
            PROTOCOL_ID, PROTOCOL_HASH, PROTOCOL_EPOCH,
        ):
            raise OutcomeFinalizationError("outcome final protocol identity mismatch")
        if self.eligibility not in {SMOKE_ONLY, OFFICIAL_ELIGIBLE}:
            raise OutcomeFinalizationError("outcome final eligibility is invalid")
        try:
            expected_key = outcome_identity(
                self.run_id, self.ticker, self.cohort, self.horizon_sessions,
            )
        except ValidationContractError as exc:
            raise OutcomeFinalizationError("outcome final expectation identity is invalid") from exc
        if self.expectation_key != expected_key or self.horizon_sessions not in HORIZONS:
            raise OutcomeFinalizationError("outcome final expectation key mismatch")
        _sha(self.expectation_payload_sha256, "expectation_payload_sha256")
        try:
            parse_utc(self.decision_timestamp_utc, "decision_timestamp_utc")
        except ValidationContractError as exc:
            raise OutcomeFinalizationError("outcome final decision timestamp is invalid") from exc
        _session(self.decision_target_session, "decision_target_session")
        d1 = _session(
            self.expected_first_tradable_session, "expected_first_tradable_session",
        )
        dn = _session(self.maturity_session, "maturity_session")
        if d1 > dn:
            raise OutcomeFinalizationError("outcome final D1/DN ordering is invalid")
        _session(self.slot_target_session, "slot_target_session")
        if self.slot_target_session < dn:
            raise OutcomeFinalizationError("outcome final slot precedes maturity")
        if self.stock_classification not in _STOCK_CLASSIFICATIONS:
            raise OutcomeFinalizationError("outcome final stock classification is invalid")
        for field in (
            "observation_slot_id", "observation_cycle_id", "observation_cycle_sha256",
            "cycle_manifest_sha256", "stock_snapshot_sha256", "stock_manifest_sha256",
            "spy_snapshot_sha256", "spy_manifest_sha256",
        ):
            _sha(getattr(self, field), field)
        _portable_identifier(self.stock_snapshot_id, "stock_snapshot_id")
        _portable_identifier(self.spy_snapshot_id, "spy_snapshot_id")
        if not isinstance(self.quality_flags, (list, tuple)):
            raise OutcomeFinalizationError("quality_flags must be a list")
        flags = tuple(self.quality_flags)
        if any(not isinstance(flag, str) for flag in flags) \
                or flags != tuple(sorted(flags)) or len(flags) != len(set(flags)):
            raise OutcomeFinalizationError("quality_flags must be unique and ordered")
        numeric = (
            self.first_tradable_price, self.future_price,
            self.benchmark_first_tradable_price, self.benchmark_future_price,
            self.stock_return_pct, self.benchmark_return_pct, self.active_return_pct,
        )
        if self.status == COMPLETE:
            if any(value is None for value in numeric) or flags:
                raise OutcomeFinalizationError("COMPLETE outcome final invariants are invalid")
            stock_open = _finite(self.first_tradable_price, "first_tradable_price", positive=True)
            stock_close = _finite(self.future_price, "future_price", positive=True)
            spy_open = _finite(
                self.benchmark_first_tradable_price,
                "benchmark_first_tradable_price", positive=True,
            )
            spy_close = _finite(
                self.benchmark_future_price, "benchmark_future_price", positive=True,
            )
            stock_return = _finite(self.stock_return_pct, "stock_return_pct")
            benchmark_return = _finite(self.benchmark_return_pct, "benchmark_return_pct")
            active_return = _finite(self.active_return_pct, "active_return_pct")
            calculated_stock = (stock_close / stock_open - 1.0) * 100.0
            calculated_benchmark = (spy_close / spy_open - 1.0) * 100.0
            if not math.isclose(stock_return, calculated_stock, rel_tol=1e-12, abs_tol=1e-12) \
                    or not math.isclose(
                        benchmark_return, calculated_benchmark,
                        rel_tol=1e-12, abs_tol=1e-12,
                    ) or not math.isclose(
                        active_return, stock_return - benchmark_return,
                        rel_tol=1e-12, abs_tol=1e-12,
                    ):
                raise OutcomeFinalizationError("COMPLETE outcome return arithmetic mismatch")
        elif self.status == DATA_QUALITY_FAILURE:
            if any(value is not None for value in numeric) or not flags \
                    or not set(flags).issubset(_DQF_FLAGS) \
                    or self.stock_classification != "CORPORATE_ACTION_NO_LONGER_TRADING":
                raise OutcomeFinalizationError("DATA_QUALITY_FAILURE invariants are invalid")
        else:
            raise OutcomeFinalizationError("outcome final status must be terminal")
        object.__setattr__(self, "quality_flags", flags)

    @property
    def key(self) -> str:
        return self.expectation_key

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OutcomeFinal":
        if not isinstance(value, Mapping):
            raise OutcomeFinalizationError("outcome final payload must be an object")
        try:
            return cls(**dict(value))
        except TypeError as exc:
            raise OutcomeFinalizationError("outcome final payload shape is invalid") from exc


@dataclass(frozen=True)
class OutcomeResolution:
    expectation_key: str
    state: str
    reason_codes: tuple[str, ...] = ()
    outcome_final: OutcomeFinal | None = None


@dataclass(frozen=True)
class MaturitySummary:
    expectations_total: int
    already_final: int
    pending_unassigned: int
    pending_source_unavailable: int
    completed: int
    data_quality_failure: int
    new_final_records: int
    operational_errors: int
    resolutions: tuple[OutcomeResolution, ...]


def load_outcome_finals(
    store: ValidationStore, expectations: Iterable[OutcomeExpectation],
) -> tuple[OutcomeFinal, ...]:
    expectation_by_key = {item.key: item for item in expectations}
    result: list[OutcomeFinal] = []
    for record_key, payload in store.record_items(OUTCOME_FINAL):
        if not isinstance(payload, Mapping):
            raise OutcomeFinalizationError("stored OUTCOME_FINAL is not an object")
        protocol_hash = payload.get("protocol_hash")
        if not isinstance(protocol_hash, str):
            raise OutcomeFinalizationError("stored OUTCOME_FINAL protocol identity is malformed")
        _sha(protocol_hash, "stored OUTCOME_FINAL protocol_hash")
        if protocol_hash != PROTOCOL_HASH:
            continue
        try:
            final = OutcomeFinal.from_dict(payload)
        except (TypeError, ValueError, ValidationContractError) as exc:
            raise OutcomeFinalizationError("stored current-protocol OUTCOME_FINAL is invalid") from exc
        if record_key != final.expectation_key:
            raise OutcomeFinalizationError("OUTCOME_FINAL record key and expectation differ")
        expectation = expectation_by_key.get(final.expectation_key)
        if expectation is None:
            raise OutcomeFinalizationError("OUTCOME_FINAL references no current expectation")
        if _payload_sha256(expectation.to_dict()) != final.expectation_payload_sha256:
            raise OutcomeFinalizationError("OUTCOME_FINAL expectation payload binding mismatch")
        if (
            final.run_id, final.eligibility, final.ticker, final.cohort,
            final.horizon_sessions, final.decision_timestamp_utc,
            final.decision_target_session, final.expected_first_tradable_session,
            final.maturity_session,
        ) != (
            expectation.run_id, expectation.eligibility, expectation.ticker,
            expectation.cohort, expectation.horizon_sessions,
            expectation.decision_timestamp_utc, expectation.decision_target_session,
            expectation.expected_first_tradable_session, expectation.maturity_session,
        ):
            raise OutcomeFinalizationError("OUTCOME_FINAL expectation fields mismatch")
        result.append(final)
    keys = [item.expectation_key for item in result]
    if len(keys) != len(set(keys)):
        raise OutcomeFinalizationError("duplicate current-protocol OUTCOME_FINAL")
    return tuple(sorted(result, key=lambda item: item.expectation_key))


def _locator_map(
    cycle_sources: Iterable[CycleSourceLocator],
) -> dict[str, CycleSourceLocator]:
    if isinstance(cycle_sources, (str, bytes, Mapping)):
        raise OutcomeFinalizationError("cycle_sources must be an iterable of locators")
    result: dict[str, CycleSourceLocator] = {}
    try:
        supplied = tuple(cycle_sources)
    except TypeError as exc:
        raise OutcomeFinalizationError("cycle_sources is invalid") from exc
    for value in supplied:
        if not isinstance(value, CycleSourceLocator):
            raise OutcomeFinalizationError("cycle_sources contains an invalid locator")
        if value.observation_cycle_id in result:
            raise OutcomeFinalizationError("duplicate observation_cycle_id locator")
        result[value.observation_cycle_id] = value
    return result


def _authoritative_links(
    expectation: OutcomeExpectation, assignment: ExpectationCycleAssignment,
    closure_by_id: Mapping[str, ObservationSlotClosure],
    registration_by_id: Mapping[str, ObservationCycleRegistration],
) -> tuple[ObservationSlotClosure, ObservationCycleRegistration]:
    expectation_sha = _payload_sha256(expectation.to_dict())
    if assignment.expectation_key != expectation.key \
            or assignment.expectation_payload_sha256 != expectation_sha:
        raise OutcomeFinalizationError("assignment expectation lineage mismatch")
    closure = closure_by_id.get(assignment.observation_slot_id)
    if closure is None or closure.closure_state != ASSIGNED_EXACT_CYCLE \
            or closure.authoritative_cycle_id != assignment.observation_cycle_id \
            or assignment.expectation_key not in closure.expected_expectation_keys \
            or assignment.slot_target_session != closure.slot_target_session \
            or assignment.expected_expectation_set_sha256 \
            != closure.expected_expectation_set_sha256 \
            or assignment.expected_ticker_set_sha256 != closure.expected_ticker_set_sha256:
        raise OutcomeFinalizationError("authoritative slot closure lineage mismatch")
    registration = registration_by_id.get(assignment.observation_cycle_id)
    if registration is None \
            or assignment.expectation_key not in registration.expectation_keys \
            or assignment.observation_cycle_sha256 != registration.observation_cycle_sha256 \
            or assignment.cycle_manifest_sha256 != registration.cycle_manifest_sha256 \
            or assignment.expected_expectation_set_sha256 != registration.expectation_set_sha256 \
            or assignment.expected_ticker_set_sha256 \
            != registration.expected_ticker_set_sha256 \
            or assignment.slot_target_session != registration.observation_target_session:
        raise OutcomeFinalizationError("registered observation cycle lineage mismatch")
    return closure, registration


def _bar_by_session(bars: Iterable[Mapping[str, object]]) -> dict[str, Mapping[str, object]]:
    result: dict[str, Mapping[str, object]] = {}
    for bar in bars:
        session = str(bar.get("date") or "")
        if not session or session in result:
            raise OutcomeFinalizationError("verified observation bars are not uniquely indexed")
        result[session] = bar
    return result


def _price(bar: Mapping[str, object], field: str, label: str) -> float:
    try:
        value = float(bar[field])
    except (KeyError, TypeError, ValueError) as exc:
        raise OutcomeFinalizationError(f"{label} is not numeric") from exc
    if not math.isfinite(value) or value <= 0:
        raise OutcomeFinalizationError(f"{label} is not positive and finite")
    return value


def _verify_sources(
    *, locator: CycleSourceLocator, registration: ObservationCycleRegistration,
    cycle_expectations: tuple[OutcomeExpectation, ...],
) -> tuple[Any, Any]:
    try:
        manifest = verify_observation_cycle(
            locator.cycle_root, expectations=cycle_expectations,
            stock_root=locator.stock_root, spy_root=locator.spy_root,
        )
        stock = load_stock_observation_snapshot(
            locator.stock_root, expected_tickers=registration.expected_tickers,
            expected_observation_target_session=registration.observation_target_session,
        )
        spy = load_spy_benchmark_snapshot(
            locator.spy_root,
            trinity_target_session=registration.observation_target_session,
        )
        manifest_sha = hashlib.sha256(
            (Path(locator.cycle_root) / MANIFEST_NAME).read_bytes()
        ).hexdigest()
    except (OSError, ObservationCycleError, StockObservationError,
            BenchmarkSnapshotError, ValidationContractError) as exc:
        raise OutcomeFinalizationError(
            f"supplied cycle source verification failed: {exc}"
        ) from exc
    if locator.observation_cycle_id != registration.observation_cycle_id \
            or manifest.get("observation_cycle_id") != registration.observation_cycle_id \
            or manifest.get("observation_cycle_sha256") \
            != registration.observation_cycle_sha256 \
            or manifest_sha != registration.cycle_manifest_sha256:
        raise OutcomeFinalizationError("cycle source identity mismatch")
    if stock.source_snapshot_id != registration.stock_snapshot_id \
            or stock.source_snapshot_sha256 != registration.stock_snapshot_sha256 \
            or spy.snapshot_id != registration.spy_snapshot_id \
            or spy.snapshot_sha256 != registration.spy_snapshot_sha256:
        raise OutcomeFinalizationError("source snapshot identity mismatch")
    try:
        stock_manifest_sha = hashlib.sha256(
            (Path(locator.stock_root) / "snapshot_manifest.json").read_bytes()
        ).hexdigest()
        spy_manifest_sha = hashlib.sha256(
            (Path(locator.spy_root) / "benchmark_manifest.json").read_bytes()
        ).hexdigest()
    except OSError as exc:
        raise OutcomeFinalizationError("source snapshot manifest is unavailable") from exc
    if stock_manifest_sha != registration.stock_manifest_sha256 \
            or spy_manifest_sha != registration.spy_manifest_sha256:
        raise OutcomeFinalizationError("source snapshot manifest lineage mismatch")
    return stock, spy


def _build_final(
    expectation: OutcomeExpectation, assignment: ExpectationCycleAssignment,
    registration: ObservationCycleRegistration, stock: Any, spy: Any,
) -> OutcomeFinal:
    stock_series = stock.series(expectation.ticker)
    stock_bars = _bar_by_session(stock_series.bars)
    spy_bars = _bar_by_session(spy.bars)
    d1 = expectation.expected_first_tradable_session
    dn = expectation.maturity_session
    stock_d1 = stock_bars.get(d1)
    stock_dn = stock_bars.get(dn)
    spy_d1 = spy_bars.get(d1)
    spy_dn = spy_bars.get(dn)
    if spy_d1 is None or spy_dn is None:
        missing = "D1" if spy_d1 is None else "DN"
        raise OutcomeFinalizationError(f"verified SPY source is missing required {missing} session")

    common = dict(
        protocol_id=PROTOCOL_ID, protocol_hash=PROTOCOL_HASH,
        protocol_epoch=PROTOCOL_EPOCH, expectation_key=expectation.key,
        expectation_payload_sha256=_payload_sha256(expectation.to_dict()),
        run_id=expectation.run_id, eligibility=expectation.eligibility,
        ticker=expectation.ticker, cohort=expectation.cohort,
        horizon_sessions=expectation.horizon_sessions,
        decision_timestamp_utc=expectation.decision_timestamp_utc,
        decision_target_session=expectation.decision_target_session,
        expected_first_tradable_session=d1, maturity_session=dn,
        stock_classification=stock_series.classification,
        observation_slot_id=assignment.observation_slot_id,
        slot_target_session=assignment.slot_target_session,
        observation_cycle_id=registration.observation_cycle_id,
        observation_cycle_sha256=registration.observation_cycle_sha256,
        cycle_manifest_sha256=registration.cycle_manifest_sha256,
        stock_snapshot_id=registration.stock_snapshot_id,
        stock_snapshot_sha256=registration.stock_snapshot_sha256,
        stock_manifest_sha256=registration.stock_manifest_sha256,
        spy_snapshot_id=registration.spy_snapshot_id,
        spy_snapshot_sha256=registration.spy_snapshot_sha256,
        spy_manifest_sha256=registration.spy_manifest_sha256,
    )
    if stock_d1 is None or stock_dn is None:
        if stock_series.classification == "ACTIVE_COMPLETE":
            missing = "D1" if stock_d1 is None else "DN"
            raise OutcomeFinalizationError(
                f"ACTIVE_COMPLETE stock is missing required {missing} session"
            )
        flags = tuple(sorted(
            flag for missing, flag in (
                (stock_d1 is None, TERMINAL_STOCK_D1_UNAVAILABLE),
                (stock_dn is None, TERMINAL_STOCK_DN_UNAVAILABLE),
            ) if missing
        ))
        return OutcomeFinal(
            **common, status=DATA_QUALITY_FAILURE,
            first_tradable_price=None, future_price=None,
            benchmark_first_tradable_price=None, benchmark_future_price=None,
            stock_return_pct=None, benchmark_return_pct=None, active_return_pct=None,
            quality_flags=flags,
        )

    stock_open = _price(stock_d1, "open", "stock D1 OPEN")
    stock_close = _price(stock_dn, "close", "stock DN CLOSE")
    spy_open = _price(spy_d1, "open", "SPY D1 OPEN")
    spy_close = _price(spy_dn, "close", "SPY DN CLOSE")
    stock_return = (stock_close / stock_open - 1.0) * 100.0
    benchmark_return = (spy_close / spy_open - 1.0) * 100.0
    return OutcomeFinal(
        **common, status=COMPLETE,
        first_tradable_price=stock_open, future_price=stock_close,
        benchmark_first_tradable_price=spy_open,
        benchmark_future_price=spy_close,
        stock_return_pct=stock_return, benchmark_return_pct=benchmark_return,
        active_return_pct=stock_return - benchmark_return, quality_flags=(),
    )


def _validate_final_lineage(
    final: OutcomeFinal, expectation: OutcomeExpectation,
    assignment: ExpectationCycleAssignment,
    closure_by_id: Mapping[str, ObservationSlotClosure],
    registration_by_id: Mapping[str, ObservationCycleRegistration],
) -> None:
    _closure, registration = _authoritative_links(
        expectation, assignment, closure_by_id, registration_by_id,
    )
    if final.observation_slot_id != assignment.observation_slot_id \
            or final.slot_target_session != assignment.slot_target_session \
            or final.observation_cycle_id != registration.observation_cycle_id \
            or final.observation_cycle_sha256 != registration.observation_cycle_sha256 \
            or final.cycle_manifest_sha256 != registration.cycle_manifest_sha256 \
            or final.stock_snapshot_id != registration.stock_snapshot_id \
            or final.stock_snapshot_sha256 != registration.stock_snapshot_sha256 \
            or final.stock_manifest_sha256 != registration.stock_manifest_sha256 \
            or final.spy_snapshot_id != registration.spy_snapshot_id \
            or final.spy_snapshot_sha256 != registration.spy_snapshot_sha256 \
            or final.spy_manifest_sha256 != registration.spy_manifest_sha256:
        raise OutcomeFinalizationError("existing OUTCOME_FINAL lineage mismatch")


def finalize_outcomes(
    store: ValidationStore, *, cycle_sources: Iterable[CycleSourceLocator] = (),
) -> MaturitySummary:
    """Resolve current expectations and persist only terminal COMPLETE/DQF records."""

    locators = _locator_map(cycle_sources)
    expectations = load_current_expectations(store)
    expectation_by_key = {item.key: item for item in expectations}
    assignments = load_expectation_assignments(store, expectations)
    assignment_by_key = {item.expectation_key: item for item in assignments}
    closures = load_slot_closures(store)
    closure_by_id = {item.observation_slot_id: item for item in closures}
    registrations = load_registered_cycles(store)
    registration_by_id = {item.observation_cycle_id: item for item in registrations}
    existing_finals = load_outcome_finals(store, expectations)
    final_by_key = {item.expectation_key: item for item in existing_finals}

    cycle_expectations: dict[str, tuple[OutcomeExpectation, ...]] = {}
    verified_sources: dict[str, tuple[Any, Any]] = {}
    resolutions: list[OutcomeResolution] = []
    new_finals: list[OutcomeFinal] = []
    for expectation in expectations:
        assignment = assignment_by_key.get(expectation.key)
        existing = final_by_key.get(expectation.key)
        if existing is not None:
            if assignment is None:
                raise OutcomeFinalizationError("terminal outcome has no authoritative assignment")
            _validate_final_lineage(
                existing, expectation, assignment, closure_by_id, registration_by_id,
            )
            resolutions.append(OutcomeResolution(
                expectation.key, ALREADY_FINAL, ("TERMINAL_RECORD_PRESENT",), existing,
            ))
            continue
        if assignment is None:
            resolutions.append(OutcomeResolution(
                expectation.key, PENDING_UNASSIGNED, ("NO_AUTHORITATIVE_ASSIGNMENT",),
            ))
            continue
        _closure, registration = _authoritative_links(
            expectation, assignment, closure_by_id, registration_by_id,
        )
        locator = locators.get(registration.observation_cycle_id)
        if locator is None:
            resolutions.append(OutcomeResolution(
                expectation.key, PENDING_SOURCE_NOT_AVAILABLE,
                ("AUTHORITATIVE_SOURCE_NOT_AVAILABLE",),
            ))
            continue
        if registration.observation_cycle_id not in cycle_expectations:
            try:
                cycle_expectations[registration.observation_cycle_id] = tuple(
                    expectation_by_key[key] for key in registration.expectation_keys
                )
            except KeyError as exc:
                raise OutcomeFinalizationError(
                    "registered cycle references no current expectation"
                ) from exc
        if registration.observation_cycle_id not in verified_sources:
            verified_sources[registration.observation_cycle_id] = _verify_sources(
                locator=locator, registration=registration,
                cycle_expectations=cycle_expectations[registration.observation_cycle_id],
            )
        stock, spy = verified_sources[registration.observation_cycle_id]
        final = _build_final(expectation, assignment, registration, stock, spy)
        new_finals.append(final)
        state = FINALIZED_COMPLETE if final.status == COMPLETE \
            else FINALIZED_DATA_QUALITY_FAILURE
        resolutions.append(OutcomeResolution(expectation.key, state, (), final))

    store.connection.execute("SAVEPOINT outcome_finalization")
    try:
        for final in new_finals:
            if not store.append(OUTCOME_FINAL, final.key, final.to_dict()):
                raise OutcomeFinalizationError("OUTCOME_FINAL appeared concurrently")
        store.connection.execute("RELEASE SAVEPOINT outcome_finalization")
    except Exception:
        store.connection.execute("ROLLBACK TO SAVEPOINT outcome_finalization")
        store.connection.execute("RELEASE SAVEPOINT outcome_finalization")
        raise

    states = [item.state for item in resolutions]
    return MaturitySummary(
        expectations_total=len(expectations),
        already_final=states.count(ALREADY_FINAL),
        pending_unassigned=states.count(PENDING_UNASSIGNED),
        pending_source_unavailable=states.count(PENDING_SOURCE_NOT_AVAILABLE),
        completed=states.count(FINALIZED_COMPLETE),
        data_quality_failure=states.count(FINALIZED_DATA_QUALITY_FAILURE),
        new_final_records=len(new_finals), operational_errors=0,
        resolutions=tuple(resolutions),
    )
