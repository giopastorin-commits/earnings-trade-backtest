"""Frozen weekly XNYS observation-slot governance for Live Validation V1.

Slots govern when immutable observation artifacts may be acquired.  They do
not alter D1/DN, calculate returns, assign expectations, or turn a missed
operational observation into a data-quality failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib

import exchange_calendars

from .contracts import ValidationContractError
from .market_calendar import CALENDAR_NAME, session_times
from .protocol_v1 import (
    PROTOCOL,
    PROTOCOL_EPOCH,
    PROTOCOL_HASH,
    PROTOCOL_ID,
    canonical_json,
)


CADENCE = "WEEKLY"
WEEK_DEFINITION = "ISO_WEEK"
SLOT_TARGET_POLICY = "FIRST_XNYS_SESSION_OF_ISO_WEEK"
FINALITY_GRACE_MINUTES = 20
FIRST_ELIGIBLE_POLICY = "FIRST_SLOT_TARGET_ON_OR_AFTER_MATURITY_SESSION"

_EXPECTED_POLICY = {
    "cadence": CADENCE,
    "week_definition": WEEK_DEFINITION,
    "slot_target": SLOT_TARGET_POLICY,
    "finality_grace_minutes": FINALITY_GRACE_MINUTES,
    "first_eligible_slot": FIRST_ELIGIBLE_POLICY,
    "slot_expected_set": "ALL_MATURE_UNASSIGNED_V1_EXPECTATIONS",
    "missing_slot": "REMAIN_PENDING",
    "partial_cycle_official": False,
    "missing_slot_is_dqf": False,
    "multiple_candidate_cycles": "AMBIGUOUS_OPERATIONAL_ERROR",
}
if dict(PROTOCOL.get("observation_policy", {})) != _EXPECTED_POLICY:
    raise RuntimeError("TRINITY V1 observation-slot policy drift")


class ObservationSlotError(ValidationContractError):
    """An ISO week or maturity session cannot satisfy frozen slot policy."""


def _iso_monday(iso_year: int, iso_week: int) -> date:
    if type(iso_year) is not int or type(iso_week) is not int:
        raise ObservationSlotError("ISO year and week must be integers")
    try:
        return date.fromisocalendar(iso_year, iso_week, 1)
    except ValueError as exc:
        raise ObservationSlotError("invalid ISO year/week") from exc


def _utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def weekly_slot_target_for_iso_week(iso_year: int, iso_week: int) -> str:
    """Return the first actual XNYS trading session in an ISO week."""

    monday = _iso_monday(iso_year, iso_week)
    sunday = monday + timedelta(days=6)
    calendar = exchange_calendars.get_calendar(
        CALENDAR_NAME, start=monday, end=sunday,
    )
    sessions = [item.date() for item in calendar.sessions]
    sessions = [item for item in sessions if monday <= item <= sunday]
    if not sessions:
        raise ObservationSlotError("ISO week contains no XNYS trading session")
    target = sessions[0]
    if target.isocalendar()[:2] != (iso_year, iso_week):
        raise ObservationSlotError("XNYS slot target is outside requested ISO week")
    return target.isoformat()


def observation_slot_finality(slot_target_session: str) -> datetime:
    """Return close plus the frozen 20-minute data-integrity grace in UTC."""

    try:
        _opened, closed = session_times(slot_target_session)
    except ValidationContractError as exc:
        raise ObservationSlotError("slot target is not an XNYS session") from exc
    return closed.astimezone(timezone.utc) + timedelta(minutes=FINALITY_GRACE_MINUTES)


def observation_slot_identity(
    iso_year: int, iso_week: int, slot_target_session: str | None = None,
) -> str:
    """Return the deterministic content identity of one protocol-week slot."""

    target = weekly_slot_target_for_iso_week(iso_year, iso_week)
    if slot_target_session is not None and slot_target_session != target:
        raise ObservationSlotError("slot target does not match frozen ISO-week policy")
    core = {
        "protocol_hash": PROTOCOL_HASH,
        "iso_year": iso_year,
        "iso_week": iso_week,
        "slot_target_session": target,
    }
    return hashlib.sha256(canonical_json(core)).hexdigest()


@dataclass(frozen=True)
class ObservationSlot:
    protocol_id: str
    protocol_hash: str
    protocol_epoch: int
    cadence: str
    week_definition: str
    iso_year: int
    iso_week: int
    slot_target_session: str
    finality_eligible_at_utc: str
    observation_slot_id: str


def weekly_observation_slot(iso_year: int, iso_week: int) -> ObservationSlot:
    target = weekly_slot_target_for_iso_week(iso_year, iso_week)
    return ObservationSlot(
        protocol_id=PROTOCOL_ID,
        protocol_hash=PROTOCOL_HASH,
        protocol_epoch=PROTOCOL_EPOCH,
        cadence=CADENCE,
        week_definition=WEEK_DEFINITION,
        iso_year=iso_year,
        iso_week=iso_week,
        slot_target_session=target,
        finality_eligible_at_utc=_utc_timestamp(observation_slot_finality(target)),
        observation_slot_id=observation_slot_identity(iso_year, iso_week, target),
    )


def first_eligible_observation_slot(maturity_session: str) -> ObservationSlot:
    """Return the first weekly target on or after an immutable DN session."""

    try:
        maturity = date.fromisoformat(maturity_session)
        session_times(maturity_session)
    except (TypeError, ValueError, ValidationContractError) as exc:
        raise ObservationSlotError("maturity_session must be an XNYS session") from exc
    iso_year, iso_week, _weekday = maturity.isocalendar()
    current = weekly_observation_slot(iso_year, iso_week)
    if date.fromisoformat(current.slot_target_session) >= maturity:
        return current
    next_monday = _iso_monday(iso_year, iso_week) + timedelta(days=7)
    next_year, next_week, _weekday = next_monday.isocalendar()
    return weekly_observation_slot(next_year, next_week)
