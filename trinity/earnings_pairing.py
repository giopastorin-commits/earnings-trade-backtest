"""Deterministic pairing of comparable earnings-release documents.

The engine uses only metadata contained in the supplied document records.  It
does not inspect document text, calculate H2, or use market and future outcome
data.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Mapping, Sequence, TypeAlias


ReleaseRecord: TypeAlias = Mapping[str, object]
PairingResult: TypeAlias = dict[str, object]

_PERIOD_TYPES = frozenset({"QUARTERLY", "ANNUAL", "UNKNOWN"})
_DOCUMENT_TYPES = frozenset(
    {"EARNINGS_RELEASE", "10-Q", "10-K", "TRANSCRIPT", "UNKNOWN"}
)


class EarningsPairingInputError(ValueError):
    """Raised when release metadata is invalid or internally inconsistent."""


@dataclass(frozen=True)
class _Release:
    ticker: str
    release_id: str
    release_at: datetime
    release_at_text: str
    timezone_aware: bool
    fiscal_year: int | None
    fiscal_quarter: int | None
    period_type: str
    document_type: str


def pair_earnings_release(
    current: ReleaseRecord,
    history: Sequence[ReleaseRecord],
) -> PairingResult:
    """Select the previous comparable release for ``current``.

    Candidates must have the same normalized ticker, document type, and period
    type and must be strictly earlier than the current release.  An immediately
    preceding fiscal period is preferred.  Otherwise, the chronologically
    closest compatible candidate is used as an explicit fallback.

    The order of ``history`` never affects the result.  A copy of the current
    release may be present in history and is excluded.  Duplicate history IDs
    are invalid.  If multiple best candidates share a timestamp, no arbitrary
    ID-based tie-break is applied and the result is AMBIGUOUS.

    Raises:
        EarningsPairingInputError: If current or historical metadata is invalid.
    """

    current_release = _normalize_release(current, "current")
    history_releases = [
        _normalize_release(record, f"history[{index}]")
        for index, record in enumerate(history)
    ]
    _validate_release_ids(current_release, history_releases)
    _validate_timezone_awareness(current_release, history_releases)

    candidates = sorted(
        (
            release
            for release in history_releases
            if release.release_id != current_release.release_id
            and release.ticker == current_release.ticker
            and release.release_at < current_release.release_at
            and release.document_type == current_release.document_type
            and release.period_type == current_release.period_type
        ),
        key=lambda release: (release.release_at, release.release_id),
    )
    candidate_count = len(candidates)
    if not candidates:
        return _unmatched_result(current_release, candidate_count)

    expected_period = _expected_previous_period(current_release)
    exact_candidates = (
        [candidate for candidate in candidates if _fiscal_period(candidate) == expected_period]
        if expected_period is not None
        else []
    )

    if exact_candidates:
        preferred = exact_candidates
        pairing_method = "EXACT_PREVIOUS_FISCAL_PERIOD"
        fiscal_continuity = "EXACT"
        fallback_used = False
    else:
        preferred = candidates
        pairing_method = "CHRONOLOGICAL_COMPATIBLE_FALLBACK"
        fallback_used = True
        fiscal_continuity = "UNKNOWN"

    latest_timestamp = max(candidate.release_at for candidate in preferred)
    finalists = [
        candidate for candidate in preferred if candidate.release_at == latest_timestamp
    ]
    if len(finalists) != 1:
        return _ambiguous_result(current_release, candidate_count)

    previous = finalists[0]
    if fallback_used and _has_complete_fiscal_period(current_release):
        if _has_complete_fiscal_period(previous):
            fiscal_continuity = "BROKEN"

    return {
        "ticker": current_release.ticker,
        "current_release_id": current_release.release_id,
        "previous_release_id": previous.release_id,
        "current_release_at": current_release.release_at_text,
        "previous_release_at": previous.release_at_text,
        "pairing_status": "MATCHED",
        "pairing_method": pairing_method,
        "fiscal_continuity": fiscal_continuity,
        "fallback_used": fallback_used,
        "candidate_count": candidate_count,
        "days_between_releases": _days_between(previous, current_release),
    }


def _normalize_release(record: ReleaseRecord, label: str) -> _Release:
    if not isinstance(record, Mapping):
        raise EarningsPairingInputError(f"{label} must be a mapping")

    ticker = _required_text(record.get("ticker"), "ticker", label).upper()
    release_id = _required_text(record.get("release_id"), "release_id", label)
    release_at, release_at_text, timezone_aware = _parse_release_at(
        record.get("release_at"), label
    )
    fiscal_year = _optional_integer(record.get("fiscal_year"), "fiscal_year", label)
    fiscal_quarter = _optional_quarter(record.get("fiscal_quarter"), label)
    period_type = _enum_value(
        record.get("period_type"), "period_type", _PERIOD_TYPES, label
    )
    document_type = _enum_value(
        record.get("document_type"), "document_type", _DOCUMENT_TYPES, label
    )
    return _Release(
        ticker=ticker,
        release_id=release_id,
        release_at=release_at,
        release_at_text=release_at_text,
        timezone_aware=timezone_aware,
        fiscal_year=fiscal_year,
        fiscal_quarter=fiscal_quarter,
        period_type=period_type,
        document_type=document_type,
    )


def _required_text(value: object, field: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EarningsPairingInputError(f"{label}.{field} must be a non-empty string")
    return value.strip()


def _parse_release_at(value: object, label: str) -> tuple[datetime, str, bool]:
    parsed: datetime
    serialized: str
    if isinstance(value, datetime):
        parsed = value
        serialized = value.isoformat()
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
        serialized = value.isoformat()
    elif isinstance(value, str) and value.strip():
        serialized = value
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise EarningsPairingInputError(
                f"{label}.release_at must be a valid ISO-8601 date or datetime"
            ) from error
    else:
        raise EarningsPairingInputError(
            f"{label}.release_at must be a date, datetime, or ISO-8601 string"
        )

    aware = parsed.utcoffset() is not None
    if aware:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed, serialized, aware


def _optional_integer(value: object, field: str, label: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise EarningsPairingInputError(f"{label}.{field} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise EarningsPairingInputError(f"{label}.{field} must be an integer")


def _optional_quarter(value: object, label: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, str) and value.strip().upper().startswith("Q"):
        value = value.strip()[1:]
    quarter = _optional_integer(value, "fiscal_quarter", label)
    if quarter not in (1, 2, 3, 4):
        raise EarningsPairingInputError(
            f"{label}.fiscal_quarter must be one of 1, 2, 3, or 4"
        )
    return quarter


def _enum_value(
    value: object,
    field: str,
    allowed: frozenset[str],
    label: str,
) -> str:
    if value is None or value == "":
        return "UNKNOWN"
    if not isinstance(value, str):
        raise EarningsPairingInputError(f"{label}.{field} must be a string")
    normalized = value.strip().upper()
    if normalized not in allowed:
        choices = ", ".join(sorted(allowed))
        raise EarningsPairingInputError(
            f"{label}.{field} must be one of: {choices}"
        )
    return normalized


def _validate_release_ids(current: _Release, history: Sequence[_Release]) -> None:
    counts = Counter(release.release_id for release in history)
    duplicates = sorted(release_id for release_id, count in counts.items() if count > 1)
    if duplicates:
        raise EarningsPairingInputError(
            f"duplicate release_id in history: {', '.join(duplicates)}"
        )

    current_copies = [
        release for release in history if release.release_id == current.release_id
    ]
    if current_copies and current_copies[0] != current:
        raise EarningsPairingInputError(
            "history contains current release_id with conflicting metadata"
        )


def _validate_timezone_awareness(
    current: _Release, history: Sequence[_Release]
) -> None:
    if any(release.timezone_aware != current.timezone_aware for release in history):
        raise EarningsPairingInputError(
            "release_at values must consistently be timezone-aware or timezone-naive"
        )


def _expected_previous_period(release: _Release) -> tuple[int, int | None] | None:
    if release.fiscal_year is None:
        return None
    if release.period_type == "ANNUAL":
        return release.fiscal_year - 1, None
    if release.period_type == "QUARTERLY" and release.fiscal_quarter is not None:
        if release.fiscal_quarter == 1:
            return release.fiscal_year - 1, 4
        return release.fiscal_year, release.fiscal_quarter - 1
    return None


def _fiscal_period(release: _Release) -> tuple[int, int | None] | None:
    if release.fiscal_year is None:
        return None
    if release.period_type == "ANNUAL":
        return release.fiscal_year, None
    if release.period_type == "QUARTERLY" and release.fiscal_quarter is not None:
        return release.fiscal_year, release.fiscal_quarter
    return None


def _has_complete_fiscal_period(release: _Release) -> bool:
    return _fiscal_period(release) is not None


def _days_between(previous: _Release, current: _Release) -> int | float:
    days = (current.release_at - previous.release_at).total_seconds() / 86_400.0
    return int(days) if days.is_integer() else days


def _unmatched_result(current: _Release, candidate_count: int) -> PairingResult:
    return {
        "ticker": current.ticker,
        "current_release_id": current.release_id,
        "previous_release_id": None,
        "current_release_at": current.release_at_text,
        "previous_release_at": None,
        "pairing_status": "NO_MATCH",
        "pairing_method": "NONE",
        "fiscal_continuity": "UNKNOWN",
        "fallback_used": False,
        "candidate_count": candidate_count,
        "days_between_releases": None,
    }


def _ambiguous_result(current: _Release, candidate_count: int) -> PairingResult:
    return {
        "ticker": current.ticker,
        "current_release_id": current.release_id,
        "previous_release_id": None,
        "current_release_at": current.release_at_text,
        "previous_release_at": None,
        "pairing_status": "AMBIGUOUS",
        "pairing_method": "AMBIGUOUS",
        "fiscal_continuity": "UNKNOWN",
        "fallback_used": False,
        "candidate_count": candidate_count,
        "days_between_releases": None,
    }
