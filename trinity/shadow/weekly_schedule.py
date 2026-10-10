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


@dataclass(frozen=True)
class WeeklyScheduleDecision:
    event_name: str
    mode: str
    intended_monday: str
    expected_schedule: str
    received_schedule: str | None
    should_run: bool
    target_market_session: str
    target_session_open_utc: str
    target_session_close_utc: str
    session_calendar: str
    session_calendar_version: str
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


def _intended_monday(started_at: datetime) -> date:
    if started_at.tzinfo is None:
        raise ValueError("run_started_at must be timezone-aware")
    local_day = started_at.astimezone(ROME).date()
    return local_day - timedelta(days=local_day.weekday())


def decide_weekly_schedule(
    *, event_name: str, event_schedule: str | None, run_started_at: datetime,
    manual_monday: date | None = None, requested_mode: str | None = None,
) -> WeeklyScheduleDecision:
    if event_name not in {"schedule", "workflow_dispatch"}:
        raise ValueError("unsupported weekly-production event")
    mode = "OFFICIAL" if event_name == "schedule" else str(requested_mode or "VALIDATION").upper()
    if mode not in MODES:
        raise ValueError("mode must be VALIDATION or OFFICIAL")
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
    return WeeklyScheduleDecision(
        event_name=event_name, mode=mode, intended_monday=monday.isoformat(),
        expected_schedule=expected, received_schedule=event_schedule, should_run=should_run,
        **resolve_completed_xnys_session(monday),
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
    args = parser.parse_args(argv)
    decision = decide_weekly_schedule(
        event_name=args.event_name, event_schedule=args.event_schedule or None,
        run_started_at=args.run_started_at, manual_monday=args.manual_monday,
        requested_mode=args.mode,
    )
    _write_github_output(decision)
    print(json.dumps(asdict(decision), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
