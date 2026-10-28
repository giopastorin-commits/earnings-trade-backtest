from __future__ import annotations

import json
import os
import sqlite3
from decimal import Decimal
import subprocess
import sys
from pathlib import Path

import pytest

from experiments.sol_effort_v1.client import (
    LiveShadowDisabled,
    MockResponsesClient,
    OpenAIResponsesClient,
    ProviderCallError,
    calculate_cost,
)
from experiments.sol_effort_v1.adjudication import build_adjudication_dataset
from experiments.sol_effort_v1.archive import _json_payload, _official_request
from experiments.sol_effort_v1.canonical import sha256_bytes
from experiments.sol_effort_v1.journal import BudgetExceeded, CallJournal, JournalBusy, JournalError
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
    first_manifest = json.loads((first_dir / "manifest.json").read_text())
    second_manifest = json.loads((second_dir / "manifest.json").read_text())
    assert first_manifest["dataset_identity"] == second_manifest["dataset_identity"]
    assert first_manifest["planned_calls"] == second_manifest["planned_calls"]
    assert first_manifest["execution_timestamp_utc"] != second_manifest["execution_timestamp_utc"]
    first_manifest.pop("execution_timestamp_utc")
    second_manifest.pop("execution_timestamp_utc")
    first_manifest.pop("manifest_hash")
    second_manifest.pop("manifest_hash")
    assert first_manifest == second_manifest


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
    assert sum(item["status"] == "SKIPPED_DEPENDENCY" for item in result["calls"]) == 4
    assert len(result["calls"]) == result["planned_calls"]
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


def test_mock_reasoning_tokens_are_billable() -> None:
    client = MockResponsesClient()
    schema = {"type": "object", "required": ["x"], "properties": {"x": {"type": "string"}}}
    medium = client.invoke(call_id="m", role="ANALYST", ticker="X", effort="medium", prompt="p", schema=schema)
    high = client.invoke(call_id="h", role="ANALYST", ticker="X", effort="high", prompt="p", schema=schema)
    assert medium.output_tokens == medium.visible_output_tokens + medium.reasoning_tokens
    assert high.output_tokens == high.visible_output_tokens + high.reasoning_tokens
    assert medium.total_tokens == medium.input_tokens + medium.output_tokens
    assert high.total_tokens == high.input_tokens + high.output_tokens
    assert Decimal(high.cost_usd) > 0
    assert Decimal(high.cost_usd) == calculate_cost(high.input_tokens, 0, 0, high.output_tokens)


class _Response:
    def __init__(self, payload: bytes, status_code: int = 200):
        self.content = payload
        self.status_code = status_code


class _Session:
    def __init__(self, response):
        self.response = response
        self.calls = 0
        self.last_kwargs = None

    def post(self, *args, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        return self.response


def _live_client(payload: dict, tmp_path: Path) -> tuple[OpenAIResponsesClient, CallJournal, str]:
    raw = json.dumps(payload).encode()
    journal = CallJournal(tmp_path / "journal")
    client = OpenAIResponsesClient(
        max_budget_usd=Decimal("1"), authorization="I_UNDERSTAND_LIVE_SHADOW_IS_PAID_AND_ISOLATED",
        api_key="test-only", session=_Session(_Response(raw)),
    )
    call_id = "call-http"
    journal.start(call_id=call_id, request_hash="request", metadata={})
    return client, journal, call_id


def test_paid_invalid_schema_response_is_accounted_before_failure(tmp_path: Path) -> None:
    payload = {
        "id": "r1", "status": "completed", "output": [{"content": [{"type": "output_text", "text": "{}"}]}],
        "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
    }
    client, journal, call_id = _live_client(payload, tmp_path)
    captured = []
    schema = {"type": "object", "required": ["x"], "properties": {"x": {"type": "string"}}}
    with pytest.raises(ProviderCallError) as exc:
        client.invoke(call_id=call_id, role="ANALYST", ticker="X", effort="high", prompt="p", schema=schema,
                      checkpoint=lambda **kwargs: (captured.append(kwargs), journal.record_http_response(**kwargs)))
    assert exc.value.error_type == "SCHEMA"
    assert captured and journal.consumed_cost() > 0
    assert journal.load(call_id)["raw_sha256"] == sha256_bytes(json.dumps(payload).encode())


def test_incomplete_response_with_usage_is_not_a_financial_result(tmp_path: Path) -> None:
    payload = {"id": "r1", "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
               "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}}
    client, journal, call_id = _live_client(payload, tmp_path)
    with pytest.raises(ProviderCallError) as exc:
        client.invoke(call_id=call_id, role="ANALYST", ticker="X", effort="high", prompt="p", schema={"type": "object"},
                      checkpoint=journal.record_http_response)
    assert exc.value.error_type == "INCOMPLETE"
    assert journal.consumed_cost() > 0


def test_raw_checkpoint_precedes_envelope_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    import experiments.sol_effort_v1.client as client_module
    payload = {"id": "r1", "status": "completed", "output": [{"content": [{"type": "output_text", "text": "{}"}]}],
               "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
    session = _Session(_Response(json.dumps(payload).encode()))
    client = OpenAIResponsesClient(
        max_budget_usd=Decimal("1"), authorization="I_UNDERSTAND_LIVE_SHADOW_IS_PAID_AND_ISOLATED",
        api_key="test-only", session=session,
    )
    events: list[str] = []
    original_loads = client_module.json.loads

    def traced_loads(value):
        events.append("parse")
        return original_loads(value)

    monkeypatch.setattr(client_module.json, "loads", traced_loads)
    client.invoke(call_id="order", role="ANALYST", ticker="X", effort="high", prompt="p", schema={"type": "object"},
                  checkpoint=lambda **kwargs: events.append("checkpoint"))
    assert events[0] == "checkpoint"
    assert "parse" in events


def test_unknown_cost_is_not_recorded_as_zero(tmp_path: Path) -> None:
    journal = CallJournal(tmp_path / "journal")
    journal.start(call_id="unknown", request_hash="h", metadata={}, reservation_usd=Decimal("0.08"))
    session = _Session(_Response(b"not-json"))
    client = OpenAIResponsesClient(
        max_budget_usd=Decimal("0.10"), authorization="I_UNDERSTAND_LIVE_SHADOW_IS_PAID_AND_ISOLATED",
        api_key="test-only", session=session, journal=journal,
    )
    with pytest.raises(ProviderCallError) as exc:
        client.invoke(call_id="unknown", role="ANALYST", ticker="X", effort="high", prompt="p", schema={"type": "object"},
                      checkpoint=journal.record_http_response)
    assert exc.value.accounting["cost_usd"] is None
    assert journal.consumed_cost() == 0
    assert journal.reserved_cost() == Decimal("0.08")


def test_http_200_invalid_json_is_checkpointed_and_blocks_retry(tmp_path: Path) -> None:
    journal = CallJournal(tmp_path / "journal")
    journal.start(call_id="bad-json", request_hash="h", metadata={})
    session = _Session(_Response(b"not-json"))
    client = OpenAIResponsesClient(
        max_budget_usd=Decimal("1"), authorization="I_UNDERSTAND_LIVE_SHADOW_IS_PAID_AND_ISOLATED",
        api_key="test-only", session=session,
    )
    with pytest.raises(ProviderCallError) as exc:
        client.invoke(call_id="bad-json", role="ANALYST", ticker="X", effort="high", prompt="p", schema={"type": "object"},
                      checkpoint=journal.record_http_response)
    assert exc.value.call_status == "INDETERMINATE"
    assert journal.load("bad-json")["raw_sha256"] == sha256_bytes(b"not-json")
    assert journal.consumed_cost() == 0


def test_budget_uses_persisted_consumption_and_effort_is_sent(tmp_path: Path) -> None:
    journal = CallJournal(tmp_path / "journal")
    journal.start(call_id="spent", request_hash="h", metadata={})
    journal.record_http_response(call_id="spent", raw=b"{}", accounting={"cost_usd": "1.00"})
    session = _Session(_Response(json.dumps({"id": "x", "status": "completed", "output": [], "usage": {}}).encode()))
    client = OpenAIResponsesClient(
        max_budget_usd=Decimal("1"), authorization="I_UNDERSTAND_LIVE_SHADOW_IS_PAID_AND_ISOLATED",
        api_key="test-only", session=session, journal=journal,
    )
    with pytest.raises(LiveShadowDisabled, match="budget"):
        client.invoke(call_id="new", role="ANALYST", ticker="X", effort="high", prompt="p", schema={"type": "object"})
    assert session.calls == 0


def test_responses_body_contains_explicit_reasoning_effort(tmp_path: Path) -> None:
    payload = {"id": "r1", "status": "completed", "output": [{"content": [{"type": "output_text", "text": "{}"}]}],
               "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
    session = _Session(_Response(json.dumps(payload).encode()))
    client = OpenAIResponsesClient(
        max_budget_usd=Decimal("1"), authorization="I_UNDERSTAND_LIVE_SHADOW_IS_PAID_AND_ISOLATED",
        api_key="test-only", session=session, max_output_tokens=512,
    )
    client.invoke(call_id="body", role="ANALYST", ticker="X", effort="high", prompt="p", schema={"type": "object"})
    body = session.last_kwargs["json"]
    assert body["reasoning"] == {"effort": "high"}
    assert body["max_output_tokens"] == 512


def test_resume_reuses_terminal_calls_after_crash(tmp_path: Path) -> None:
    dataset = load_dataset(FIXTURE)
    records = select_records(dataset, 1)
    base = MockResponsesClient()

    class SyntheticCrash(BaseException):
        pass

    class CrashClient:
        def __init__(self):
            self.count = 0

        def invoke(self, **kwargs):
            self.count += 1
            if self.count == 3:
                raise SyntheticCrash("synthetic process crash")
            return base.invoke(**kwargs)

    with pytest.raises(SyntheticCrash):
        run_experiment(records=records, dataset=dataset, mode="mock", output_dir=tmp_path, client=CrashClient())
    resumed = MockResponsesClient()
    with pytest.raises(JournalError, match="INDETERMINATE"):
        run_experiment(records=records, dataset=dataset, mode="mock", output_dir=tmp_path, client=resumed)
    assert resumed.calls == []


def test_repeated_run_does_not_repeat_completed_calls(tmp_path: Path) -> None:
    dataset = load_dataset(FIXTURE)
    records = select_records(dataset, 1)
    run_experiment(records=records, dataset=dataset, mode="mock", output_dir=tmp_path, client=MockResponsesClient())
    second = MockResponsesClient()
    run_experiment(records=records, dataset=dataset, mode="mock", output_dir=tmp_path, client=second)
    assert second.calls == []


def test_journal_blocks_indeterminate_retry(tmp_path: Path) -> None:
    journal = CallJournal(tmp_path / "journal")
    journal.start(call_id="c", request_hash="h", metadata={})
    journal.record_http_response(call_id="c", raw=b"{}", accounting={"cost_usd": "0.01"})
    journal.finalize(call_id="c", status="INDETERMINATE", error_type="RECOVERY", error="test")
    with pytest.raises(JournalError, match="INDETERMINATE"):
        journal.start(call_id="c", request_hash="h", metadata={})


def test_cross_process_claim_and_budget_are_atomic(tmp_path: Path) -> None:
    first = CallJournal(tmp_path / "journal")
    second = CallJournal(tmp_path / "journal")
    first.configure_budget(Decimal("0.10"))
    first.start(call_id="claimed", request_hash="h", metadata={}, reservation_usd=Decimal("0.08"))
    second.owner_pid = 99999999
    with pytest.raises(JournalBusy):
        second.start(call_id="claimed", request_hash="h", metadata={}, reservation_usd=Decimal("0.08"))
    with pytest.raises(BudgetExceeded):
        second.start(call_id="different", request_hash="h2", metadata={}, reservation_usd=Decimal("0.08"))
    assert first.reserved_cost() == Decimal("0.08")


@pytest.mark.skipif(
    os.environ.get("TRINITY_RUN_PROCESS_TEST") != "1",
    reason="real subprocess coordination test is run explicitly to avoid hanging the full pytest worker",
)
def test_two_real_processes_cannot_claim_same_call(tmp_path: Path) -> None:
    root = tmp_path / "shared"
    CallJournal(root)
    outputs = [tmp_path / "one.txt", tmp_path / "two.txt"]
    release = tmp_path / "release.txt"
    script = (
        "import sys,time\n"
        "from decimal import Decimal\n"
        "from experiments.sol_effort_v1.journal import CallJournal\n"
        "j=CallJournal(sys.argv[1])\n"
        "j.configure_budget(Decimal('0.10'))\n"
        "try:\n"
        " j.start(call_id='same',request_hash='h',metadata={},reservation_usd=Decimal('0.08'))\n"
        " open(sys.argv[2],'w').write('CLAIMED')\n"
        " while not __import__('pathlib').Path(sys.argv[3]).exists(): time.sleep(0.01)\n"
        "except Exception as e:\n"
        " open(sys.argv[2],'w').write(type(e).__name__)\n"
    )
    code = "exec(" + repr(script) + ")"
    process_one = subprocess.Popen(
        [sys.executable, "-c", code, str(root), str(outputs[0]), str(release)], cwd=str(Path.cwd()),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    for _ in range(100):
        if outputs[0].exists():
            break
        import time
        time.sleep(0.01)
    process_two = subprocess.Popen(
        [sys.executable, "-c", code, str(root), str(outputs[1]), str(release)], cwd=str(Path.cwd()),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    assert process_two.wait(timeout=10) == 0
    release.write_text("release")
    assert process_one.wait(timeout=10) == 0
    statuses = [path.read_text() for path in outputs]
    assert statuses.count("CLAIMED") == 1
    assert statuses.count("JournalBusy") == 1


def test_ledger_checksum_tampering_is_rejected() -> None:
    payload = b'{"x":1}'
    row = {"payload": payload, "byte_length": len(payload), "sha256": "0" * 64, "content_hash": "0" * 64}
    with pytest.raises(Exception, match="sha256 mismatch"):
        _json_payload({"a": row}, "a")


def test_ledger_adapter_rejects_non_official_request() -> None:
    with pytest.raises(Exception, match="not marked OFFICIAL"):
        _official_request({"request_kind": "EXPLORATORY_NON_PIT", "parameters_json": "{}"})
    _official_request({"request_kind": "PIT_SAFE_DECISION", "parameters_json": "{}"})


def test_source_without_temporal_provenance_is_rejected(tmp_path: Path) -> None:
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    value["records"][0]["sources"] = [{"id": "missing-time"}]
    path = tmp_path / "missing-time.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(DatasetError, match="temporal provenance"):
        load_dataset(path)


def test_membership_and_factorial_contrasts_are_arm_specific() -> None:
    outcome = {"D5": {"status": "COMPLETE", "stock_return_pct": 5.0, "spy_return_pct": 1.0}}
    arms = [{"record_id": "R1", "arm_id": arm, "status": "SUCCEEDED", "membership": arm != "A", "outcomes": outcome} for arm in ("A", "B", "C", "D")]
    scorecard = build_scorecard({"calls": [], "arms": arms})
    assert scorecard["financial_outcomes"]["by_arm"]["A"]["selection"]["D5"]["n"] == 0
    assert scorecard["financial_outcomes"]["by_arm"]["B"]["selection"]["D5"]["n"] == 1
    assert scorecard["financial_outcomes"]["factorial_contrasts"]["D5"]["B-A"]["mean_pct"] == 4.0


def test_blind_adjudication_does_not_assign_fact_scores() -> None:
    result = {"calls": [{"call_id": "c", "parsed": {"claims": ["claim"]}}],
              "arms": [{"record_id": "R", "arm_id": "A", "critic_call_id": "c", "critic_output_hash": "h"}]}
    dataset, key = build_adjudication_dataset(result)
    assert dataset["rows"][0]["verdict"] is None
    assert dataset["factual_scores_assigned"] is False
    assert key


def test_call_order_is_counterbalanced() -> None:
    dataset = load_dataset(FIXTURE)
    records = select_records(dataset, 4)
    from experiments.sol_effort_v1.runner import build_call_plan
    first_efforts = [next(item["effort"] for item in build_call_plan(records) if item["record_id"] == record.record_id and item["role"] == "ANALYST") for record in records]
    assert set(first_efforts) == {"medium", "high"}

