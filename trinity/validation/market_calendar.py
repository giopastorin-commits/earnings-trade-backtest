"""Versioned XNYS session utilities for offline validation observations."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import importlib.metadata
import json
from typing import Any, Mapping

import exchange_calendars

from .contracts import ValidationContractError


CALENDAR_NAME = "XNYS"
CALENDAR_VERSION = importlib.metadata.version("exchange-calendars")


def _aware(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValidationContractError(f"invalid {field}") from exc
    if parsed.tzinfo is None:
        raise ValidationContractError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def session_times(session: str) -> tuple[datetime, datetime]:
    """Return authoritative UTC open/close for one XNYS session."""

    try:
        day = date.fromisoformat(session)
    except (TypeError, ValueError) as exc:
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


def _outcome_schedule(
    decision_timestamp_utc: str, horizon_sessions: int,
) -> tuple[datetime, list[tuple[str, datetime, datetime]]]:
    if horizon_sessions <= 0:
        raise ValidationContractError("outcome horizon must be positive")
    decision = _aware(decision_timestamp_utc, "decision_timestamp_utc")
    start = decision.date() - timedelta(days=2)
    end = decision.date() + timedelta(days=max(120, horizon_sessions * 3))
    calendar = exchange_calendars.get_calendar(CALENDAR_NAME, start=start, end=end)
    eligible: list[tuple[str, datetime, datetime]] = []
    for label, row in calendar.schedule.iterrows():
        opened = row["open"].to_pydatetime().astimezone(timezone.utc)
        if opened > decision:
            eligible.append((
                label.date().isoformat(), opened,
                row["close"].to_pydatetime().astimezone(timezone.utc),
            ))
        if len(eligible) == horizon_sessions:
            break
    if len(eligible) != horizon_sessions:
        raise ValidationContractError("calendar does not cover requested outcome horizon")
    return decision, eligible


def derive_outcome_sessions(
    decision_timestamp_utc: str, horizon_sessions: int,
) -> tuple[str, str]:
    """Derive D1 and inclusive DN from XNYS opens, without consulting prices."""

    _decision, eligible = _outcome_schedule(decision_timestamp_utc, horizon_sessions)
    return eligible[0][0], eligible[-1][0]


def schedule_evidence_sha256(evidence: Mapping[str, Any]) -> str:
    """Hash detached schedule evidence without consulting the runtime calendar."""

    detached = dict(evidence)
    detached.pop("schedule_sha256", None)
    raw = json.dumps(
        detached, ensure_ascii=False, allow_nan=False,
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def outcome_schedule_evidence(
    decision_timestamp_utc: str, horizon_sessions: int,
) -> dict[str, Any]:
    """Return deterministic creation-time provenance for a frozen D1..DN slice.

    The recorded calendar version is provenance, not a requirement that future
    readers run that same version. Historical verification policy belongs to
    the future expectation contract.
    """

    decision, eligible = _outcome_schedule(decision_timestamp_utc, horizon_sessions)
    evidence: dict[str, Any] = {
        "calendar": CALENDAR_NAME,
        "calendar_version": CALENDAR_VERSION,
        "decision_timestamp_utc": _utc_timestamp(decision),
        "horizon_sessions": horizon_sessions,
        "first_tradable_session": eligible[0][0],
        "maturity_session": eligible[-1][0],
        "sessions": [
            {
                "session": session,
                "open_utc": _utc_timestamp(opened),
                "close_utc": _utc_timestamp(closed),
            }
            for session, opened, closed in eligible
        ],
    }
    evidence["schedule_sha256"] = schedule_evidence_sha256(evidence)
    return evidence


def add_tradable_at(bars: list[dict[str, object]]) -> tuple[dict[str, object], ...]:
    """Add XNYS-open timestamps to detached normalized bars."""

    if not bars:
        raise ValidationContractError("stock observation contains no bars")
    try:
        dates = [date.fromisoformat(str(row.get("date") or "")) for row in bars]
    except (AttributeError, TypeError, ValueError) as exc:
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
        row["tradable_at"] = _utc_timestamp(opened)
        result.append(row)
        previous = session
    return tuple(result)


def parse_utc(value: str, field: str) -> datetime:
    """Parse an ISO-8601 timestamp and normalize it to aware UTC."""

    return _aware(value, field)
