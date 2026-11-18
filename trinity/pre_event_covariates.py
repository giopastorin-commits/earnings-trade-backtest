"""Pre-Event Covariates V0.1 for H2_PROTOCOL_V2 / V2.1 (sections 6 and 5-bis).

The module computes only quantities available at or before an event's
reference session. It never reads a bar after ``reference_session_date``:
the caller's series is truncated before any calculation, so later prices
cannot influence the result. It performs no data acquisition, no outcome
calculation and no statistical analysis.

Definitions (frozen by the protocol):

- ``sigma_pre_60``: population standard deviation of the 60 daily log close
  returns ending at the reference session (61 consecutive closes in the
  supplied series), annualized by sqrt(252) and expressed in percent. Same
  formula as ``realized_volatility`` in Outcome Engine V0.2, applied to the
  preceding window. Missing if fewer than 61 closes exist or any close in
  the window is not strictly positive and finite.
- ``dollar_volume_pre_60``: mean of close * volume over the 60 sessions
  ending at the reference session. Missing if any of those closes or volumes
  is invalid.
- ``log_word_ratio``: ln(current_word_count / previous_word_count).
- ``log_word_count``: ln(current_word_count).
- ``after_market``: 1 for AFTER_MARKET, 0 for PRE_MARKET, missing otherwise.

No value is imputed, clipped or otherwise transformed.
"""

from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
import math
from statistics import pstdev
from typing import Mapping, Sequence


COMPONENT = "PRE_EVENT_COVARIATES_V0_1"
WINDOW_RETURNS = 60
WINDOW_CLOSES = WINDOW_RETURNS + 1
ANNUALIZATION = 252.0


class PreEventCovariateInputError(ValueError):
    """Raised when inputs are structurally malformed (not merely missing)."""


def compute_pre_event_covariates(
    reference_session_date: date | datetime | str,
    ohlcv: Sequence[Mapping[str, object]],
    current_word_count: object,
    previous_word_count: object,
    event_timing: object,
) -> dict[str, object]:
    """Return the frozen pre-event covariates for one event as a flat dict.

    ``ohlcv`` rows need ``date``, ``close`` and ``volume`` and must be strictly
    ordered by ascending date. Rows after the reference session are discarded
    before any calculation.
    """

    reference = _parse_date(reference_session_date, "reference_session_date")
    dates = [_parse_date(_field(row, "date", index), f"ohlcv[{index}].date")
             for index, row in enumerate(ohlcv)]
    if any(current <= previous for previous, current in zip(dates, dates[1:])):
        raise PreEventCovariateInputError("ohlcv sessions must be unique and strictly ordered")

    record: dict[str, object] = {
        "covariate_component": COMPONENT,
        "reference_session_date": reference.isoformat(),
        "sigma_pre_60": None,
        "sigma_status": None,
        "dollar_volume_pre_60": None,
        "dollar_volume_status": None,
        "price_window_start": None,
        "price_window_sha256": None,
        "log_word_ratio": None,
        "log_word_count": None,
        "words_status": None,
        "after_market": None,
        "timing_status": None,
    }

    if reference not in dates:
        record["sigma_status"] = "REFERENCE_SESSION_NOT_FOUND"
        record["dollar_volume_status"] = "REFERENCE_SESSION_NOT_FOUND"
    else:
        cut = dates.index(reference) + 1
        truncated = list(ohlcv[:cut])  # nothing after the reference is ever read
        _fill_price_covariates(record, truncated, dates[:cut])

    _fill_word_covariates(record, current_word_count, previous_word_count)
    _fill_timing(record, event_timing)
    return record


def _fill_price_covariates(
    record: dict[str, object],
    rows: Sequence[Mapping[str, object]],
    dates: Sequence[date],
) -> None:
    if len(rows) < WINDOW_CLOSES:
        record["sigma_status"] = "INSUFFICIENT_HISTORY"
        record["dollar_volume_status"] = "INSUFFICIENT_HISTORY"
        return

    window = rows[-WINDOW_CLOSES:]
    window_dates = dates[-WINDOW_CLOSES:]
    closes = [_optional_number(row.get("close")) for row in window]
    volumes = [_optional_number(row.get("volume")) for row in window]

    record["price_window_start"] = window_dates[0].isoformat()
    record["price_window_sha256"] = _window_hash(window_dates, closes, volumes)

    if all(close is not None and close > 0 for close in closes):
        log_returns = [math.log(current / previous)  # type: ignore[operator]
                       for previous, current in zip(closes, closes[1:])]
        record["sigma_pre_60"] = pstdev(log_returns) * math.sqrt(ANNUALIZATION) * 100.0
        record["sigma_status"] = "COMPLETE"
    else:
        record["sigma_status"] = "INVALID_CLOSE"

    last_closes = closes[-WINDOW_RETURNS:]
    last_volumes = volumes[-WINDOW_RETURNS:]
    if all(close is not None and close > 0 for close in last_closes) and all(
        volume is not None and volume >= 0 for volume in last_volumes
    ):
        products = [close * volume for close, volume in zip(last_closes, last_volumes)]  # type: ignore[operator]
        record["dollar_volume_pre_60"] = sum(products) / WINDOW_RETURNS
        record["dollar_volume_status"] = "COMPLETE"
    else:
        record["dollar_volume_status"] = "INVALID_CLOSE_OR_VOLUME"


def _fill_word_covariates(record: dict[str, object], current: object, previous: object) -> None:
    current_count = _positive_int(current)
    previous_count = _positive_int(previous)
    if current_count is None or previous_count is None:
        record["words_status"] = "INVALID_WORD_COUNT"
        return
    record["log_word_ratio"] = math.log(current_count / previous_count)
    record["log_word_count"] = math.log(current_count)
    record["words_status"] = "COMPLETE"


def _fill_timing(record: dict[str, object], timing: object) -> None:
    value = getattr(timing, "value", timing)
    if value == "AFTER_MARKET":
        record["after_market"] = 1
        record["timing_status"] = "COMPLETE"
    elif value == "PRE_MARKET":
        record["after_market"] = 0
        record["timing_status"] = "COMPLETE"
    else:
        record["timing_status"] = "NOT_PRE_OR_AFTER_MARKET"


def _window_hash(
    dates: Sequence[date], closes: Sequence[float | None], volumes: Sequence[float | None]
) -> str:
    payload = [
        {"date": day.isoformat(), "close": close, "volume": volume}
        for day, close, volume in zip(dates, closes, volumes)
    ]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _field(row: Mapping[str, object], name: str, index: int) -> object:
    try:
        return row[name]
    except KeyError as error:
        raise PreEventCovariateInputError(f"ohlcv row {index} is missing {name!r}") from error


def _parse_date(value: object, name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise PreEventCovariateInputError(f"{name} is not an ISO date: {value!r}") from error
    raise PreEventCovariateInputError(f"{name} must be a date or ISO date string")


def _optional_number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None
