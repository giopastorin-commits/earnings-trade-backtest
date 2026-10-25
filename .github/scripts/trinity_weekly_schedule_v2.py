"""Workflow-owned, DST-safe TRINITY weekly orchestrator V2 schedule gate."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
import importlib.metadata
import json
import os
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

import exchange_calendars
import pandas as pd


ROME = ZoneInfo("Europe/Rome")
CALENDAR_NAME = "XNYS"
CONTRACT_VERSION = "TRINITY_WEEKLY_ORCHESTRATOR_V2"
VALIDATION_CONTRACT_VERSION = "TRINITY_WEEKLY_ORCHESTRATOR_V1"
SCHEDULES = frozenset({"0 21 * * 1", "0 22 * * 1"})
FINALITY_GRACE = timedelta(minutes=20)
OFFICIAL_TARGET_RESOLUTION = "LATEST_FINAL_XNYS_AT_MONDAY_2300_EUROPE_ROME"
VALIDATION_WEEKLY_TARGET_RESOLUTION = "WEEKLY_INTENDED_MONDAY"
VALIDATION_SMOKE_TARGET_RESOLUTION = "VALIDATION_LATEST_FINAL_XNYS"


@dataclass(frozen=True)
class ScheduleDecision:
    event_name: str
    mode: str
    execution_scope: str | None
    intended_monday: str
    expected_schedule: str
    received_schedule: str | None
    should_run: bool
    target_market_session: str
    target_session_open_utc: str
    target_session_close_utc: str
    session_calendar: str
    session_calendar_version: str
    target_session_resolution: str
    method_version: str = CONTRACT_VERSION


def _utc_text(value: pd.Timestamp) -> str:
    return value.to_pydatetime().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _calendar_result(session: pd.Timestamp, row: pd.Series) -> dict[str, str]:
    return {
        "target_market_session": session.date().isoformat(),
        "target_session_open_utc": _utc_text(row["open"]),
        "target_session_close_utc": _utc_text(row["close"]),
        "session_calendar": CALENDAR_NAME,
        "session_calendar_version": importlib.metadata.version("exchange-calendars"),
    }


def resolve_latest_final_xnys_session(decision_instant: datetime) -> dict[str, str]:
    """Resolve the latest session final by close plus the frozen 20-minute grace."""

    if decision_instant.tzinfo is None or decision_instant.utcoffset() is None:
        raise ValueError("decision_instant must be timezone-aware")
    instant_utc = decision_instant.astimezone(timezone.utc)
    calendar = exchange_calendars.get_calendar(
        CALENDAR_NAME,
        start=instant_utc.date() - timedelta(days=31),
        end=instant_utc.date() + timedelta(days=1),
    )
    eligible = calendar.schedule[
        calendar.schedule["close"] + pd.Timedelta(FINALITY_GRACE) <= pd.Timestamp(instant_utc)
    ]
    if eligible.empty:
        raise ValueError("no final XNYS session is available for decision_instant")
    session = eligible.index[-1]
    return _calendar_result(session, eligible.iloc[-1])


def resolve_validation_weekly_session(intended_monday: date) -> dict[str, str]:
    """Preserve the V1 manual non-smoke target: last session by Rome Monday noon."""

    instant = datetime.combine(intended_monday, time(12), ROME).astimezone(timezone.utc)
    calendar = exchange_calendars.get_calendar(
        CALENDAR_NAME,
        start=intended_monday - timedelta(days=14),
        end=intended_monday + timedelta(days=1),
    )
    session = calendar.minute_to_past_session(pd.Timestamp(instant))
    return _calendar_result(session, calendar.schedule.loc[session])


def _intended_monday(started_at: datetime) -> date:
    if started_at.tzinfo is None or started_at.utcoffset() is None:
        raise ValueError("run_started_at must be timezone-aware")
    local_day = started_at.astimezone(ROME).date()
    return local_day - timedelta(days=local_day.weekday())


def _official_decision_instant(monday: date) -> datetime:
    return datetime.combine(monday, time(23), ROME)


def _expected_schedule(monday: date) -> str:
    hour = _official_decision_instant(monday).astimezone(timezone.utc).hour
    expected = f"0 {hour} * * 1"
    if expected not in SCHEDULES:
        raise ValueError("Europe/Rome 23:00 resolved outside registered schedules")
    return expected


def decide_schedule(
    *, event_name: str, event_schedule: str | None, run_started_at: datetime,
    manual_monday: date | None = None, requested_mode: str | None = None,
    execution_scope: str | None = None,
) -> ScheduleDecision:
    if event_name not in {"schedule", "workflow_dispatch"}:
        raise ValueError("unsupported weekly-production event")
    monday = manual_monday or _intended_monday(run_started_at)
    if monday.weekday() != 0:
        raise ValueError("intended production date must be a Monday")
    expected = _expected_schedule(monday)

    if event_name == "schedule":
        if event_schedule not in SCHEDULES:
            raise ValueError("unrecognized scheduled cron expression")
        if requested_mode not in (None, "", "OFFICIAL"):
            raise ValueError("scheduled weekly production must be OFFICIAL")
        target = resolve_latest_final_xnys_session(_official_decision_instant(monday))
        return ScheduleDecision(
            event_name=event_name, mode="OFFICIAL", execution_scope="OFFICIAL_WEEKLY",
            intended_monday=monday.isoformat(), expected_schedule=expected,
            received_schedule=event_schedule, should_run=event_schedule == expected,
            target_session_resolution=OFFICIAL_TARGET_RESOLUTION, **target,
        )

    mode = str(requested_mode or "VALIDATION").upper()
    if mode != "VALIDATION":
        raise ValueError("manual weekly production is restricted to VALIDATION")
    scope = str(execution_scope or "PREFLIGHT_ONLY").upper()
    if scope not in {"PREFLIGHT_ONLY", "FULL_SMOKE"}:
        raise ValueError("invalid VALIDATION execution_scope")
    if scope == "FULL_SMOKE":
        target = resolve_latest_final_xnys_session(run_started_at)
        resolution = VALIDATION_SMOKE_TARGET_RESOLUTION
    else:
        target = resolve_validation_weekly_session(monday)
        resolution = VALIDATION_WEEKLY_TARGET_RESOLUTION
    return ScheduleDecision(
        event_name=event_name, mode=mode, execution_scope=scope,
        intended_monday=monday.isoformat(), expected_schedule=expected,
        received_schedule=event_schedule, should_run=True,
        target_session_resolution=resolution, method_version=VALIDATION_CONTRACT_VERSION,
        **target,
    )


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("run_started_at must include a timezone")
    return parsed


def _write_github_output(decision: ScheduleDecision) -> None:
    output = os.getenv("GITHUB_OUTPUT")
    if not output:
        return
    values = asdict(decision)
    values["should_run"] = str(decision.should_run).lower()
    with Path(output).open("a", encoding="utf-8") as stream:
        for key, value in values.items():
            if value is not None:
                stream.write(f"{key}={value}\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--event-schedule")
    parser.add_argument("--run-started-at", required=True, type=_parse_timestamp)
    parser.add_argument("--manual-monday", type=date.fromisoformat)
    parser.add_argument("--mode")
    parser.add_argument("--execution-scope")
    args = parser.parse_args(argv)
    decision = decide_schedule(
        event_name=args.event_name, event_schedule=args.event_schedule or None,
        run_started_at=args.run_started_at, manual_monday=args.manual_monday,
        requested_mode=args.mode, execution_scope=args.execution_scope,
    )
    _write_github_output(decision)
    print(json.dumps(asdict(decision), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
