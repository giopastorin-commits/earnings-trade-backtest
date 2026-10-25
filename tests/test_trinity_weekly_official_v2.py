from __future__ import annotations

from datetime import date, datetime, timezone
import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCHEDULE_PATH = ROOT / ".github" / "scripts" / "trinity_weekly_schedule_v2.py"
SPEC = importlib.util.spec_from_file_location("trinity_weekly_schedule_v2", SCHEDULE_PATH)
assert SPEC and SPEC.loader
schedule = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = schedule
SPEC.loader.exec_module(schedule)


def scheduled(started: datetime, cron: str):
    return schedule.decide_schedule(
        event_name="schedule", event_schedule=cron, run_started_at=started,
    )


def test_normal_monday_targets_that_completed_monday_session():
    result = scheduled(datetime(2026, 7, 6, 21, 0, tzinfo=timezone.utc), "0 21 * * 1")
    assert result.should_run is True
    assert result.mode == "OFFICIAL"
    assert result.target_market_session == "2026-07-06"
    assert result.target_session_close_utc == "2026-07-06T20:00:00Z"
    assert result.target_session_resolution == schedule.OFFICIAL_TARGET_RESOLUTION


@pytest.mark.parametrize(
    ("started", "valid_cron", "invalid_cron", "close_utc"),
    [
        # U.S. DST has started; Europe is still on standard time.
        (datetime(2026, 3, 16, 22, tzinfo=timezone.utc), "0 22 * * 1", "0 21 * * 1", "20:00:00Z"),
        # Europe has left DST; the U.S. is still on daylight time.
        (datetime(2026, 10, 26, 22, tzinfo=timezone.utc), "0 22 * * 1", "0 21 * * 1", "20:00:00Z"),
    ],
)
def test_europe_us_dst_mismatch_selects_only_rome_2300_twin(
    started, valid_cron, invalid_cron, close_utc,
):
    valid = scheduled(started, valid_cron)
    invalid = scheduled(started, invalid_cron)
    assert valid.should_run is True and invalid.should_run is False
    assert valid.expected_schedule == valid_cron
    assert valid.target_market_session == started.date().isoformat()
    assert valid.target_session_close_utc.endswith(close_utc)


def test_monday_market_holiday_uses_latest_session_final_at_decision_instant():
    result = scheduled(datetime(2026, 9, 7, 21, tzinfo=timezone.utc), "0 21 * * 1")
    assert result.target_market_session == "2026-09-04"
    assert result.target_session_close_utc == "2026-09-04T20:00:00Z"


def test_monday_early_close_is_calendar_derived_and_final():
    result = scheduled(datetime(2028, 7, 3, 21, tzinfo=timezone.utc), "0 21 * * 1")
    assert result.target_market_session == "2028-07-03"
    assert result.target_session_close_utc == "2028-07-03T17:00:00Z"


def test_latest_final_resolution_obeys_close_plus_twenty_minute_boundary():
    before = schedule.resolve_latest_final_xnys_session(
        datetime(2026, 10, 19, 20, 19, 59, tzinfo=timezone.utc)
    )
    exact = schedule.resolve_latest_final_xnys_session(
        datetime(2026, 10, 19, 20, 20, tzinfo=timezone.utc)
    )
    assert before["target_market_session"] == "2026-10-16"
    assert exact["target_market_session"] == "2026-10-19"


def test_runner_delay_cannot_advance_the_official_target():
    delayed = scheduled(datetime(2026, 10, 27, 20, tzinfo=timezone.utc), "0 22 * * 1")
    assert delayed.intended_monday == "2026-10-26"
    assert delayed.target_market_session == "2026-10-26"


def test_manual_validation_full_smoke_behavior_remains_available():
    result = schedule.decide_schedule(
        event_name="workflow_dispatch", event_schedule=None,
        run_started_at=datetime(2026, 10, 23, 7, 42, tzinfo=timezone.utc),
        manual_monday=date(2026, 10, 19), requested_mode="VALIDATION",
        execution_scope="FULL_SMOKE",
    )
    assert result.mode == "VALIDATION"
    assert result.target_market_session == "2026-10-22"
    assert result.target_session_resolution == schedule.VALIDATION_SMOKE_TARGET_RESOLUTION
    assert result.method_version == "TRINITY_WEEKLY_ORCHESTRATOR_V1"
    with pytest.raises(ValueError, match="restricted to VALIDATION"):
        schedule.decide_schedule(
            event_name="workflow_dispatch", event_schedule=None,
            run_started_at=datetime(2026, 10, 23, 7, 42, tzinfo=timezone.utc),
            manual_monday=date(2026, 10, 19), requested_mode="OFFICIAL",
            execution_scope="FULL_SMOKE",
        )


def test_workflow_fail_closed_ordering_and_side_effect_guards():
    workflow = (ROOT / ".github" / "workflows" / "trinity-weekly-production.yml").read_text(
        encoding="utf-8"
    )
    dispatch = workflow.split("jobs:", 1)[0]
    cycle = workflow.split("  cycle-guard:", 1)[1].split("  price-snapshot:", 1)[0]
    price = workflow.split("  price-snapshot:", 1)[1].split("  analytical-production:", 1)[0]
    analytical = workflow.split("  analytical-production:", 1)[1]
    telegram = analytical.split(
        "- name: Deliver committed post-archive Telegram notifications", 1
    )[1].split("- name: Preserve analytical diagnostics", 1)[0]

    assert "TRINITY_ANALYTICAL_REF: eb15c45b041b8890194d2f98b025aa98ed7fa527" in workflow
    assert "TRINITY_ORCHESTRATOR_METHOD: TRINITY_WEEKLY_ORCHESTRATOR_V2" in workflow
    assert dispatch.count("- VALIDATION") == 1 and "- OFFICIAL" not in dispatch
    assert "inputs.execution_scope == 'FULL_SMOKE'" in cycle
    assert "github.event_name == 'schedule'" in cycle
    assert "trinity_weekly_cycle_v2.py" in cycle and "args=(check" in cycle
    assert "needs.cycle-guard.outputs.eligible == 'true'" in price
    assert "if: needs.price-snapshot.result == 'success'" in analytical
    assert analytical.index("cli archive") < analytical.index("args=(complete")
    assert analytical.index("args=(complete") < analytical.index(
        "notifications.telegram"
    )
    assert "if: needs.schedule-gate.outputs.mode == 'OFFICIAL'" in telegram
    assert "broker" not in workflow.lower()


def test_v2_registration_documents_new_marker_namespace_without_rewriting_v1():
    wrapper = (ROOT / ".github" / "scripts" / "trinity_weekly_cycle_v2.py").read_text(
        encoding="utf-8"
    )
    amendment = (ROOT / "docs" / "TRINITY_WEEKLY_ORCHESTRATOR_V2.md").read_text(
        encoding="utf-8"
    )
    assert 'CONTRACT_VERSION = "TRINITY_WEEKLY_ORCHESTRATOR_V2"' in wrapper
    assert "method=TRINITY_WEEKLY_ORCHESTRATOR_V2" in amendment
    assert "not a new live-validation analytical epoch" in " ".join(amendment.split())
