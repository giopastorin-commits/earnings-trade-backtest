from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path

import pytest

from trinity.shadow import cli as shadow_cli
from trinity.shadow.weekly_cycle import (
    WeeklyCycleError, check_cycle, completion_marker_key,
    validate_snapshot_handoff, write_official_completion_marker,
)
from trinity.shadow.weekly_schedule import (
    CONTRACT_VERSION, decide_weekly_schedule, resolve_completed_xnys_session,
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
        "TRINITY_SESSION_CALENDAR": "XNYS",
        "TRINITY_SESSION_CALENDAR_VERSION": "4.13.2",
        "TRINITY_ANALYTICAL_GIT_SHA": "a" * 40,
        "TRINITY_WORKFLOW_GIT_SHA": "b" * 40,
    }
    for key, value in values.items(): monkeypatch.setenv(key, value)
    provenance = shadow_cli._weekly_orchestrator_provenance()
    assert provenance["official_benchmark_observation"] is False
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
    assert weekly.count('cron: "0 10 * * 1"') == 1
    assert weekly.count('cron: "0 11 * * 1"') == 1
    assert "concurrency:\n  group: trinity-weekly-production-v1" in weekly
    assert "schedule:" not in price and "schedule:" not in shadow
    gate = weekly.split("  schedule-gate:", 1)[1].split("  cycle-guard:", 1)[0]
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
    assert "ref: ${{ env.TRINITY_ANALYTICAL_REF }}" in weekly
    assert "TRINITY_WORKFLOW_GIT_SHA: ${{ github.sha }}" in weekly
    assert "needs.price-snapshot.outputs.price_snapshot_r2_prefix" in analytical
    assert "needs.price-snapshot.outputs.price_snapshot_sha256" in analytical
    assert "needs.price-snapshot.result == 'success'" in analytical
    for output in ("target_market_session", "price_snapshot_id", "price_snapshot_sha256",
                   "price_snapshot_r2_prefix"):
        assert f"{output}: ${{{{ steps.snapshot.outputs.{output} }}}}" in weekly
    assert "broker_execution\": False" in (root / "trinity" / "shadow" / "cli.py").read_text(encoding="utf-8")


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
