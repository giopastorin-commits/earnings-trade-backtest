"""Deterministic outcome measurement for earnings events.

The module deliberately contains no data acquisition or trading logic.  It
operates only on the daily bars supplied by its caller and returns flat
dictionaries that can be serialized directly to JSON or written as CSV rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
import math
from statistics import pstdev
from typing import Mapping, Sequence, TypeAlias


DEFAULT_HORIZONS: tuple[int, ...] = (1, 5, 20, 60)
TradingBar: TypeAlias = Mapping[str, object]
OutcomeRecord: TypeAlias = dict[str, object]


class EventTiming(str, Enum):
    """Supported timing classifications for an earnings event."""

    PRE_MARKET = "PRE_MARKET"
    AFTER_MARKET = "AFTER_MARKET"
    INTRADAY = "INTRADAY"
    UNKNOWN = "UNKNOWN"


class OutcomeInputError(ValueError):
    """Raised when outcome inputs are malformed or internally inconsistent."""


class InsufficientDataError(OutcomeInputError):
    """Raised when supplied sessions cannot establish a valid reference."""


@dataclass(frozen=True)
class _Bar:
    session_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float


def calculate_event_outcomes(
    ticker: str,
    earnings_event_at: date | datetime | str,
    timing: EventTiming | str,
    ohlcv: Sequence[TradingBar],
    benchmark_ohlcv: Sequence[TradingBar] | None = None,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
) -> list[OutcomeRecord]:
    """Calculate forward outcomes from an earnings event.

    ``ohlcv`` rows must be strictly ordered by ascending ``date`` and contain
    ``open``, ``high``, ``low``, ``close`` and ``volume``.  Dates may be
    ``date``/``datetime`` objects or ISO-8601 strings.  The optional benchmark
    needs only ``date`` and ``close`` and must also be strictly ordered.

    PRE_MARKET uses the session immediately before the first session on or
    after the event date as its reference.  AFTER_MARKET uses the last session
    on or before the event date.  This makes weekends and holidays deterministic
    without inventing sessions, and prevents a post-market event from using a
    future close.  ``reference_resolution`` records the exact rule used.
    INTRADAY and UNKNOWN produce one null-valued PENDING row per horizon with
    ``timing_ambiguous=True`` because daily data cannot define a safe reference.

    Every requested horizon produces a row.  A horizon for which the future
    session is not present is PENDING; available horizons are COMPLETE.  Aware
    datetimes are never converted to another timezone: their received civil
    date is used as-is and recorded in ``event_date_received``.

    Excursions use each intervening session's high/low relative to the reference
    close.  Adverse excursion is signed (zero or negative).  Realized volatility
    is the annualized population standard deviation of daily log close returns,
    expressed as a percentage; it is zero when an horizon contains one return.

    Raises:
        OutcomeInputError: If values, ordering, or requested horizons are invalid.
        InsufficientDataError: If a valid reference session is unavailable.
    """

    normalized_ticker = ticker.strip() if isinstance(ticker, str) else ""
    if not normalized_ticker:
        raise OutcomeInputError("ticker must be a non-empty string")

    event_date, event_at_text = _parse_event_at(earnings_event_at)
    event_timing = _parse_timing(timing)
    normalized_horizons = _validate_horizons(horizons)
    bars = _normalize_bars(ohlcv, series_name="ohlcv")

    if event_timing in (EventTiming.INTRADAY, EventTiming.UNKNOWN):
        return [
            _ambiguous_record(
                normalized_ticker,
                event_at_text,
                event_date,
                event_timing,
                horizon,
            )
            for horizon in normalized_horizons
        ]

    reference_index, reference_resolution = _resolve_reference(
        bars, event_date, event_timing
    )
    reference = bars[reference_index]
    if reference.volume == 0:
        raise OutcomeInputError(
            f"reference session {reference.session_date.isoformat()} has zero volume; "
            "volume_change_pct is undefined"
        )

    benchmark_closes = (
        _normalize_benchmark(benchmark_ohlcv) if benchmark_ohlcv is not None else None
    )

    records: list[OutcomeRecord] = []
    for horizon in normalized_horizons:
        future_index = reference_index + horizon
        if future_index >= len(bars):
            records.append(
                _pending_record(
                    normalized_ticker,
                    event_at_text,
                    event_date,
                    event_timing,
                    horizon,
                    reference,
                    reference_resolution,
                )
            )
            continue

        future = bars[future_index]
        path = bars[reference_index + 1 : future_index + 1]
        raw_return = _percent_change(reference.close, future.close)

        benchmark_return: float | None = None
        excess_return: float | None = None
        if benchmark_closes is not None:
            benchmark_reference = _benchmark_close_on(
                benchmark_closes, reference.session_date
            )
            benchmark_future = _benchmark_close_on(
                benchmark_closes, future.session_date
            )
            if benchmark_reference is not None and benchmark_future is not None:
                benchmark_return = _percent_change(benchmark_reference, benchmark_future)
                excess_return = raw_return - benchmark_return

        records.append(
            {
                "ticker": normalized_ticker,
                "event_at": event_at_text,
                "event_date_received": event_date.isoformat(),
                "timing": event_timing.value,
                "timing_ambiguous": False,
                "reference_resolution": reference_resolution,
                "outcome_status": "COMPLETE",
                "horizon_sessions": horizon,
                "reference_session_date": reference.session_date.isoformat(),
                "future_session_date": future.session_date.isoformat(),
                "reference_price": reference.close,
                "future_close": future.close,
                "raw_return_pct": raw_return,
                "benchmark_return_pct": benchmark_return,
                "excess_return_pct": excess_return,
                "absolute_return_pct": abs(raw_return),
                "max_favorable_excursion_pct": max(
                    0.0, *(_percent_change(reference.close, bar.high) for bar in path)
                ),
                "max_adverse_excursion_pct": min(
                    0.0, *(_percent_change(reference.close, bar.low) for bar in path)
                ),
                "volume_change_pct": _percent_change(reference.volume, future.volume),
                "realized_volatility": _realized_volatility(reference, path),
            }
        )
    return records


def _ambiguous_record(
    ticker: str,
    event_at: str,
    event_date: date,
    timing: EventTiming,
    horizon: int,
) -> OutcomeRecord:
    return {
        "ticker": ticker,
        "event_at": event_at,
        "event_date_received": event_date.isoformat(),
        "timing": timing.value,
        "timing_ambiguous": True,
        "reference_resolution": "AMBIGUOUS",
        "outcome_status": "PENDING",
        "horizon_sessions": horizon,
        "reference_session_date": None,
        "future_session_date": None,
        "reference_price": None,
        "future_close": None,
        "raw_return_pct": None,
        "benchmark_return_pct": None,
        "excess_return_pct": None,
        "absolute_return_pct": None,
        "max_favorable_excursion_pct": None,
        "max_adverse_excursion_pct": None,
        "volume_change_pct": None,
        "realized_volatility": None,
    }


def _pending_record(
    ticker: str,
    event_at: str,
    event_date: date,
    timing: EventTiming,
    horizon: int,
    reference: _Bar,
    reference_resolution: str,
) -> OutcomeRecord:
    """Build a horizon row whose future session has not arrived yet."""

    return {
        "ticker": ticker,
        "event_at": event_at,
        "event_date_received": event_date.isoformat(),
        "timing": timing.value,
        "timing_ambiguous": False,
        "reference_resolution": reference_resolution,
        "outcome_status": "PENDING",
        "horizon_sessions": horizon,
        "reference_session_date": reference.session_date.isoformat(),
        "future_session_date": None,
        "reference_price": reference.close,
        "future_close": None,
        "raw_return_pct": None,
        "benchmark_return_pct": None,
        "excess_return_pct": None,
        "absolute_return_pct": None,
        "max_favorable_excursion_pct": None,
        "max_adverse_excursion_pct": None,
        "volume_change_pct": None,
        "realized_volatility": None,
    }


def _parse_event_at(value: date | datetime | str) -> tuple[date, str]:
    if isinstance(value, datetime):
        return value.date(), value.isoformat()
    if isinstance(value, date):
        return value, value.isoformat()
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.date(), value
        except ValueError:
            try:
                parsed_date = date.fromisoformat(value)
                return parsed_date, parsed_date.isoformat()
            except ValueError as error:
                raise OutcomeInputError(
                    "earnings_event_at must be an ISO-8601 date or datetime"
                ) from error
    raise OutcomeInputError("earnings_event_at must be a date, datetime, or ISO string")


def _parse_timing(value: EventTiming | str) -> EventTiming:
    if isinstance(value, EventTiming):
        return value
    try:
        return EventTiming(value)
    except (TypeError, ValueError) as error:
        allowed = ", ".join(item.value for item in EventTiming)
        raise OutcomeInputError(f"timing must be one of: {allowed}") from error


def _validate_horizons(horizons: Sequence[int]) -> tuple[int, ...]:
    if not horizons:
        raise OutcomeInputError("at least one horizon is required")
    if any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in horizons):
        raise OutcomeInputError("horizons must contain only positive integers")
    if len(set(horizons)) != len(horizons):
        raise OutcomeInputError("horizons must not contain duplicates")
    return tuple(horizons)


def _normalize_bars(rows: Sequence[TradingBar], series_name: str) -> list[_Bar]:
    if not rows:
        raise InsufficientDataError(f"{series_name} must contain at least one session")
    bars: list[_Bar] = []
    for index, row in enumerate(rows):
        try:
            session_date = _parse_session_date(row["date"])
            open_price = _positive_number(row["open"], "open", index, series_name)
            high = _positive_number(row["high"], "high", index, series_name)
            low = _positive_number(row["low"], "low", index, series_name)
            close = _positive_number(row["close"], "close", index, series_name)
            volume = _nonnegative_number(row["volume"], "volume", index, series_name)
        except KeyError as error:
            raise OutcomeInputError(
                f"{series_name} row {index} is missing field {error.args[0]!r}"
            ) from error
        if high < max(open_price, low, close) or low > min(open_price, high, close):
            raise OutcomeInputError(f"{series_name} row {index} has inconsistent OHLC values")
        bars.append(_Bar(session_date, open_price, high, low, close, volume))
    _validate_order([bar.session_date for bar in bars], series_name)
    return bars


def _normalize_benchmark(rows: Sequence[TradingBar]) -> dict[date, float]:
    if not rows:
        return {}
    dates: list[date] = []
    closes: dict[date, float] = {}
    for index, row in enumerate(rows):
        try:
            session_date = _parse_session_date(row["date"])
            close = _positive_number(row["close"], "close", index, "benchmark_ohlcv")
        except KeyError as error:
            raise OutcomeInputError(
                f"benchmark_ohlcv row {index} is missing field {error.args[0]!r}"
            ) from error
        dates.append(session_date)
        closes[session_date] = close
    _validate_order(dates, "benchmark_ohlcv")
    return closes


def _parse_session_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise OutcomeInputError(f"invalid session date: {value!r}") from error
    raise OutcomeInputError(f"session date must be a date or ISO date string, got {value!r}")


def _positive_number(value: object, field: str, index: int, series_name: str) -> float:
    number = _finite_number(value, field, index, series_name)
    if number <= 0:
        raise OutcomeInputError(f"{series_name} row {index} field {field!r} must be positive")
    return number


def _nonnegative_number(value: object, field: str, index: int, series_name: str) -> float:
    number = _finite_number(value, field, index, series_name)
    if number < 0:
        raise OutcomeInputError(f"{series_name} row {index} field {field!r} cannot be negative")
    return number


def _finite_number(value: object, field: str, index: int, series_name: str) -> float:
    if isinstance(value, bool):
        raise OutcomeInputError(f"{series_name} row {index} field {field!r} must be numeric")
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise OutcomeInputError(
            f"{series_name} row {index} field {field!r} must be numeric"
        ) from error
    if not math.isfinite(number):
        raise OutcomeInputError(f"{series_name} row {index} field {field!r} must be finite")
    return number


def _validate_order(dates: Sequence[date], series_name: str) -> None:
    if any(current <= previous for previous, current in zip(dates, dates[1:])):
        raise OutcomeInputError(
            f"{series_name} sessions must be unique and strictly ordered by date"
        )


def _resolve_reference(
    bars: Sequence[_Bar], event_date: date, timing: EventTiming
) -> tuple[int, str]:
    """Return the reference index and an auditable resolution label."""

    if timing is EventTiming.AFTER_MARKET:
        candidates = [index for index, bar in enumerate(bars) if bar.session_date <= event_date]
        if not candidates:
            raise InsufficientDataError("no session exists on or before the AFTER_MARKET event")
        reference_index = candidates[-1]
        resolution = (
            "EVENT_SESSION_CLOSE"
            if bars[reference_index].session_date == event_date
            else "LAST_SESSION_BEFORE_NON_TRADING_EVENT"
        )
        return reference_index, resolution

    event_session = next(
        (index for index, bar in enumerate(bars) if bar.session_date >= event_date),
        None,
    )
    if event_session is None:
        raise InsufficientDataError("no session exists on or after the PRE_MARKET event")
    if event_session == 0:
        raise InsufficientDataError(
            "PRE_MARKET outcome requires a previous session for the reference close"
        )
    resolution = (
        "PREVIOUS_SESSION_CLOSE"
        if bars[event_session].session_date == event_date
        else "PREVIOUS_SESSION_BEFORE_NEXT_TRADING_SESSION"
    )
    return event_session - 1, resolution


def _benchmark_close_on(closes: Mapping[date, float], target: date) -> float | None:
    """Return an exact-date benchmark close, if the supplied series has it."""

    return closes.get(target)


def _percent_change(start: float, end: float) -> float:
    return (end / start - 1.0) * 100.0


def _realized_volatility(reference: _Bar, path: Sequence[_Bar]) -> float:
    closes = [reference.close, *(bar.close for bar in path)]
    log_returns = [math.log(current / previous) for previous, current in zip(closes, closes[1:])]
    return pstdev(log_returns) * math.sqrt(252.0) * 100.0
