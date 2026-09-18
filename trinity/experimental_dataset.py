"""Build flat, auditable TRINITY/H2 experimental datasets from loaded data.

This module only orchestrates the frozen pairing, H2, and outcome engines.  It
does not fetch data, inspect future outcomes when selecting pairs, or apply any
statistical classification or trading rule.
"""

from __future__ import annotations

from collections import Counter
from typing import Mapping, Sequence, TypeAlias

from .earnings_pairing import pair_earnings_release
from .h2_novelty import H2InputError, calculate_h2_novelty
from .outcomes import EventTiming, OutcomeInputError, calculate_event_outcomes


ReleaseRecord: TypeAlias = Mapping[str, object]
TradingBar: TypeAlias = Mapping[str, object]
DatasetRow: TypeAlias = dict[str, object]

_HORIZONS = (1, 5, 20, 60)
_OUTCOME_METRICS = (
    "raw_return_pct",
    "absolute_return_pct",
    "benchmark_return_pct",
    "excess_return_pct",
    "max_favorable_excursion_pct",
    "max_adverse_excursion_pct",
    "realized_volatility",
)


class ExperimentalDatasetInputError(ValueError):
    """Raised when dataset-level inputs cannot be represented deterministically."""


def build_experimental_dataset(
    releases: Sequence[ReleaseRecord],
    ohlcv_by_ticker: Mapping[str, Sequence[TradingBar]],
    benchmark_ohlcv: Sequence[TradingBar] | None = None,
    timing_by_release_id: Mapping[str, EventTiming | str] | None = None,
) -> list[DatasetRow]:
    """Build one flat experimental row for every supplied earnings release.

    Releases are processed in deterministic ``(ticker, release_id)`` order.
    Duplicate release IDs are rejected because they would make timing lookup,
    previous-text resolution, and one-row-per-release provenance ambiguous.

    A MATCHED pair is retained whether exact or fallback.  NO_MATCH and
    AMBIGUOUS pairs remain in the output as excluded audit rows with null H2
    fields.  H2 validation failures and missing/invalid OHLCV exclude only the
    affected row, not the complete dataset.  PENDING outcome horizons do not
    exclude a row.
    """

    release_list = list(releases)
    _validate_unique_release_ids(release_list)
    ordered_releases = sorted(release_list, key=_release_sort_key)
    releases_by_id = {
        _required_release_text(release, "release_id"): release
        for release in ordered_releases
    }
    bars_by_ticker = _normalize_ohlcv_mapping(ohlcv_by_ticker)
    timing_lookup = timing_by_release_id or {}

    rows: list[DatasetRow] = []
    for current in ordered_releases:
        pairing = pair_earnings_release(current, ordered_releases)
        current_id = str(pairing["current_release_id"])
        ticker = str(pairing["ticker"])
        event_timing = _timing_text(timing_lookup.get(current_id, "UNKNOWN"))
        exclusion_reasons: list[str] = []

        row: DatasetRow = {
            **pairing,
            "pairing_quality": _pairing_quality(pairing),
            "h2_similarity": None,
            "h2_novelty": None,
            "h2_method": None,
            "current_word_count": None,
            "previous_word_count": None,
            "current_char_count": None,
            "previous_char_count": None,
            "event_timing": event_timing,
            "timing_ambiguous": None,
            "reference_resolution": None,
            **_empty_outcome_fields(),
        }

        if pairing["pairing_status"] == "MATCHED":
            previous_id = str(pairing["previous_release_id"])
            previous = releases_by_id[previous_id]
            try:
                h2 = calculate_h2_novelty(
                    current.get("text"),  # type: ignore[arg-type]
                    previous.get("text"),  # type: ignore[arg-type]
                )
            except H2InputError as error:
                exclusion_reasons.append(f"H2_INPUT_ERROR: {error}")
            else:
                row.update(
                    {
                        "h2_similarity": h2["similarity"],
                        "h2_novelty": h2["h2_novelty"],
                        "h2_method": h2["method"],
                        "current_word_count": h2["current_word_count"],
                        "previous_word_count": h2["previous_word_count"],
                        "current_char_count": h2["current_char_count"],
                        "previous_char_count": h2["previous_char_count"],
                    }
                )
        else:
            exclusion_reasons.append(f"PAIRING_{pairing['pairing_status']}")

        ticker_bars = bars_by_ticker.get(ticker)
        if ticker_bars is None:
            exclusion_reasons.append("MISSING_OHLCV")
        else:
            try:
                outcomes = calculate_event_outcomes(
                    ticker=ticker,
                    earnings_event_at=current.get("release_at"),  # type: ignore[arg-type]
                    timing=event_timing,
                    ohlcv=ticker_bars,
                    benchmark_ohlcv=benchmark_ohlcv,
                    horizons=_HORIZONS,
                )
            except OutcomeInputError as error:
                exclusion_reasons.append(f"OUTCOME_INPUT_ERROR: {error}")
            else:
                _apply_outcomes(row, outcomes)

        row["row_status"] = "EXCLUDED" if exclusion_reasons else "INCLUDED"
        row["exclusion_reason"] = (
            "; ".join(exclusion_reasons) if exclusion_reasons else None
        )
        rows.append(row)
    return rows


def _validate_unique_release_ids(releases: Sequence[ReleaseRecord]) -> None:
    release_ids = [
        _required_release_text(release, "release_id") for release in releases
    ]
    duplicates = sorted(
        release_id
        for release_id, count in Counter(release_ids).items()
        if count > 1
    )
    if duplicates:
        raise ExperimentalDatasetInputError(
            f"duplicate release_id: {', '.join(duplicates)}"
        )


def _release_sort_key(release: ReleaseRecord) -> tuple[str, str]:
    return (
        _required_release_text(release, "ticker").upper(),
        _required_release_text(release, "release_id"),
    )


def _required_release_text(release: ReleaseRecord, field: str) -> str:
    if not isinstance(release, Mapping):
        raise ExperimentalDatasetInputError("each release must be a mapping")
    value = release.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ExperimentalDatasetInputError(
            f"release {field} must be a non-empty string"
        )
    return value.strip()


def _normalize_ohlcv_mapping(
    ohlcv_by_ticker: Mapping[str, Sequence[TradingBar]],
) -> dict[str, Sequence[TradingBar]]:
    normalized: dict[str, Sequence[TradingBar]] = {}
    for ticker, bars in ohlcv_by_ticker.items():
        if not isinstance(ticker, str) or not ticker.strip():
            raise ExperimentalDatasetInputError(
                "ohlcv_by_ticker keys must be non-empty strings"
            )
        normalized_ticker = ticker.strip().upper()
        if normalized_ticker in normalized:
            raise ExperimentalDatasetInputError(
                f"duplicate normalized OHLCV ticker: {normalized_ticker}"
            )
        normalized[normalized_ticker] = bars
    return normalized


def _timing_text(value: EventTiming | str) -> str:
    return value.value if isinstance(value, EventTiming) else str(value)


def _pairing_quality(pairing: Mapping[str, object]) -> str:
    status = pairing["pairing_status"]
    if status == "AMBIGUOUS":
        return "AMBIGUOUS"
    if status == "NO_MATCH":
        return "NO_MATCH"
    if pairing["pairing_method"] == "EXACT_PREVIOUS_FISCAL_PERIOD":
        return "EXACT"
    return "FALLBACK"


def _empty_outcome_fields() -> DatasetRow:
    fields: DatasetRow = {}
    for horizon in _HORIZONS:
        fields[f"outcome_status_{horizon}"] = None
        for metric in _OUTCOME_METRICS:
            fields[f"{metric}_{horizon}"] = None
    return fields


def _apply_outcomes(
    row: DatasetRow,
    outcomes: Sequence[Mapping[str, object]],
) -> None:
    by_horizon = {
        int(outcome["horizon_sessions"]): outcome for outcome in outcomes
    }
    for horizon in _HORIZONS:
        outcome = by_horizon[horizon]
        row[f"outcome_status_{horizon}"] = outcome["outcome_status"]
        for metric in _OUTCOME_METRICS:
            row[f"{metric}_{horizon}"] = outcome[metric]

    first = by_horizon[_HORIZONS[0]]
    row["event_timing"] = first["timing"]
    row["timing_ambiguous"] = first["timing_ambiguous"]
    row["reference_resolution"] = first["reference_resolution"]
