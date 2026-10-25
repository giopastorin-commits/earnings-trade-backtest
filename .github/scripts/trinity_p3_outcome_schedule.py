"""DST-safe, post-production schedule gate for prospective outcome collection."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from typing import Sequence
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path.cwd()))

from trinity.validation.observation_slots import weekly_observation_slot


ROME = ZoneInfo("Europe/Rome")
SCHEDULES = frozenset(
    f"0 {hour} * * {weekday}" for hour in (4, 5) for weekday in range(2, 7)
)
METHOD = "TRINITY_P3_AUTOMATIC_OUTCOMES_V1"


@dataclass(frozen=True)
class OutcomeScheduleDecision:
    should_run: bool
    observation_slot_id: str
    iso_year: int
    iso_week: int
    target_market_session: str
    finality_eligible_at_utc: str
    expected_schedule: str
    received_schedule: str
    method_version: str = METHOD


def _parse_schedule(value: str) -> tuple[int, int]:
    if value not in SCHEDULES:
        raise ValueError("unrecognized P3 scheduled cron expression")
    minute, hour, day_of_month, month, weekday = value.split()
    if (minute, day_of_month, month) != ("0", "*", "*"):
        raise ValueError("unrecognized P3 scheduled cron expression")
    return int(hour), int(weekday)


def _scheduled_local_date(event_schedule: str, run_started_at: datetime) -> date:
    _hour, weekday = _parse_schedule(event_schedule)
    local_started = run_started_at.astimezone(ROME)
    monday = local_started.date() - timedelta(days=local_started.date().weekday())
    candidate = monday + timedelta(days=weekday - 1)
    intended = datetime.combine(candidate, time(6), ROME)
    if intended - local_started > timedelta(hours=6):
        candidate -= timedelta(days=7)
    return candidate


def decide_outcome_schedule(
    *, event_schedule: str, run_started_at: datetime,
) -> OutcomeScheduleDecision:
    """Run at 06:00 Rome on the day after the frozen target session only."""

    if run_started_at.tzinfo is None or run_started_at.utcoffset() is None:
        raise ValueError("run_started_at must be timezone-aware")
    scheduled_date = _scheduled_local_date(event_schedule, run_started_at)
    candidate_target = scheduled_date - timedelta(days=1)
    iso_year, iso_week, _ = candidate_target.isocalendar()
    slot = weekly_observation_slot(iso_year, iso_week)
    actual_target = date.fromisoformat(slot.slot_target_session)
    intended = datetime.combine(scheduled_date, time(6), ROME)
    expected = f"0 {intended.astimezone(timezone.utc).hour} * * {scheduled_date.isoweekday()}"
    if expected not in SCHEDULES:
        raise ValueError("Europe/Rome 06:00 resolved outside registered schedules")
    delay = run_started_at.astimezone(timezone.utc) - intended.astimezone(timezone.utc)
    finality = datetime.fromisoformat(
        slot.finality_eligible_at_utc.replace("Z", "+00:00")
    )
    should_run = (
        actual_target == candidate_target
        and event_schedule == expected
        and run_started_at.astimezone(timezone.utc) >= finality
        and timedelta(0) <= delay <= timedelta(hours=48)
    )
    return OutcomeScheduleDecision(
        should_run=should_run,
        observation_slot_id=slot.observation_slot_id,
        iso_year=slot.iso_year,
        iso_week=slot.iso_week,
        target_market_session=slot.slot_target_session,
        finality_eligible_at_utc=slot.finality_eligible_at_utc,
        expected_schedule=expected,
        received_schedule=event_schedule,
    )


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("run_started_at must include a timezone")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-schedule", required=True)
    parser.add_argument("--run-started-at", required=True, type=_parse_timestamp)
    args = parser.parse_args(argv)
    decision = decide_outcome_schedule(
        event_schedule=args.event_schedule, run_started_at=args.run_started_at,
    )
    output = os.getenv("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as stream:
            for key, value in asdict(decision).items():
                stream.write(f"{key}={str(value).lower() if isinstance(value, bool) else value}\n")
    print(json.dumps(asdict(decision), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
