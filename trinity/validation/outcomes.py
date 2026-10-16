"""Point-in-time outcome calculations over caller-supplied immutable bars."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Mapping, Sequence

from .contracts import OutcomeObservation, ValidationContractError


@dataclass(frozen=True)
class BenchmarkSeries:
    """Immutable, separately supplied benchmark input; never an investible ticker."""

    symbol: str
    return_kind: str
    provider: str
    adjustment: str
    interval: str
    snapshot_id: str
    snapshot_sha256: str
    coverage_start_session: str
    coverage_end_session: str
    bars: tuple[Mapping[str, object], ...]

    def __post_init__(self) -> None:
        if self.symbol != "SPY" or self.return_kind != "PRICE_RETURN":
            raise ValidationContractError("V1 benchmark must be SPY price return")
        if self.provider != "TWELVE_DATA" or self.adjustment != "none" or self.interval != "1day":
            raise ValidationContractError("V1 benchmark source contract is invalid")
        if not self.bars:
            raise ValidationContractError("benchmark session coverage is empty")
        sessions = tuple(str(row.get("date") or "") for row in self.bars)
        if (
            sessions[0] != self.coverage_start_session
            or sessions[-1] != self.coverage_end_session
            or not self.coverage_start_session
        ):
            raise ValidationContractError("benchmark session coverage does not match bars")
        if len(self.snapshot_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.snapshot_sha256
        ):
            raise ValidationContractError("benchmark snapshot hash must be SHA-256")


def _number(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise ValidationContractError(f"{field} must be numeric")
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValidationContractError(f"{field} must be numeric") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValidationContractError(f"{field} must be positive and finite")
    return result


def _bars(rows: Sequence[Mapping[str, object]], name: str) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    previous: datetime | None = None
    for row in rows:
        try:
            tradable_at = datetime.fromisoformat(str(row["tradable_at"]).replace("Z", "+00:00"))
            session = str(row["date"])
            open_price = _number(row["open"], f"{name}.open")
            close = _number(row["close"], f"{name}.close")
        except KeyError as exc:
            raise ValidationContractError(f"{name} bar lacks {exc.args[0]}") from exc
        if tradable_at.tzinfo is None or (previous is not None and tradable_at <= previous):
            raise ValidationContractError(f"{name} tradable timestamps must be aware and increasing")
        previous = tradable_at
        normalized.append({"tradable_at": tradable_at, "date": session, "open": open_price, "close": close})
    return normalized


def calculate_selection_outcome(
    *, run_id: str, ticker: str, cohort: str, decision_timestamp_utc: str,
    horizon_sessions: int, stock_bars: Sequence[Mapping[str, object]],
    benchmark: BenchmarkSeries | None,
    source_snapshot_id: str, source_snapshot_sha256: str,
    eligibility: str,
) -> OutcomeObservation:
    """Calculate one horizon without acquiring data or using a pre-decision fill."""

    decision = datetime.fromisoformat(decision_timestamp_utc.replace("Z", "+00:00"))
    if decision.tzinfo is None:
        raise ValidationContractError("decision timestamp must be timezone-aware")
    stock = _bars(stock_bars, "stock")
    start = next((index for index, bar in enumerate(stock)
                  if bar["tradable_at"] > decision), None)
    if start is None:
        return OutcomeObservation(
            run_id=run_id, ticker=ticker, cohort=cohort, horizon_sessions=horizon_sessions,
            first_tradable_session=None, first_tradable_price=None, future_session=None,
            stock_return_pct=None, benchmark_return_pct=None, active_return_pct=None,
            maturity_status="PENDING", source_snapshot_id=source_snapshot_id,
            source_snapshot_sha256=source_snapshot_sha256, eligibility=eligibility,
            quality_flags=("FIRST_TRADABLE_PRICE_PENDING",),
        )
    first = stock[start]
    future_index = start + horizon_sessions - 1
    if future_index >= len(stock):
        return OutcomeObservation(
            run_id=run_id, ticker=ticker, cohort=cohort, horizon_sessions=horizon_sessions,
            first_tradable_session=str(first["date"]), first_tradable_price=float(first["open"]),
            future_session=None, stock_return_pct=None, benchmark_return_pct=None,
            active_return_pct=None, maturity_status="PENDING", source_snapshot_id=source_snapshot_id,
            source_snapshot_sha256=source_snapshot_sha256, eligibility=eligibility,
            quality_flags=("HORIZON_NOT_MATURE",),
        )
    future = stock[future_index]
    stock_return = (float(future["close"]) / float(first["open"]) - 1.0) * 100.0
    if benchmark is None:
        return OutcomeObservation(
            run_id=run_id, ticker=ticker, cohort=cohort, horizon_sessions=horizon_sessions,
            first_tradable_session=str(first["date"]), first_tradable_price=float(first["open"]),
            future_session=str(future["date"]), stock_return_pct=stock_return,
            benchmark_return_pct=None, active_return_pct=None,
            maturity_status="DATA_QUALITY_FAILURE", source_snapshot_id=source_snapshot_id,
            source_snapshot_sha256=source_snapshot_sha256, eligibility=eligibility,
            quality_flags=("BENCHMARK_SERIES_MISSING",),
        )
    benchmark_rows = _bars(benchmark.bars, "benchmark")
    benchmark_by_date = {str(row["date"]): row for row in benchmark_rows}
    benchmark_first = benchmark_by_date.get(str(first["date"]))
    benchmark_future = benchmark_by_date.get(str(future["date"]))
    if benchmark_first is None or benchmark_future is None:
        return OutcomeObservation(
            run_id=run_id, ticker=ticker, cohort=cohort, horizon_sessions=horizon_sessions,
            first_tradable_session=str(first["date"]), first_tradable_price=float(first["open"]),
            future_session=str(future["date"]), stock_return_pct=stock_return,
            benchmark_return_pct=None, active_return_pct=None,
            maturity_status="DATA_QUALITY_FAILURE", source_snapshot_id=source_snapshot_id,
            source_snapshot_sha256=source_snapshot_sha256, eligibility=eligibility,
            benchmark_snapshot_id=benchmark.snapshot_id,
            benchmark_snapshot_sha256=benchmark.snapshot_sha256,
            quality_flags=("BENCHMARK_SESSION_MISSING",),
        )
    benchmark_return = (
        float(benchmark_future["close"]) / float(benchmark_first["open"]) - 1.0
    ) * 100.0
    return OutcomeObservation(
        run_id=run_id, ticker=ticker, cohort=cohort, horizon_sessions=horizon_sessions,
        first_tradable_session=str(first["date"]), first_tradable_price=float(first["open"]),
        future_session=str(future["date"]), stock_return_pct=stock_return,
        benchmark_return_pct=benchmark_return, active_return_pct=stock_return - benchmark_return,
        maturity_status="COMPLETE", source_snapshot_id=source_snapshot_id,
        source_snapshot_sha256=source_snapshot_sha256, eligibility=eligibility,
        benchmark_snapshot_id=benchmark.snapshot_id,
        benchmark_snapshot_sha256=benchmark.snapshot_sha256,
    )


def net_return_after_round_trip_cost(gross_return_pct: float, bps_per_side: int) -> float:
    """Apply the frozen analytics-only two-sided cost assumption."""

    if bps_per_side < 0:
        raise ValidationContractError("trading cost cannot be negative")
    return float(gross_return_pct) - (2.0 * bps_per_side / 100.0)
