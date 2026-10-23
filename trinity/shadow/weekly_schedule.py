"""DST-safe weekly schedule gate backed by the established XNYS calendar."""

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
SCHEDULES = frozenset({"0 10 * * 1", "0 11 * * 1"})
CONTRACT_VERSION = "TRINITY_WEEKLY_ORCHESTRATOR_V1"
CALENDAR_NAME = "XNYS"
MODES = frozenset({"VALIDATION", "OFFICIAL"})
EXECUTION_SCOPES = frozenset({"PREFLIGHT_ONLY", "FULL_SMOKE"})
FINALITY_GRACE = timedelta(minutes=20)
WEEKLY_TARGET_RESOLUTION = "WEEKLY_INTENDED_MONDAY"
SMOKE_TARGET_RESOLUTION = "VALIDATION_LATEST_FINAL_XNYS"


@dataclass(frozen=True)
class WeeklyScheduleDecision:
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


def resolve_completed_xnys_session(intended_monday: date) -> dict[str, str]:
    """Resolve the last XNYS close at or before intended Rome Monday noon."""

    intended_instant = datetime.combine(intended_monday, time(12), ROME).astimezone(timezone.utc)
    calendar = exchange_calendars.get_calendar(
        CALENDAR_NAME,
        start=intended_monday - timedelta(days=14),
        end=intended_monday + timedelta(days=1),
    )
    session = calendar.minute_to_past_session(pd.Timestamp(intended_instant))
    row = calendar.schedule.loc[session]

    def utc_text(value: pd.Timestamp) -> str:
        return value.to_pydatetime().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    return {
        "target_market_session": session.date().isoformat(),
        "target_session_open_utc": utc_text(row["open"]),
        "target_session_close_utc": utc_text(row["close"]),
        "session_calendar": CALENDAR_NAME,
        "session_calendar_version": importlib.metadata.version("exchange-calendars"),
    }


def resolve_latest_final_xnys_session(run_started_at: datetime) -> dict[str, str]:
    """Resolve the latest XNYS session final by close plus the frozen grace."""

    if run_started_at.tzinfo is None or run_started_at.utcoffset() is None:
        raise ValueError("run_started_at must be timezone-aware")
    started_utc = run_started_at.astimezone(timezone.utc)
    started_day = started_utc.date()
    calendar = exchange_calendars.get_calendar(
        CALENDAR_NAME,
        start=started_day - timedelta(days=31),
        end=started_day + timedelta(days=1),
    )
    eligible = calendar.schedule[
        calendar.schedule["close"] + pd.Timedelta(FINALITY_GRACE) <= pd.Timestamp(started_utc)
    ]
    if eligible.empty:
        raise ValueError("no final XNYS session is available for run_started_at")
    session = eligible.index[-1]
    row = eligible.iloc[-1]

    def utc_text(value: pd.Timestamp) -> str:
        return value.to_pydatetime().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    return {
        "target_market_session": session.date().isoformat(),
        "target_session_open_utc": utc_text(row["open"]),
        "target_session_close_utc": utc_text(row["close"]),
        "session_calendar": CALENDAR_NAME,
        "session_calendar_version": importlib.metadata.version("exchange-calendars"),
    }


def _intended_monday(started_at: datetime) -> date:
    if started_at.tzinfo is None:
        raise ValueError("run_started_at must be timezone-aware")
    local_day = started_at.astimezone(ROME).date()
    return local_day - timedelta(days=local_day.weekday())


def decide_weekly_schedule(
    *, event_name: str, event_schedule: str | None, run_started_at: datetime,
    manual_monday: date | None = None, requested_mode: str | None = None,
    execution_scope: str | None = None,
) -> WeeklyScheduleDecision:
    if run_started_at.tzinfo is None or run_started_at.utcoffset() is None:
        raise ValueError("run_started_at must be timezone-aware")
    if event_name not in {"schedule", "workflow_dispatch"}:
        raise ValueError("unsupported weekly-production event")
    mode = "OFFICIAL" if event_name == "schedule" else str(requested_mode or "VALIDATION").upper()
    if mode not in MODES:
        raise ValueError("mode must be VALIDATION or OFFICIAL")
    scope = str(execution_scope).upper() if execution_scope not in (None, "") else None
    if scope is not None and scope not in EXECUTION_SCOPES:
        raise ValueError("execution_scope must be PREFLIGHT_ONLY or FULL_SMOKE")
    if mode == "OFFICIAL" and scope == "FULL_SMOKE":
        raise ValueError("FULL_SMOKE is restricted to VALIDATION")
    if event_name == "schedule" and requested_mode not in (None, "", "OFFICIAL"):
        raise ValueError("scheduled weekly production must be OFFICIAL")
    monday = manual_monday or _intended_monday(run_started_at)
    if monday.weekday() != 0:
        raise ValueError("intended production date must be a Monday")
    local_noon = datetime.combine(monday, time(12), ROME)
    expected = f"0 {local_noon.astimezone(timezone.utc).hour} * * 1"
    if expected not in SCHEDULES:
        raise ValueError("Europe/Rome noon resolved outside registered schedules")
    if event_name == "schedule" and event_schedule not in SCHEDULES:
        raise ValueError("unrecognized scheduled cron expression")
    should_run = event_name == "workflow_dispatch" or event_schedule == expected
    latest_smoke = event_name == "workflow_dispatch" and mode == "VALIDATION" \
        and scope == "FULL_SMOKE"
    target = (
        resolve_latest_final_xnys_session(run_started_at)
        if latest_smoke else resolve_completed_xnys_session(monday)
    )
    return WeeklyScheduleDecision(
        event_name=event_name, mode=mode, execution_scope=scope,
        intended_monday=monday.isoformat(),
        expected_schedule=expected, received_schedule=event_schedule, should_run=should_run,
        target_session_resolution=(
            SMOKE_TARGET_RESOLUTION if latest_smoke else WEEKLY_TARGET_RESOLUTION
        ),
        **target,
    )


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("run_started_at must include a timezone")
    return parsed


def _write_github_output(decision: WeeklyScheduleDecision) -> None:
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
    parser.add_argument("--mode", choices=sorted(MODES))
    parser.add_argument("--execution-scope", choices=sorted(EXECUTION_SCOPES))
    args = parser.parse_args(argv)
    decision = decide_weekly_schedule(
        event_name=args.event_name, event_schedule=args.event_schedule or None,
        run_started_at=args.run_started_at, manual_monday=args.manual_monday,
        requested_mode=args.mode, execution_scope=args.execution_scope,
    )
    _write_github_output(decision)
    print(json.dumps(asdict(decision), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
