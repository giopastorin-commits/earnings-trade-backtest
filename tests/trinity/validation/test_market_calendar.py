from __future__ import annotations

from datetime import timezone

import pytest

from trinity.validation.contracts import ValidationContractError
from trinity.validation.market_calendar import (
    CALENDAR_NAME,
    CALENDAR_VERSION,
    add_tradable_at,
    derive_outcome_sessions,
    outcome_schedule_evidence,
    parse_utc,
    schedule_evidence_sha256,
    session_times,
)


def test_decision_before_open_uses_same_session_as_d1():
    assert derive_outcome_sessions("2026-03-09T13:29:59Z", 5) == (
        "2026-03-09", "2026-03-13",
    )


@pytest.mark.parametrize("decision", [
    "2026-03-09T13:30:00Z",
    "2026-03-09T13:30:01Z",
    "2026-03-09T20:00:01Z",
])
def test_decision_at_or_after_open_uses_next_session_as_d1(decision):
    assert derive_outcome_sessions(decision, 5)[0] == "2026-03-10"


def test_weekend_and_holiday_are_not_sessions():
    assert derive_outcome_sessions("2026-03-07T12:00:00Z", 5)[0] == "2026-03-09"
    assert derive_outcome_sessions("2026-07-03T12:00:00Z", 5)[0] == "2026-07-06"


def test_thanksgiving_is_skipped_and_early_close_is_d1():
    assert derive_outcome_sessions("2026-11-26T12:00:00Z", 5)[0] == "2026-11-27"


@pytest.mark.parametrize(("horizon", "expected_dn"), [
    (5, "2026-10-16"),
    (20, "2026-11-06"),
    (60, "2027-01-06"),
])
def test_frozen_horizon_uses_inclusive_nth_xnys_session(horizon, expected_dn):
    assert derive_outcome_sessions("2026-10-09T20:01:00Z", horizon) == (
        "2026-10-12", expected_dn,
    )


def test_edt_and_est_session_times_come_from_xnys():
    edt_open, edt_close = session_times("2026-03-09")
    est_open, est_close = session_times("2026-11-02")
    assert edt_open.isoformat() == "2026-03-09T13:30:00+00:00"
    assert edt_close.isoformat() == "2026-03-09T20:00:00+00:00"
    assert est_open.isoformat() == "2026-11-02T14:30:00+00:00"
    assert est_close.isoformat() == "2026-11-02T21:00:00+00:00"


def test_early_close_comes_from_xnys_schedule():
    opened, closed = session_times("2026-11-27")
    assert opened.isoformat() == "2026-11-27T14:30:00+00:00"
    assert closed.isoformat() == "2026-11-27T18:00:00+00:00"


@pytest.mark.parametrize("horizon", [0, -1])
def test_non_positive_horizon_is_rejected(horizon):
    with pytest.raises(ValidationContractError, match="positive"):
        derive_outcome_sessions("2026-10-09T20:01:00Z", horizon)


def test_invalid_or_naive_timestamp_is_rejected():
    with pytest.raises(ValidationContractError, match="invalid"):
        parse_utc("garbage", "decision")
    with pytest.raises(ValidationContractError, match="timezone-aware"):
        parse_utc("2026-10-09T20:01:00", "decision")


def test_non_xnys_session_is_rejected():
    with pytest.raises(ValidationContractError, match="not an XNYS session"):
        session_times("2026-11-28")


def test_add_tradable_at_preserves_order_and_uses_real_xnys_open():
    bars = [
        {"date": "2026-03-09", "open": 100.0, "close": 101.0},
        {"date": "2026-11-02", "open": 102.0, "close": 103.0},
    ]
    adapted = add_tradable_at(bars)
    assert [row["date"] for row in adapted] == ["2026-03-09", "2026-11-02"]
    assert adapted[0]["tradable_at"] == "2026-03-09T13:30:00.000000Z"
    assert adapted[1]["tradable_at"] == "2026-11-02T14:30:00.000000Z"
    assert "tradable_at" not in bars[0]


@pytest.mark.parametrize("bars", [
    [{"date": "2026-03-09"}, {"date": "2026-03-09"}],
    [{"date": "2026-03-10"}, {"date": "2026-03-09"}],
])
def test_duplicate_or_unordered_stock_sessions_are_rejected(bars):
    with pytest.raises(ValidationContractError, match="unique and ordered"):
        add_tradable_at(bars)


def test_add_tradable_at_rejects_non_xnys_session():
    with pytest.raises(ValidationContractError, match="not an XNYS session"):
        add_tradable_at([{"date": "2026-11-28"}])


def test_schedule_evidence_is_deterministic_creation_time_provenance():
    first = outcome_schedule_evidence("2026-10-09T20:01:00Z", 5)
    second = outcome_schedule_evidence("2026-10-09T22:01:00+02:00", 5)
    assert first == second
    assert first["calendar"] == CALENDAR_NAME == "XNYS"
    assert first["calendar_version"] == CALENDAR_VERSION
    assert first["first_tradable_session"] == "2026-10-12"
    assert first["maturity_session"] == "2026-10-16"
    assert len(first["sessions"]) == 5
    assert first["schedule_sha256"] == schedule_evidence_sha256(first)


def test_parse_utc_normalizes_offsets_without_runtime_version_policy():
    parsed = parse_utc("2026-10-09T22:01:00+02:00", "decision")
    assert parsed.tzinfo == timezone.utc
    assert parsed.isoformat() == "2026-10-09T20:01:00+00:00"
