"""Final prospective V1 scorecard derived from authoritative weekly baskets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import math
import random
from statistics import fmean

from .baskets import (
    AVAILABLE,
    EMPTY_BENCHMARK,
    PENDING,
    UNAVAILABLE_DQF,
    StageBasket,
    WeeklyRunBaskets,
    build_official_weekly_baskets,
)
from .contracts import OFFICIAL_ELIGIBLE, ValidationContractError
from .outcome_expectations import outcome_identity
from .outcome_finalization import COMPLETE, load_outcome_finals
from .protocol_v1 import PROTOCOL, PROTOCOL_EPOCH, PROTOCOL_HASH, PROTOCOL_ID
from .run_observation import load_run_observations
from .slot_assignment import load_current_expectations
from .storage import ValidationStore


NO_OFFICIAL_WEEKS = "NO_OFFICIAL_WEEKS"
INSUFFICIENT_PRIMARY_USABLE_WEEKS = "INSUFFICIENT_PRIMARY_USABLE_WEEKS"
FORMAL_INFERENCE_AVAILABLE = "FORMAL_INFERENCE_AVAILABLE"

PRIMARY_USABLE = "PRIMARY_USABLE"
PRIMARY_PENDING = "PRIMARY_PENDING"
PRIMARY_DQF_UNAVAILABLE = "PRIMARY_DQF_UNAVAILABLE"

_USABLE_STAGE_STATES = frozenset({AVAILABLE, EMPTY_BENCHMARK})
_POLICY = PROTOCOL["weekly_analytics_policy"]
_MINIMUM_WEEKS = int(_POLICY["formal_inference_minimum_weeks"])
_BOOTSTRAP_REPLICATES = int(_POLICY["formal_inference_replicates"])
_BLOCK_LENGTH = int(_POLICY["primary_block_length_weeks"])
_CONFIDENCE = float(_POLICY["confidence_interval"])
_COST_BPS = tuple(int(value) for value in _POLICY["trading_cost_scenarios_bps_per_side"])
_BOOTSTRAP_LABEL = "FINAL_SCORECARD_PRIMARY_AR20"

if (
    _MINIMUM_WEEKS, _BOOTSTRAP_REPLICATES, _BLOCK_LENGTH, _CONFIDENCE, _COST_BPS
) != (52, 10000, 4, 0.95, (0, 10, 25)):
    raise RuntimeError("TRINITY V1 final-scorecard policy drift")


class FinalScorecardError(ValidationContractError):
    """Authoritative weekly records cannot form the frozen V1 scorecard."""


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FinalScorecardError(f"{field} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise FinalScorecardError(f"{field} must be finite")
    return number


@dataclass(frozen=True)
class EndpointSummary:
    endpoint: str
    horizon_sessions: int
    n_total_official_weeks: int
    n_usable: int
    n_pending: int
    n_dqf: int
    mean_delta_ar20_pct: float | None

    def __post_init__(self) -> None:
        counts = (
            self.n_total_official_weeks, self.n_usable, self.n_pending, self.n_dqf,
        )
        if self.horizon_sessions != 20 or any(
            type(value) is not int or value < 0 for value in counts
        ) or self.n_total_official_weeks != self.n_usable + self.n_pending + self.n_dqf:
            raise FinalScorecardError("endpoint denominator invariant is invalid")
        if self.n_usable == 0:
            if self.mean_delta_ar20_pct is not None:
                raise FinalScorecardError("endpoint without usable weeks cannot have a mean")
        elif self.mean_delta_ar20_pct is None:
            raise FinalScorecardError("usable endpoint lacks a mean")
        else:
            _finite(self.mean_delta_ar20_pct, "endpoint mean")


@dataclass(frozen=True)
class CostSensitivityResult:
    bps_per_side: int
    n_usable: int
    mean_delta_ar20_pct: float | None

    def __post_init__(self) -> None:
        if self.bps_per_side not in _COST_BPS or type(self.n_usable) is not int \
                or self.n_usable < 0:
            raise FinalScorecardError("cost sensitivity identity is invalid")
        if self.n_usable == 0:
            if self.mean_delta_ar20_pct is not None:
                raise FinalScorecardError("empty cost sensitivity cannot have a mean")
        elif self.mean_delta_ar20_pct is None:
            raise FinalScorecardError("cost sensitivity lacks a mean")
        else:
            _finite(self.mean_delta_ar20_pct, "cost sensitivity mean")


@dataclass(frozen=True)
class MissedWinnerDiagnostic:
    metric: str
    metric_class: str
    threshold_active_return_pct_20d: float
    scanner_winners: int
    luna_promoted: int
    sol_promoted: int
    missed_by_luna_count: int
    missed_by_sol_count: int
    missed_by_luna_rate: float | None
    missed_by_sol_rate: float | None

    def __post_init__(self) -> None:
        if self.metric != "missed_winner_rate_20d" or self.metric_class != "DIAGNOSTIC":
            raise FinalScorecardError("missed-winner diagnostic identity is invalid")
        _finite(self.threshold_active_return_pct_20d, "missed-winner threshold")
        counts = (
            self.scanner_winners, self.luna_promoted, self.sol_promoted,
            self.missed_by_luna_count, self.missed_by_sol_count,
        )
        if any(type(value) is not int or value < 0 for value in counts) \
                or self.luna_promoted + self.missed_by_luna_count != self.scanner_winners \
                or self.sol_promoted + self.missed_by_sol_count != self.scanner_winners:
            raise FinalScorecardError("missed-winner diagnostic counts are inconsistent")
        if self.scanner_winners == 0:
            if self.missed_by_luna_rate is not None or self.missed_by_sol_rate is not None:
                raise FinalScorecardError("empty missed-winner denominator must have null rates")
        else:
            for value in (self.missed_by_luna_rate, self.missed_by_sol_rate):
                rate = _finite(value, "missed-winner rate")
                if not 0.0 <= rate <= 1.0:
                    raise FinalScorecardError("missed-winner rate is outside [0, 1]")


@dataclass(frozen=True)
class ScorecardDiagnostics:
    missed_winner_20d: MissedWinnerDiagnostic


@dataclass(frozen=True)
class FinalScorecard:
    protocol_id: str
    protocol_hash: str
    protocol_epoch: int
    status: str
    n_total_official_weeks: int
    n_primary_usable_weeks: int
    n_primary_pending_weeks: int
    n_primary_dqf_unavailable_weeks: int
    primary_dqf_rate: float | None
    primary_pending_rate: float | None
    primary_mean_delta_ar20_pct: float | None
    primary_confidence_interval_95: tuple[float, float] | None
    luna_minus_scanner: EndpointSummary
    sol_minus_luna: EndpointSummary
    cost_sensitivity: tuple[CostSensitivityResult, ...]
    diagnostics: ScorecardDiagnostics

    def __post_init__(self) -> None:
        if (self.protocol_id, self.protocol_hash, self.protocol_epoch) != (
            PROTOCOL_ID, PROTOCOL_HASH, PROTOCOL_EPOCH,
        ):
            raise FinalScorecardError("final scorecard protocol identity mismatch")
        counts = (
            self.n_total_official_weeks, self.n_primary_usable_weeks,
            self.n_primary_pending_weeks, self.n_primary_dqf_unavailable_weeks,
        )
        if any(type(value) is not int or value < 0 for value in counts) \
                or self.n_total_official_weeks != sum(counts[1:]):
            raise FinalScorecardError("primary denominator invariant is invalid")
        expected_status = NO_OFFICIAL_WEEKS if self.n_total_official_weeks == 0 else (
            FORMAL_INFERENCE_AVAILABLE
            if self.n_primary_usable_weeks >= _MINIMUM_WEEKS
            else INSUFFICIENT_PRIMARY_USABLE_WEEKS
        )
        if self.status != expected_status:
            raise FinalScorecardError("final scorecard status is inconsistent")
        if self.n_total_official_weeks == 0:
            if self.primary_dqf_rate is not None or self.primary_pending_rate is not None:
                raise FinalScorecardError("zero-week scorecard rates must be null")
        else:
            expected_rates = (
                self.n_primary_dqf_unavailable_weeks / self.n_total_official_weeks,
                self.n_primary_pending_weeks / self.n_total_official_weeks,
            )
            actual_rates = (
                _finite(self.primary_dqf_rate, "primary DQF rate"),
                _finite(self.primary_pending_rate, "primary pending rate"),
            )
            if actual_rates != expected_rates:
                raise FinalScorecardError("primary rates differ from fixed denominators")
        if self.n_primary_usable_weeks == 0:
            if self.primary_mean_delta_ar20_pct is not None:
                raise FinalScorecardError("primary mean requires usable weeks")
        elif self.primary_mean_delta_ar20_pct is None:
            raise FinalScorecardError("usable primary endpoint lacks a mean")
        else:
            _finite(self.primary_mean_delta_ar20_pct, "primary mean")
        if self.status == FORMAL_INFERENCE_AVAILABLE:
            if not isinstance(self.primary_confidence_interval_95, tuple) \
                    or len(self.primary_confidence_interval_95) != 2:
                raise FinalScorecardError("formal scorecard lacks a confidence interval")
            low, high = (
                _finite(value, "primary confidence interval")
                for value in self.primary_confidence_interval_95
            )
            if low > high:
                raise FinalScorecardError("primary confidence interval is reversed")
        elif self.primary_confidence_interval_95 is not None:
            raise FinalScorecardError("non-formal scorecard cannot expose a confidence interval")
        if self.luna_minus_scanner.n_total_official_weeks != self.n_total_official_weeks \
                or self.sol_minus_luna.n_total_official_weeks != self.n_total_official_weeks:
            raise FinalScorecardError("secondary denominator differs from total official weeks")
        if self.luna_minus_scanner.endpoint != "LUNA_MINUS_SCANNER_AR20" \
                or self.sol_minus_luna.endpoint != "SOL_MINUS_LUNA_AR20":
            raise FinalScorecardError("secondary endpoint identity is invalid")
        sensitivities = tuple(self.cost_sensitivity)
        if tuple(item.bps_per_side for item in sensitivities) != _COST_BPS \
                or any(item.n_usable != self.n_primary_usable_weeks for item in sensitivities):
            raise FinalScorecardError("cost sensitivity set is incomplete or inconsistent")
        object.__setattr__(self, "cost_sensitivity", sensitivities)
        zero_cost = sensitivities[0].mean_delta_ar20_pct
        if (zero_cost is None) != (self.primary_mean_delta_ar20_pct is None) or (
            zero_cost is not None
            and not math.isclose(
                zero_cost, self.primary_mean_delta_ar20_pct,
                rel_tol=0.0, abs_tol=1e-12,
            )
        ):
            raise FinalScorecardError("zero-cost sensitivity differs from gross primary")

    def sensitivity(self, bps_per_side: int) -> CostSensitivityResult:
        try:
            return next(
                item for item in self.cost_sensitivity
                if item.bps_per_side == bps_per_side
            )
        except StopIteration as exc:
            raise FinalScorecardError("unknown cost sensitivity") from exc


def bootstrap_seed() -> int:
    material = f"{PROTOCOL_HASH}:{_BOOTSTRAP_LABEL}".encode("utf-8")
    return int(hashlib.sha256(material).hexdigest()[:16], 16)


def _circular_block_bootstrap_ci(values: tuple[float, ...]) -> tuple[float, float]:
    if len(values) < _MINIMUM_WEEKS:
        raise FinalScorecardError("formal bootstrap requires 52 primary-usable weeks")
    rng = random.Random(bootstrap_seed())
    samples: list[float] = []
    n = len(values)
    for _ in range(_BOOTSTRAP_REPLICATES):
        draw: list[float] = []
        while len(draw) < n:
            start = rng.randrange(n)
            draw.extend(values[(start + offset) % n] for offset in range(_BLOCK_LENGTH))
        samples.append(fmean(draw[:n]))
    samples.sort()
    alpha = (1.0 - _CONFIDENCE) / 2.0
    return (
        samples[int(alpha * (_BOOTSTRAP_REPLICATES - 1))],
        samples[int((1.0 - alpha) * (_BOOTSTRAP_REPLICATES - 1))],
    )


def _week_state(first: StageBasket, second: StageBasket) -> str:
    states = {first.status, second.status}
    if UNAVAILABLE_DQF in states:
        return PRIMARY_DQF_UNAVAILABLE
    if PENDING in states:
        return PRIMARY_PENDING
    if states.issubset(_USABLE_STAGE_STATES):
        return PRIMARY_USABLE
    raise FinalScorecardError("weekly endpoint contains an unknown stage state")


def _value(stage: StageBasket, *, bps: int | None = None) -> float:
    value = stage.gross_active_return_pct if bps is None \
        else stage.cost_adjusted_active_return_pct[bps]
    if value is None:
        raise FinalScorecardError("usable weekly endpoint lacks a return")
    return _finite(value, "weekly stage return")


def _endpoint_summary(
    weekly: tuple[WeeklyRunBaskets, ...], *, first: str, second: str, name: str,
) -> EndpointSummary:
    deltas: list[float] = []
    pending = dqf = 0
    for week in weekly:
        first_basket = week.basket(20, first)
        second_basket = week.basket(20, second)
        state = _week_state(first_basket, second_basket)
        if state == PRIMARY_DQF_UNAVAILABLE:
            dqf += 1
        elif state == PRIMARY_PENDING:
            pending += 1
        else:
            deltas.append(_value(first_basket) - _value(second_basket))
    return EndpointSummary(
        endpoint=name, horizon_sessions=20,
        n_total_official_weeks=len(weekly), n_usable=len(deltas),
        n_pending=pending, n_dqf=dqf,
        mean_delta_ar20_pct=None if not deltas else fmean(deltas),
    )


def _missed_winner_diagnostic(store: ValidationStore) -> MissedWinnerDiagnostic:
    runs = {
        item.run_id: item for item in load_run_observations(store)
        if item.eligibility == OFFICIAL_ELIGIBLE
    }
    expectations = load_current_expectations(store)
    finals = {
        item.expectation_key: item for item in load_outcome_finals(store, expectations)
    }
    threshold = float(PROTOCOL["missed_winner_threshold_active_return_pct_20d"])
    winners: list[tuple[str, str]] = []
    for run in runs.values():
        for ticker in run.scanner_tickers:
            key = outcome_identity(run.run_id, ticker, "SCANNER", 20)
            final = finals.get(key)
            if final is not None and final.status == COMPLETE \
                    and final.active_return_pct is not None \
                    and final.active_return_pct >= threshold:
                winners.append((run.run_id, ticker))
    luna = sum(ticker in runs[run_id].luna_tickers for run_id, ticker in winners)
    sol = sum(ticker in runs[run_id].sol_tickers for run_id, ticker in winners)
    denominator = len(winners)
    return MissedWinnerDiagnostic(
        metric="missed_winner_rate_20d", metric_class="DIAGNOSTIC",
        threshold_active_return_pct_20d=threshold,
        scanner_winners=denominator, luna_promoted=luna, sol_promoted=sol,
        missed_by_luna_count=denominator - luna,
        missed_by_sol_count=denominator - sol,
        missed_by_luna_rate=None if denominator == 0 else (denominator - luna) / denominator,
        missed_by_sol_rate=None if denominator == 0 else (denominator - sol) / denominator,
    )


def build_final_scorecard(store: ValidationStore) -> FinalScorecard:
    """Build the read-only V1 scorecard from authoritative immutable records."""

    weekly = build_official_weekly_baskets(store)
    order = tuple((item.decision_target_session, item.run_id) for item in weekly)
    if order != tuple(sorted(order)) or len({item.run_id for item in weekly}) != len(weekly):
        raise FinalScorecardError("weekly basket ordering or run uniqueness is invalid")
    weeks = set()
    for item in weekly:
        iso = date.fromisoformat(item.decision_target_session).isocalendar()
        identity = (iso.year, iso.week)
        if identity in weeks:
            raise FinalScorecardError("multiple weekly units share an ISO week")
        weeks.add(identity)

    primary: list[float] = []
    primary_pending = primary_dqf = 0
    cost_deltas = {bps: [] for bps in _COST_BPS}
    for week in weekly:
        scanner = week.basket(20, "SCANNER")
        sol = week.basket(20, "SOL")
        state = _week_state(scanner, sol)
        if state == PRIMARY_DQF_UNAVAILABLE:
            primary_dqf += 1
        elif state == PRIMARY_PENDING:
            primary_pending += 1
        else:
            primary.append(_value(sol) - _value(scanner))
            for bps in _COST_BPS:
                cost_deltas[bps].append(_value(sol, bps=bps) - _value(scanner, bps=bps))

    total = len(weekly)
    usable = len(primary)
    status = NO_OFFICIAL_WEEKS if total == 0 else (
        FORMAL_INFERENCE_AVAILABLE if usable >= _MINIMUM_WEEKS
        else INSUFFICIENT_PRIMARY_USABLE_WEEKS
    )
    return FinalScorecard(
        protocol_id=PROTOCOL_ID, protocol_hash=PROTOCOL_HASH, protocol_epoch=PROTOCOL_EPOCH,
        status=status, n_total_official_weeks=total,
        n_primary_usable_weeks=usable, n_primary_pending_weeks=primary_pending,
        n_primary_dqf_unavailable_weeks=primary_dqf,
        primary_dqf_rate=None if total == 0 else primary_dqf / total,
        primary_pending_rate=None if total == 0 else primary_pending / total,
        primary_mean_delta_ar20_pct=None if not primary else fmean(primary),
        primary_confidence_interval_95=(
            _circular_block_bootstrap_ci(tuple(primary)) if usable >= _MINIMUM_WEEKS else None
        ),
        luna_minus_scanner=_endpoint_summary(
            weekly, first="LUNA", second="SCANNER", name="LUNA_MINUS_SCANNER_AR20",
        ),
        sol_minus_luna=_endpoint_summary(
            weekly, first="SOL", second="LUNA", name="SOL_MINUS_LUNA_AR20",
        ),
        cost_sensitivity=tuple(
            CostSensitivityResult(
                bps_per_side=bps, n_usable=usable,
                mean_delta_ar20_pct=None if not cost_deltas[bps] else fmean(cost_deltas[bps]),
            )
            for bps in _COST_BPS
        ),
        diagnostics=ScorecardDiagnostics(_missed_winner_diagnostic(store)),
    )
