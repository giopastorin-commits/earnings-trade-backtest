from __future__ import annotations

from datetime import date, datetime, timezone
import inspect
import socket

import pytest

import trinity.validation.observation_slots as slots_module
from trinity.validation.market_calendar import derive_outcome_sessions
from trinity.validation.observation_slots import (
    ObservationSlotError,
    first_eligible_observation_slot,
    observation_slot_finality,
    observation_slot_identity,
    weekly_observation_slot,
    weekly_slot_target_for_iso_week,
)
from trinity.validation.protocol_v1 import (
    HORIZONS,
    PROTOCOL,
    PROTOCOL_EPOCH,
    PROTOCOL_HASH,
    PROTOCOL_ID,
)


OLD_PRE_LIVE_HASH = "38980fc7bdc0ab8912f23cbc8ce67325f0d0ffe896a8d29838fe81ed1d094a1a"


def _week(day: date) -> tuple[int, int]:
    iso = day.isocalendar()
    return iso.year, iso.week


def test_protocol_identity_and_weekly_policy_are_frozen():
    assert PROTOCOL_ID == "TRINITY_LIVE_VALIDATION_V1"
    assert PROTOCOL_EPOCH == 1
    assert PROTOCOL_HASH != OLD_PRE_LIVE_HASH
    assert dict(PROTOCOL["observation_policy"]) == {
        "cadence": "WEEKLY",
        "week_definition": "ISO_WEEK",
        "slot_target": "FIRST_XNYS_SESSION_OF_ISO_WEEK",
        "finality_grace_minutes": 20,
        "first_eligible_slot": "FIRST_SLOT_TARGET_ON_OR_AFTER_MATURITY_SESSION",
        "slot_expected_set": "ALL_MATURE_UNASSIGNED_V1_EXPECTATIONS",
        "missing_slot": "REMAIN_PENDING",
        "partial_cycle_official": False,
        "missing_slot_is_dqf": False,
        "multiple_candidate_cycles": "AMBIGUOUS_OPERATIONAL_ERROR",
    }


def test_normal_week_targets_monday():
    assert weekly_slot_target_for_iso_week(*_week(date(2026, 10, 5))) == "2026-10-05"


def test_monday_holiday_targets_first_actual_tuesday_session():
    assert weekly_slot_target_for_iso_week(*_week(date(2026, 9, 7))) == "2026-09-08"


def test_iso_year_boundary_and_christmas_new_year_period():
    assert weekly_slot_target_for_iso_week(*_week(date(2025, 12, 29))) == "2025-12-29"
    assert weekly_slot_target_for_iso_week(*_week(date(2026, 1, 5))) == "2026-01-05"


def test_thanksgiving_week_still_targets_first_session():
    assert weekly_slot_target_for_iso_week(*_week(date(2026, 11, 23))) == "2026-11-23"


def test_dst_changes_utc_finality_not_session_date():
    before = weekly_observation_slot(*_week(date(2026, 3, 2)))
    after = weekly_observation_slot(*_week(date(2026, 3, 9)))
    assert before.slot_target_session == "2026-03-02"
    assert after.slot_target_session == "2026-03-09"
    assert before.finality_eligible_at_utc.endswith("21:20:00.000000Z")
    assert after.finality_eligible_at_utc.endswith("20:20:00.000000Z")


def test_early_close_finality_uses_actual_xnys_close():
    assert observation_slot_finality("2026-11-27") == datetime(
        2026, 11, 27, 18, 20, tzinfo=timezone.utc,
    )


def test_slot_identity_is_deterministic_and_target_bound():
    year, week = _week(date(2026, 10, 5))
    first = observation_slot_identity(year, week)
    second = observation_slot_identity(year, week, "2026-10-05")
    assert first == second and len(first) == 64
    assert weekly_observation_slot(year, week).observation_slot_id == first
    with pytest.raises(ObservationSlotError, match="does not match"):
        observation_slot_identity(year, week, "2026-10-06")


def test_invalid_or_empty_iso_week_fails_closed(monkeypatch):
    with pytest.raises(ObservationSlotError, match="invalid ISO"):
        weekly_slot_target_for_iso_week(2026, 99)

    class EmptyCalendar:
        sessions = ()

    monkeypatch.setattr(slots_module.exchange_calendars, "get_calendar", lambda *a, **k: EmptyCalendar())
    with pytest.raises(ObservationSlotError, match="no XNYS"):
        weekly_slot_target_for_iso_week(2026, 41)


def test_monday_maturity_uses_same_week_slot():
    slot = first_eligible_observation_slot("2026-10-05")
    assert slot.slot_target_session == "2026-10-05"


def test_tuesday_maturity_after_normal_monday_uses_next_week():
    slot = first_eligible_observation_slot("2026-10-06")
    assert slot.slot_target_session == "2026-10-12"


def test_friday_maturity_uses_next_week_without_retroactive_assignment():
    slot = first_eligible_observation_slot("2026-10-09")
    assert slot.slot_target_session == "2026-10-12"
    assert slot.slot_target_session > "2026-10-09"


def test_monday_holiday_tuesday_maturity_uses_same_tuesday_slot():
    slot = first_eligible_observation_slot("2026-09-08")
    assert slot.slot_target_session == "2026-09-08"


def test_non_xnys_maturity_is_rejected():
    with pytest.raises(ObservationSlotError, match="XNYS"):
        first_eligible_observation_slot("2026-10-10")


def test_outcome_horizons_and_d1_dn_semantics_are_unchanged():
    assert HORIZONS == (5, 20, 60)
    assert PROTOCOL["primary_horizon_sessions"] == 20
    assert PROTOCOL["horizon_definition"] == \
        "FIRST_TRADABLE_OPEN_TO_NTH_TRADABLE_SESSION_CLOSE_INCLUSIVE"
    assert derive_outcome_sessions("2026-10-09T20:01:00Z", 5) == (
        "2026-10-12", "2026-10-16",
    )


def test_slot_module_has_no_price_network_assignment_or_maturity_state(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket, "create_connection", denied)
    source = inspect.getsource(slots_module)
    assert "twelvedata" not in source.lower()
    assert "OUTCOME_FINAL" not in source
    assert "DATA_QUALITY_FAILURE" not in source
    assert "assign_expectation" not in source
    weekly_observation_slot(*_week(date(2026, 10, 5)))
