"""Authoritative weekly baskets derived from immutable validation records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math
from statistics import fmean
from types import MappingProxyType
from typing import Mapping

from .contracts import OFFICIAL_ELIGIBLE, ValidationContractError
from .outcome_expectations import OutcomeExpectation, outcome_identity
from .outcome_finalization import (
    COMPLETE, DATA_QUALITY_FAILURE, OutcomeFinal, load_outcome_finals,
)
from .protocol_v1 import HORIZONS, PROTOCOL, PROTOCOL_EPOCH, PROTOCOL_HASH, PROTOCOL_ID
from .run_observation import RunObservation, load_run_observations
from .slot_assignment import load_current_expectations
from .storage import ValidationStore


AVAILABLE = "AVAILABLE"
EMPTY_BENCHMARK = "EMPTY_BENCHMARK"
PENDING = "PENDING"
UNAVAILABLE_DQF = "UNAVAILABLE_DQF"

_COHORTS = ("SCANNER", "LUNA", "SOL")
_STATUSES = frozenset({AVAILABLE, EMPTY_BENCHMARK, PENDING, UNAVAILABLE_DQF})
_POLICY = PROTOCOL["weekly_analytics_policy"]
_COST_BPS = tuple(int(value) for value in _POLICY["trading_cost_scenarios_bps_per_side"])
if _COST_BPS != (0, 10, 25):
    raise RuntimeError("TRINITY V1 weekly basket cost policy drift")


class WeeklyBasketError(ValidationContractError):
    """Immutable validation records cannot form authoritative weekly baskets."""


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WeeklyBasketError(f"{field} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise WeeklyBasketError(f"{field} must be finite")
    return number


@dataclass(frozen=True)
class StageBasket:
    protocol_id: str
    protocol_hash: str
    protocol_epoch: int
    run_id: str
    eligibility: str
    decision_target_session: str
    cohort: str
    horizon_sessions: int
    members: tuple[str, ...]
    member_count: int
    complete_count: int
    pending_count: int
    dqf_count: int
    status: str
    gross_active_return_pct: float | None
    cost_adjusted_active_return_pct: Mapping[int, float | None]

    def __post_init__(self) -> None:
        if (self.protocol_id, self.protocol_hash, self.protocol_epoch) != (
            PROTOCOL_ID, PROTOCOL_HASH, PROTOCOL_EPOCH,
        ):
            raise WeeklyBasketError("stage basket protocol identity mismatch")
        if self.eligibility != OFFICIAL_ELIGIBLE or not self.run_id:
            raise WeeklyBasketError("stage basket is not an official run unit")
        try:
            date.fromisoformat(self.decision_target_session)
        except (TypeError, ValueError) as exc:
            raise WeeklyBasketError("stage basket decision session is invalid") from exc
        if self.cohort not in _COHORTS or self.horizon_sessions not in HORIZONS:
            raise WeeklyBasketError("stage basket cohort or horizon is invalid")
        members = tuple(self.members)
        if members != tuple(sorted(members)) or len(members) != len(set(members)) \
                or any(not isinstance(member, str) or not member for member in members):
            raise WeeklyBasketError("stage basket members must be unique and ordered")
        counts = (self.member_count, self.complete_count, self.pending_count, self.dqf_count)
        if any(type(value) is not int or value < 0 for value in counts) \
                or self.member_count != len(members) \
                or self.member_count != self.complete_count + self.pending_count + self.dqf_count:
            raise WeeklyBasketError("stage basket member counts are inconsistent")
        if self.status not in _STATUSES:
            raise WeeklyBasketError("stage basket status is invalid")
        if not isinstance(self.cost_adjusted_active_return_pct, Mapping) \
                or tuple(sorted(self.cost_adjusted_active_return_pct)) != _COST_BPS:
            raise WeeklyBasketError("stage basket cost scenarios are invalid")
        costs = {bps: self.cost_adjusted_active_return_pct[bps] for bps in _COST_BPS}
        if self.status == EMPTY_BENCHMARK:
            if self.member_count != 0 or self.gross_active_return_pct != 0.0 \
                    or any(value != 0.0 for value in costs.values()):
                raise WeeklyBasketError("EMPTY_BENCHMARK invariants are invalid")
        elif self.status == AVAILABLE:
            if self.member_count == 0 or self.complete_count != self.member_count \
                    or self.pending_count or self.dqf_count \
                    or self.gross_active_return_pct is None:
                raise WeeklyBasketError("AVAILABLE basket invariants are invalid")
            gross = _finite(self.gross_active_return_pct, "gross_active_return_pct")
            for bps, value in costs.items():
                adjusted = _finite(value, f"cost adjusted return at {bps} bps")
                if not math.isclose(
                    adjusted, gross - (2.0 * bps / 100.0),
                    rel_tol=1e-12, abs_tol=1e-12,
                ):
                    raise WeeklyBasketError("stage basket cost arithmetic mismatch")
        elif self.status == PENDING:
            if self.member_count == 0 or self.pending_count == 0 or self.dqf_count \
                    or self.gross_active_return_pct is not None \
                    or any(value is not None for value in costs.values()):
                raise WeeklyBasketError("PENDING basket invariants are invalid")
        elif self.member_count == 0 or self.dqf_count == 0 \
                or self.gross_active_return_pct is not None \
                or any(value is not None for value in costs.values()):
            raise WeeklyBasketError("UNAVAILABLE_DQF basket invariants are invalid")
        object.__setattr__(self, "members", members)
        object.__setattr__(self, "cost_adjusted_active_return_pct", MappingProxyType(costs))


@dataclass(frozen=True)
class WeeklyRunBaskets:
    protocol_id: str
    protocol_hash: str
    protocol_epoch: int
    run_id: str
    eligibility: str
    decision_target_session: str
    baskets: tuple[StageBasket, ...]

    def __post_init__(self) -> None:
        baskets = tuple(self.baskets)
        if (self.protocol_id, self.protocol_hash, self.protocol_epoch) != (
            PROTOCOL_ID, PROTOCOL_HASH, PROTOCOL_EPOCH,
        ) or self.eligibility != OFFICIAL_ELIGIBLE:
            raise WeeklyBasketError("weekly run basket identity is invalid")
        expected_order = tuple((horizon, cohort) for horizon in HORIZONS for cohort in _COHORTS)
        actual_order = tuple((item.horizon_sessions, item.cohort) for item in baskets)
        if actual_order != expected_order:
            raise WeeklyBasketError("weekly run basket set is incomplete or unordered")
        if any(
            (item.run_id, item.eligibility, item.decision_target_session) != (
                self.run_id, self.eligibility, self.decision_target_session,
            )
            for item in baskets
        ):
            raise WeeklyBasketError("stage basket and weekly run lineage differ")
        object.__setattr__(self, "baskets", baskets)

    def basket(self, horizon_sessions: int, cohort: str) -> StageBasket:
        try:
            return next(
                item for item in self.baskets
                if item.horizon_sessions == horizon_sessions and item.cohort == cohort
            )
        except StopIteration as exc:
            raise WeeklyBasketError("requested basket is outside the weekly set") from exc


def _members(run: RunObservation, cohort: str) -> tuple[str, ...]:
    return {
        "SCANNER": run.scanner_tickers,
        "LUNA": run.luna_tickers,
        "SOL": run.sol_tickers,
    }[cohort]


def _expected_keys(run: RunObservation) -> set[str]:
    return {
        outcome_identity(run.run_id, ticker, cohort, horizon)
        for horizon in HORIZONS
        for cohort in _COHORTS
        for ticker in _members(run, cohort)
    }


def _validate_expectation_lineage(
    run: RunObservation, expectation: OutcomeExpectation,
) -> None:
    if (
        expectation.run_id,
        expectation.eligibility,
        expectation.decision_timestamp_utc,
        expectation.decision_target_session,
        expectation.analytical_commit_sha,
        expectation.decision_snapshot_id,
        expectation.decision_snapshot_sha256,
        expectation.source_run_manifest_sha256,
        expectation.universe_version,
        expectation.universe_sha256,
    ) != (
        run.run_id,
        run.eligibility,
        run.run_timestamp_utc,
        run.decision_target_session,
        run.analytical_commit_sha,
        run.decision_snapshot_id,
        run.decision_snapshot_sha256,
        run.source_run_manifest_sha256,
        run.universe_version,
        run.universe_sha256,
    ):
        raise WeeklyBasketError("run and outcome expectation lineage mismatch")
    if expectation.ticker not in _members(run, expectation.cohort):
        raise WeeklyBasketError("outcome expectation cohort membership mismatch")


def _stage_basket(
    run: RunObservation,
    *,
    cohort: str,
    horizon: int,
    expectations: Mapping[str, OutcomeExpectation],
    finals: Mapping[str, OutcomeFinal],
) -> StageBasket:
    members = _members(run, cohort)
    completed: list[float] = []
    pending = 0
    dqf = 0
    for ticker in members:
        key = outcome_identity(run.run_id, ticker, cohort, horizon)
        if key not in expectations:
            raise WeeklyBasketError("stage basket expectation is missing")
        final = finals.get(key)
        if final is None:
            pending += 1
        elif final.status == COMPLETE:
            if final.active_return_pct is None:
                raise WeeklyBasketError("COMPLETE outcome lacks authoritative active return")
            completed.append(_finite(final.active_return_pct, "member active_return_pct"))
        elif final.status == DATA_QUALITY_FAILURE:
            dqf += 1
        else:
            raise WeeklyBasketError("OUTCOME_FINAL has a non-terminal status")
    if not members:
        status = EMPTY_BENCHMARK
        gross: float | None = 0.0
    elif dqf:
        status, gross = UNAVAILABLE_DQF, None
    elif pending:
        status, gross = PENDING, None
    else:
        status, gross = AVAILABLE, fmean(completed)
    if status in {AVAILABLE, EMPTY_BENCHMARK}:
        costs = {
            bps: 0.0 if status == EMPTY_BENCHMARK else gross - (2.0 * bps / 100.0)  # type: ignore[operator]
            for bps in _COST_BPS
        }
    else:
        costs = {bps: None for bps in _COST_BPS}
    return StageBasket(
        protocol_id=PROTOCOL_ID, protocol_hash=PROTOCOL_HASH, protocol_epoch=PROTOCOL_EPOCH,
        run_id=run.run_id, eligibility=run.eligibility,
        decision_target_session=run.decision_target_session,
        cohort=cohort, horizon_sessions=horizon, members=members,
        member_count=len(members), complete_count=len(completed),
        pending_count=pending, dqf_count=dqf, status=status,
        gross_active_return_pct=gross, cost_adjusted_active_return_pct=costs,
    )


def build_official_weekly_baskets(store: ValidationStore) -> tuple[WeeklyRunBaskets, ...]:
    """Derive official baskets without persisting or consulting mutable artifacts."""

    runs = tuple(
        run for run in load_run_observations(store)
        if run.eligibility == OFFICIAL_ELIGIBLE
    )
    weeks: dict[tuple[int, int], str] = {}
    for run in runs:
        iso = date.fromisoformat(run.decision_target_session).isocalendar()
        week = (iso.year, iso.week)
        if week in weeks:
            raise WeeklyBasketError(
                f"multiple official RUN_OBSERVATION records in ISO week {iso.year}-W{iso.week:02d}"
            )
        weeks[week] = run.run_id

    all_expectations = load_current_expectations(store)
    official_expectations = tuple(
        item for item in all_expectations if item.eligibility == OFFICIAL_ELIGIBLE
    )
    official_run_ids = {run.run_id for run in runs}
    if any(item.run_id not in official_run_ids for item in official_expectations):
        raise WeeklyBasketError("official expectation has no authoritative RUN_OBSERVATION")
    finals = load_outcome_finals(store, all_expectations)
    final_by_key = {item.expectation_key: item for item in finals}
    result: list[WeeklyRunBaskets] = []
    for run in runs:
        run_expectations = tuple(
            item for item in official_expectations if item.run_id == run.run_id
        )
        expected_keys = _expected_keys(run)
        actual_keys = {item.key for item in run_expectations}
        if actual_keys != expected_keys or len(actual_keys) != len(run_expectations):
            raise WeeklyBasketError("run expectation set is not the exact frozen membership set")
        expectation_by_key = {item.key: item for item in run_expectations}
        for expectation in run_expectations:
            _validate_expectation_lineage(run, expectation)
            if expectation.selection_data_quality_flags:
                raise WeeklyBasketError(
                    "official outcome expectation contains selection data quality flags"
                )
        baskets = tuple(
            _stage_basket(
                run, cohort=cohort, horizon=horizon,
                expectations=expectation_by_key, finals=final_by_key,
            )
            for horizon in HORIZONS
            for cohort in _COHORTS
        )
        result.append(WeeklyRunBaskets(
            protocol_id=PROTOCOL_ID, protocol_hash=PROTOCOL_HASH,
            protocol_epoch=PROTOCOL_EPOCH, run_id=run.run_id,
            eligibility=run.eligibility, decision_target_session=run.decision_target_session,
            baskets=baskets,
        ))
    return tuple(sorted(result, key=lambda item: (item.decision_target_session, item.run_id)))
