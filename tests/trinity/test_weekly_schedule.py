from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from trinity.shadow import cli as shadow_cli
from trinity.shadow.weekly_cycle import (
    WeeklyCycleError, check_cycle, completion_marker_key,
    validate_snapshot_handoff, write_official_completion_marker,
)
from trinity.shadow.weekly_schedule import (
    CONTRACT_VERSION, FINALITY_GRACE, SMOKE_TARGET_RESOLUTION,
    WEEKLY_TARGET_RESOLUTION, decide_weekly_schedule, resolve_completed_xnys_session,
    resolve_latest_final_xnys_session,
)


class Missing(Exception):
    response = {"ResponseMetadata": {"HTTPStatusCode": 404}, "Error": {"Code": "NoSuchKey"}}


class FakeR2:
    def __init__(self):
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}

    def head_object(self, *, Bucket, Key):
        del Bucket
        if Key not in self.objects:
            raise Missing()
        body, metadata = self.objects[Key]
        return {"ContentLength": len(body), "Metadata": metadata}

    def put_object(self, *, Bucket, Key, Body, Metadata, IfNoneMatch, **_kwargs):
        del Bucket
        assert IfNoneMatch == "*"
        if Key in self.objects:
            exc = RuntimeError("precondition")
            exc.response = {"ResponseMetadata": {"HTTPStatusCode": 412}}
            raise exc
        self.objects[Key] = (Body, Metadata)


def decision(day: datetime, cron: str, *, mode=None):
    return decide_weekly_schedule(
        event_name="schedule", event_schedule=cron, run_started_at=day,
        requested_mode=mode,
    )


def test_cest_valid_twin_and_delayed_runner_and_invalid_twin():
    delayed = datetime(2026, 7, 6, 18, tzinfo=timezone.utc)
    valid = decision(delayed, "0 10 * * 1")
    invalid = decision(delayed, "0 11 * * 1")
    assert valid.should_run is True and invalid.should_run is False
    assert valid.expected_schedule == "0 10 * * 1"
    assert valid.mode == "OFFICIAL"


def test_cet_valid_twin_after_tuesday_delay_and_invalid_twin():
    delayed = datetime(2026, 12, 8, 2, tzinfo=timezone.utc)
    valid = decision(delayed, "0 11 * * 1")
    invalid = decision(delayed, "0 10 * * 1")
    assert valid.should_run is True and invalid.should_run is False
    assert valid.intended_monday == "2026-12-07"
    assert valid.expected_schedule == "0 11 * * 1"


@pytest.mark.parametrize(("monday", "expected", "close"), [
    (date(2026, 10, 12), "2026-10-09", "20:00:00Z"),  # normal Monday
    (date(2026, 9, 7), "2026-09-04", "20:00:00Z"),   # US holiday Monday
    (date(2026, 7, 6), "2026-07-02", "20:00:00Z"),   # Friday holiday
    (date(2026, 11, 30), "2026-11-27", "18:00:00Z"), # early close
    (date(2027, 1, 4), "2026-12-31", "21:00:00Z"),   # year boundary
])
def test_established_xnys_session_resolution(monday, expected, close):
    result = resolve_completed_xnys_session(monday)
    assert result["target_market_session"] == expected
    assert result["target_session_close_utc"].endswith(close)
    assert result["session_calendar"] == "XNYS"
    assert result["session_calendar_version"] == "4.13.2"


def test_europe_us_dst_transition_difference_is_calendar_safe():
    result = decision(datetime(2026, 10, 26, 15, tzinfo=timezone.utc), "0 11 * * 1")
    assert result.expected_schedule == "0 11 * * 1"  # Rome is CET; New York still EDT.
    assert result.target_market_session == "2026-10-23"
    assert result.target_session_close_utc.endswith("20:00:00Z")
    assert result.target_session_resolution == WEEKLY_TARGET_RESOLUTION


def test_validation_full_smoke_uses_latest_final_session_not_intended_week():
    result = decide_weekly_schedule(
        event_name="workflow_dispatch", event_schedule=None,
        run_started_at=datetime(2026, 10, 23, 7, 42, tzinfo=timezone.utc),
        manual_monday=date(2026, 10, 19), requested_mode="VALIDATION",
        execution_scope="FULL_SMOKE",
    )
    assert result.intended_monday == "2026-10-19"
    assert result.target_market_session == "2026-10-22"
    assert result.target_session_open_utc == "2026-10-22T13:30:00Z"
    assert result.target_session_close_utc == "2026-10-22T20:00:00Z"
    assert result.target_session_resolution == SMOKE_TARGET_RESOLUTION
    assert result.execution_scope == "FULL_SMOKE"


def test_latest_final_session_obeys_exact_close_plus_twenty_boundary():
    before = resolve_latest_final_xnys_session(
        datetime(2026, 10, 23, 20, 19, 59, tzinfo=timezone.utc)
    )
    exact = resolve_latest_final_xnys_session(
        datetime(2026, 10, 23, 20, 20, tzinfo=timezone.utc)
    )
    after = resolve_latest_final_xnys_session(
        datetime(2026, 10, 23, 20, 20, 1, tzinfo=timezone.utc)
    )
    assert before["target_market_session"] == "2026-10-22"
    assert exact["target_market_session"] == "2026-10-23"
    assert after["target_market_session"] == "2026-10-23"
    assert FINALITY_GRACE == timedelta(minutes=20)


@pytest.mark.parametrize(("started", "expected", "close"), [
    (datetime(2026, 10, 25, 12, tzinfo=timezone.utc), "2026-10-23", "20:00:00Z"),
    (datetime(2026, 11, 26, 20, tzinfo=timezone.utc), "2026-11-25", "21:00:00Z"),
    (datetime(2026, 11, 27, 18, 19, 59, tzinfo=timezone.utc), "2026-11-25", "21:00:00Z"),
    (datetime(2026, 11, 27, 18, 20, tzinfo=timezone.utc), "2026-11-27", "18:00:00Z"),
    (datetime(2026, 10, 30, 20, 20, tzinfo=timezone.utc), "2026-10-30", "20:00:00Z"),
    (datetime(2026, 11, 2, 21, 20, tzinfo=timezone.utc), "2026-11-02", "21:00:00Z"),
    (datetime(2027, 1, 1, 12, tzinfo=timezone.utc), "2026-12-31", "21:00:00Z"),
])
def test_latest_final_session_calendar_edges(started, expected, close):
    result = resolve_latest_final_xnys_session(started)
    assert result["target_market_session"] == expected
    assert result["target_session_close_utc"].endswith(close)


def test_execution_scope_preserves_weekly_paths_and_rejects_unsafe_combinations():
    preflight = decide_weekly_schedule(
        event_name="workflow_dispatch", event_schedule=None,
        run_started_at=datetime(2026, 10, 23, 7, 42, tzinfo=timezone.utc),
        manual_monday=date(2026, 10, 19), requested_mode="VALIDATION",
        execution_scope="PREFLIGHT_ONLY",
    )
    official = decide_weekly_schedule(
        event_name="workflow_dispatch", event_schedule=None,
        run_started_at=datetime(2026, 10, 23, 7, 42, tzinfo=timezone.utc),
        manual_monday=date(2026, 10, 19), requested_mode="OFFICIAL",
        execution_scope="PREFLIGHT_ONLY",
    )
    scheduled = decide_weekly_schedule(
        event_name="schedule", event_schedule="0 10 * * 1",
        run_started_at=datetime(2026, 10, 19, 10, tzinfo=timezone.utc),
        manual_monday=date(2026, 10, 19), requested_mode="OFFICIAL",
    )
    assert preflight.target_market_session == "2026-10-16"
    assert official.target_market_session == "2026-10-16"
    assert scheduled.target_market_session == "2026-10-16"
    assert {
        preflight.target_session_resolution, official.target_session_resolution,
        scheduled.target_session_resolution,
    } == {WEEKLY_TARGET_RESOLUTION}
    with pytest.raises(ValueError, match="restricted to VALIDATION"):
        decide_weekly_schedule(
            event_name="workflow_dispatch", event_schedule=None,
            run_started_at=datetime(2026, 10, 23, 7, 42, tzinfo=timezone.utc),
            manual_monday=date(2026, 10, 19), requested_mode="OFFICIAL",
            execution_scope="FULL_SMOKE",
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        decide_weekly_schedule(
            event_name="workflow_dispatch", event_schedule=None,
            run_started_at=datetime(2026, 10, 23, 7, 42),
            manual_monday=date(2026, 10, 19), requested_mode="VALIDATION",
            execution_scope="FULL_SMOKE",
        )


def test_manual_modes_and_monday_validation():
    validation = decide_weekly_schedule(
        event_name="workflow_dispatch", event_schedule=None,
        run_started_at=datetime(2026, 10, 10, tzinfo=timezone.utc),
        manual_monday=date(2026, 10, 12), requested_mode="VALIDATION",
    )
    official = decide_weekly_schedule(
        event_name="workflow_dispatch", event_schedule=None,
        run_started_at=datetime(2026, 10, 10, tzinfo=timezone.utc),
        manual_monday=date(2026, 10, 12), requested_mode="OFFICIAL",
    )
    assert validation.mode == "VALIDATION" and official.mode == "OFFICIAL"
    with pytest.raises(ValueError, match="Monday"):
        decide_weekly_schedule(
            event_name="workflow_dispatch", event_schedule=None,
            run_started_at=datetime(2026, 10, 10, tzinfo=timezone.utc),
            manual_monday=date(2026, 10, 13), requested_mode="VALIDATION",
        )


def test_exact_same_run_snapshot_handoff_and_malformed_fail_closed():
    valid = validate_snapshot_handoff(
        target_session="2026-10-09", snapshot_id="TWELVEDATA_2026-10-09_RUN_123-A1",
        snapshot_sha256="a" * 64,
        prefix="prices/twelvedata/session=2026-10-09/run_id=123-A1/",
        github_run_id="123", github_run_attempt="1",
    )
    assert valid["price_snapshot_r2_prefix"].endswith("run_id=123-A1/")
    for changed in ({"snapshot_id": "old"}, {"snapshot_sha256": "bad"}, {"prefix": "prices/latest/"}):
        kwargs = {
            "target_session": "2026-10-09", "snapshot_id": "TWELVEDATA_2026-10-09_RUN_123-A1",
            "snapshot_sha256": "a" * 64,
            "prefix": "prices/twelvedata/session=2026-10-09/run_id=123-A1/",
            "github_run_id": "123", "github_run_attempt": "1",
        }
        kwargs.update(changed)
        with pytest.raises(WeeklyCycleError):
            validate_snapshot_handoff(**kwargs)


def test_official_duplicate_blocked_and_failed_prior_retry_allowed():
    client = FakeR2()
    args = {"mode": "OFFICIAL", "intended_monday": "2026-10-12", "target_session": "2026-10-09"}
    assert check_cycle(client, "bucket", **args)["eligible"] is True  # no success marker: retry allowed
    created = write_official_completion_marker(
        client, "bucket", **args, analytical_commit="a" * 40, workflow_commit="b" * 40,
        snapshot_id="snapshot", snapshot_sha256="c" * 64, snapshot_prefix="prices/immutable/",
        analytical_run_id="run", analytical_archive={"object_count": 10},
    )
    assert created["created"] is True
    assert check_cycle(client, "bucket", **args)["eligible"] is False
    with pytest.raises(WeeklyCycleError, match="already complete"):
        write_official_completion_marker(
            client, "bucket", **args, analytical_commit="a" * 40, workflow_commit="b" * 40,
            snapshot_id="snapshot", snapshot_sha256="c" * 64, snapshot_prefix="prices/immutable/",
            analytical_run_id="run", analytical_archive={},
        )


def test_validation_never_creates_official_marker_or_benchmark_observation(monkeypatch):
    client = FakeR2()
    result = write_official_completion_marker(
        client, "bucket", mode="VALIDATION", intended_monday="2026-10-12",
        target_session="2026-10-09", analytical_commit="a", workflow_commit="b",
        snapshot_id="snapshot", snapshot_sha256="c" * 64, snapshot_prefix="prefix",
        analytical_run_id="run", analytical_archive={},
    )
    assert result == {"created": False, "reason": "VALIDATION_MODE"}
    assert client.objects == {}
    values = {
        "TRINITY_ORCHESTRATOR_MODE": "VALIDATION",
        "TRINITY_ORCHESTRATOR_METHOD": CONTRACT_VERSION,
        "TRINITY_INTENDED_MONDAY": "2026-10-12",
        "TRINITY_TARGET_MARKET_SESSION": "2026-10-09",
        "TRINITY_TARGET_SESSION_OPEN_UTC": "2026-10-09T13:30:00Z",
        "TRINITY_TARGET_SESSION_CLOSE_UTC": "2026-10-09T20:00:00Z",
        "TRINITY_TARGET_SESSION_RESOLUTION": WEEKLY_TARGET_RESOLUTION,
        "TRINITY_SESSION_CALENDAR": "XNYS",
        "TRINITY_SESSION_CALENDAR_VERSION": "4.13.2",
        "TRINITY_ANALYTICAL_GIT_SHA": "a" * 40,
        "TRINITY_WORKFLOW_GIT_SHA": "b" * 40,
    }
    for key, value in values.items(): monkeypatch.setenv(key, value)
    provenance = shadow_cli._weekly_orchestrator_provenance()
    assert provenance["official_benchmark_observation"] is False
    assert provenance["target_session_resolution"] == WEEKLY_TARGET_RESOLUTION
    assert provenance["analytical_commit"] != provenance["workflow_registration_commit"]


def test_analytical_git_head_ignores_main_workflow_sha(monkeypatch):
    monkeypatch.setenv("TRINITY_ANALYTICAL_GIT_SHA", "a" * 40)
    monkeypatch.setenv("GITHUB_SHA", "b" * 40)
    assert shadow_cli._git_head() == "a" * 40


def test_workflow_contract_is_fail_closed_and_secret_scoped():
    root = Path(__file__).resolve().parents[2]
    workflows = root / ".github" / "workflows"
    weekly = (workflows / "trinity-weekly-production.yml").read_text(encoding="utf-8")
    price = (workflows / "trinity-price-refresh.yml").read_text(encoding="utf-8")
    shadow = (workflows / "trinity-shadow-manual.yml").read_text(encoding="utf-8")
    assert "\n  schedule:\n" not in weekly
    assert "cron:" not in weekly
    assert "          - OFFICIAL" not in weekly
    assert "concurrency:\n  group: trinity-weekly-production-v1" in weekly
    assert "schedule:" not in price and "schedule:" not in shadow
    gate = weekly.split("  schedule-gate:", 1)[1].split("  infrastructure-preflight:", 1)[0]
    assert not any(secret in gate for secret in ("TWELVEDATA_API_KEY", "EODHD_API_KEY", "OPENAI_API_KEY"))
    price_job = weekly.split("  price-snapshot:", 1)[1].split("  analytical-production:", 1)[0]
    assert "TWELVEDATA_API_KEY" in price_job
    assert "OPENAI_API_KEY" not in price_job and "EODHD_API_KEY" not in price_job
    analytical = weekly.split("  analytical-production:", 1)[1]
    assert "TWELVEDATA_API_KEY" not in analytical
    assert "OPENAI_API_KEY" in analytical and "EODHD_API_KEY" in analytical
    assert analytical.index("validate-handoff") < analytical.index("cli restore-prices") < analytical.index("cli run")
    assert analytical.index("cli archive") < analytical.index("weekly_cycle complete") < analytical.index("notifications.telegram")
    assert "continue-on-error: true" in analytical
    analytical_sha = "f2922598321a72fc1c3f500f14570ed54e04f433"
    assert f"TRINITY_ANALYTICAL_REF: {analytical_sha}" in weekly
    assert f"TRINITY_ANALYTICAL_REF: {analytical_sha}" in price
    assert "TRINITY_ANALYTICAL_REF: trinity-live-validation-v1" not in weekly + price
    checkout_action = "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683"
    assert weekly.count(checkout_action) == weekly.count("persist-credentials: false") == 5
    assert price.count(checkout_action) == price.count("persist-credentials: false") == 1
    assert weekly.count("ref: ${{ env.TRINITY_ANALYTICAL_REF }}") == 5
    assert price.count("ref: ${{ env.TRINITY_ANALYTICAL_REF }}") == 1
    assert "TRINITY_WORKFLOW_GIT_SHA: ${{ github.sha }}" in weekly
    assert "needs.price-snapshot.outputs.price_snapshot_r2_prefix" in analytical
    assert "needs.price-snapshot.outputs.price_snapshot_sha256" in analytical
    assert "needs.price-snapshot.result == 'success'" in analytical
    for output in ("target_market_session", "price_snapshot_id", "price_snapshot_sha256",
                   "price_snapshot_r2_prefix"):
        assert f"{output}: ${{{{ steps.snapshot.outputs.{output} }}}}" in weekly
    assert "broker_execution\": False" in (root / "trinity" / "shadow" / "cli.py").read_text(encoding="utf-8")


def test_workflow_live_preflight_is_default_and_blocks_full_smoke():
    root = Path(__file__).resolve().parents[2]
    weekly = (root / ".github" / "workflows" / "trinity-weekly-production.yml").read_text(
        encoding="utf-8"
    )
    dispatch = weekly.split("jobs:", 1)[0]
    assert "execution_scope:" in dispatch
    assert "default: PREFLIGHT_ONLY" in dispatch
    assert dispatch.count("- PREFLIGHT_ONLY") == 1
    assert dispatch.count("- FULL_SMOKE") == 1
    assert "- OFFICIAL" not in dispatch
    assert "schedule:" not in dispatch and "cron:" not in dispatch

    preflight = weekly.split("  infrastructure-preflight:", 1)[1].split("  cycle-guard:", 1)[0]
    for name in (
        "TRINITY_R2_ENDPOINT", "TRINITY_R2_ACCESS_KEY_ID",
        "TRINITY_R2_SECRET_ACCESS_KEY", "TRINITY_BASELINE_PREFIX",
        "TWELVEDATA_API_KEY", "OPENAI_API_KEY", "EODHD_API_KEY",
    ):
        assert name in preflight
    assert preflight.index("pip install") < preflight.index("TWELVEDATA_API_KEY")
    for output in (
        "config_ready", "r2_ready", "baseline_ready", "twelvedata_quota_ready",
        "required_daily_credits", "available_daily_credits", "analytical_sha",
    ):
        assert f"{output}: ${{{{ steps.preflight.outputs.{output} }}}}" in preflight

    cycle = weekly.split("  cycle-guard:", 1)[1].split("  price-snapshot:", 1)[0]
    price = weekly.split("  price-snapshot:", 1)[1].split("  analytical-production:", 1)[0]
    assert "inputs.execution_scope == 'FULL_SMOKE'" in cycle
    assert 'args+=(--execution-scope "$EXECUTION_SCOPE")' in weekly
    assert "target_session_resolution: ${{ steps.gate.outputs.target_session_resolution }}" in weekly
    assert "TRINITY_TARGET_SESSION_RESOLUTION:" in weekly
    assert "needs.infrastructure-preflight.result == 'success'" in cycle
    assert "inputs.execution_scope == 'FULL_SMOKE'" in price
    assert "needs.infrastructure-preflight.outputs.config_ready == 'true'" in price
    assert "needs.infrastructure-preflight.outputs.r2_ready == 'true'" in price
    assert "needs.infrastructure-preflight.outputs.baseline_ready == 'true'" in price
    assert "needs.infrastructure-preflight.outputs.twelvedata_quota_ready == 'true'" in price
    assert (
        "needs.infrastructure-preflight.outputs.analytical_sha == "
        "'f2922598321a72fc1c3f500f14570ed54e04f433'"
    ) in price

    analytical = weekly.split("  analytical-production:", 1)[1]
    telegram = analytical.split(
        "- name: Deliver committed post-archive Telegram notifications", 1
    )[1].split("- name: Preserve analytical diagnostics", 1)[0]
    assert "if: needs.schedule-gate.outputs.mode == 'OFFICIAL'" in telegram
    assert "- OFFICIAL" not in dispatch


def test_price_credit_or_archive_failure_cannot_start_analytics():
    workflow = (Path(__file__).resolve().parents[2] / ".github" / "workflows" /
                "trinity-weekly-production.yml").read_text(encoding="utf-8")
    price_job = workflow.split("  price-snapshot:", 1)[1].split("  analytical-production:", 1)[0]
    analytical = workflow.split("  analytical-production:", 1)[1]
    assert price_job.index("price_refresh acquire") < price_job.index("price_refresh archive")
    assert price_job.index("price_refresh archive") < price_job.index("id: snapshot")
    assert "needs.price-snapshot.result == 'success'" in analytical


def test_live_research_cutoff_contract_remains_wired_once_before_models():
    source = (Path(__file__).resolve().parents[2] / "trinity" / "shadow" / "cli.py").read_text(encoding="utf-8")
    assert source.count("capture_live_research_cutoff(") == 1
    assert "research_cutoff_utc=research_cutoff_dt()" in source
    assert '"broker_execution": False' in source


def test_marker_namespace_contains_full_official_identity():
    key = completion_marker_key("2026-10-12", "2026-10-09")
    assert key == (
        "orchestration/weekly/method=TRINITY_WEEKLY_ORCHESTRATOR_V1/"
        "intended_monday=2026-10-12/target_session=2026-10-09/official-complete.json"
    )
