from __future__ import annotations

import json
from pathlib import Path

import pytest

from experiments.sol_effort_v1.client import (
    LiveShadowDisabled,
    MockResponsesClient,
    OpenAIResponsesClient,
)
from experiments.sol_effort_v1.dataset import DatasetError, load_dataset, select_records
from experiments.sol_effort_v1.runner import build_call_plan, run_experiment
from experiments.sol_effort_v1.schema import ResponseValidationError
from experiments.sol_effort_v1.scorecard import build_scorecard


FIXTURE = Path(__file__).parents[1] / "fixtures" / "sample_dataset.json"


def test_factorial_plan_is_six_calls_per_record() -> None:
    dataset = load_dataset(FIXTURE)
    records = select_records(dataset, 3)
    plan = build_call_plan(records)
    assert len(plan) == 18
    for record in records:
        calls = [item for item in plan if item["record_id"] == record.record_id]
        assert len(calls) == 6
        assert sum(item["role"] == "ANALYST" for item in calls) == 2
        assert sum(item["role"] == "CRITIC" for item in calls) == 4
        assert all(item["depends_on"] for item in calls if item["role"] == "CRITIC")


def test_mock_reuses_analyst_draft_exactly(tmp_path: Path) -> None:
    dataset = load_dataset(FIXTURE)
    records = select_records(dataset, 3)
    client = MockResponsesClient()
    result = run_experiment(
        records=records,
        dataset=dataset,
        mode="mock",
        output_dir=tmp_path,
        client=client,
    )
    assert result["status"] == "COMPLETE"
    assert len(result["calls"]) == 18
    for record in records:
        for effort in ("medium", "high"):
            critics = [
                item for item in result["calls"]
                if item["record_id"] == record.record_id
                and item["role"] == "CRITIC"
                and item["analyst_effort"] == effort
            ]
            analyst = next(
                item for item in result["calls"]
                if item["record_id"] == record.record_id
                and item["role"] == "ANALYST"
                and item["effort"] == effort
            )
            assert len(critics) == 2
            assert len({item["analyst_output_hash"] for item in critics}) == 1
            assert len({item["prompt_hash"] for item in critics}) == 1
            assert all(analyst["output_text"] in item["prompt"] for item in critics)


def test_manifest_and_mock_results_are_reproducible(tmp_path: Path) -> None:
    dataset = load_dataset(FIXTURE)
    records = select_records(dataset, 3)
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    run_experiment(
        records=records,
        dataset=dataset,
        mode="mock",
        output_dir=first_dir,
        client=MockResponsesClient(),
    )
    run_experiment(
        records=records,
        dataset=dataset,
        mode="mock",
        output_dir=second_dir,
        client=MockResponsesClient(),
    )
    assert (first_dir / "manifest.json").read_bytes() == (second_dir / "manifest.json").read_bytes()
    assert (first_dir / "results.json").read_bytes() == (second_dir / "results.json").read_bytes()


def test_dry_run_makes_no_client_calls(tmp_path: Path) -> None:
    dataset = load_dataset(FIXTURE)
    records = select_records(dataset, 3)
    result = run_experiment(
        records=records,
        dataset=dataset,
        mode="dry-run",
        output_dir=tmp_path,
    )
    assert result["status"] == "PLANNED"
    assert len(json.loads((tmp_path / "call_plan.json").read_text()) ) == 18
    assert not (tmp_path / "results.json").read_text().count("response_id")


def test_future_source_is_rejected(tmp_path: Path) -> None:
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    value["records"][0]["sources"][0]["published_at"] = "2026-10-20T10:00:00Z"
    path = tmp_path / "future.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(DatasetError, match="future source"):
        load_dataset(path)


def test_live_shadow_requires_explicit_authorization() -> None:
    with pytest.raises(LiveShadowDisabled):
        OpenAIResponsesClient(
            max_budget_usd=1,
            authorization="",
            api_key="deliberately-not-used",
        )


def test_schema_failure_is_recorded_and_does_not_promote(tmp_path: Path) -> None:
    dataset = load_dataset(FIXTURE)
    records = select_records(dataset, 1)

    class BrokenClient(MockResponsesClient):
        def invoke(self, **kwargs):
            if kwargs["role"] == "ANALYST":
                raise ResponseValidationError("synthetic invalid response")
            return super().invoke(**kwargs)

    result = run_experiment(
        records=records,
        dataset=dataset,
        mode="mock",
        output_dir=tmp_path,
        client=BrokenClient(),
    )
    assert result["status"] == "PARTIAL"
    assert any(item["error_type"] == "SCHEMA" for item in result["calls"])
    assert all(item["status"] == "INCOMPLETE" for item in result["arms"])


def test_financial_outcome_is_not_counted_four_times() -> None:
    outcome = {
        "D20": {
            "status": "COMPLETE",
            "stock_return_pct": 3.0,
            "spy_return_pct": 1.0,
        }
    }
    scorecard = build_scorecard({
        "calls": [],
        "arms": [
            {"record_id": "R1", "arm_id": arm, "outcomes": outcome}
            for arm in ("A", "B", "C", "D")
        ],
    })
    assert scorecard["financial_outcomes"]["horizons"]["D20"]["n"] == 1

