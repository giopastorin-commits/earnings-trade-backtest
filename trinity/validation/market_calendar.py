"""Versioned XNYS session utilities for offline validation observations."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import importlib.metadata

import exchange_calendars

from .contracts import ValidationContractError


CALENDAR_NAME = "XNYS"
CALENDAR_VERSION = importlib.metadata.version("exchange-calendars")


def _aware(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationContractError(f"invalid {field}") from exc
    if parsed.tzinfo is None:
        raise ValidationContractError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def session_times(session: str) -> tuple[datetime, datetime]:
    """Return authoritative UTC open/close for one XNYS session."""

    try:
        day = date.fromisoformat(session)
    except ValueError as exc:
        raise ValidationContractError("invalid XNYS session") from exc
    calendar = exchange_calendars.get_calendar(
        CALENDAR_NAME, start=day - timedelta(days=2), end=day + timedelta(days=2),
    )
    try:
        row = calendar.schedule.loc[session]
    except KeyError as exc:
        raise ValidationContractError(f"not an XNYS session: {session}") from exc
    return (
        row["open"].to_pydatetime().astimezone(timezone.utc),
        row["close"].to_pydatetime().astimezone(timezone.utc),
    )


def derive_outcome_sessions(
    decision_timestamp_utc: str, horizon_sessions: int,
) -> tuple[str, str]:
    """Derive D1 and inclusive DN without consulting future prices."""

    if horizon_sessions <= 0:
        raise ValidationContractError("outcome horizon must be positive")
    decision = _aware(decision_timestamp_utc, "decision_timestamp_utc")
    start = decision.date() - timedelta(days=2)
    end = decision.date() + timedelta(days=max(120, horizon_sessions * 3))
    calendar = exchange_calendars.get_calendar(CALENDAR_NAME, start=start, end=end)
    eligible: list[str] = []
    for label, row in calendar.schedule.iterrows():
        opened = row["open"].to_pydatetime().astimezone(timezone.utc)
        if opened > decision:
            eligible.append(label.date().isoformat())
        if len(eligible) == horizon_sessions:
            break
    if len(eligible) != horizon_sessions:
        raise ValidationContractError("calendar does not cover requested outcome horizon")
    return eligible[0], eligible[-1]


def add_tradable_at(bars: list[dict[str, object]]) -> tuple[dict[str, object], ...]:
    """Add XNYS-open timestamps to detached normalized bars."""

    if not bars:
        raise ValidationContractError("stock observation contains no bars")
    try:
        dates = [date.fromisoformat(str(row.get("date") or "")) for row in bars]
    except ValueError as exc:
        raise ValidationContractError("invalid stock observation session") from exc
    calendar = exchange_calendars.get_calendar(
        CALENDAR_NAME, start=min(dates) - timedelta(days=1), end=max(dates) + timedelta(days=1),
    )
    result: list[dict[str, object]] = []
    previous = ""
    for source in bars:
        row = dict(source)
        session = str(row.get("date") or "")
        if not session or session <= previous:
            raise ValidationContractError("stock sessions must be unique and ordered")
        try:
            opened = calendar.schedule.loc[session]["open"].to_pydatetime().astimezone(timezone.utc)
        except KeyError as exc:
            raise ValidationContractError(f"not an XNYS session: {session}") from exc
        row["tradable_at"] = opened.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        result.append(row)
        previous = session
    return tuple(result)


def parse_utc(value: str, field: str) -> datetime:
    return _aware(value, field)
