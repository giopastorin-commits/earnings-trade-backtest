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
    MAX_OUTPUT_TOKENS,
    LiveShadowDisabled,
    MockResponsesClient,
    OpenAIResponsesClient,
    ProviderCallError,
    build_request_body,
    calculate_cost,
    calculate_prudent_reservation,
    prudent_input_token_bound,
)
from experiments.sol_effort_v1.adjudication import build_adjudication_dataset
from experiments.sol_effort_v1.archive import _json_payload, _official_request
from experiments.sol_effort_v1.canonical import sha256_bytes, sha256_json
from experiments.sol_effort_v1.config import TOKEN_PREFLIGHT_AUTHORIZATION_SENTINEL
from experiments.sol_effort_v1.journal import BudgetExceeded, CallJournal, JournalBusy, JournalError
from experiments.sol_effort_v1.dataset import DatasetError, load_dataset, select_records
from experiments.sol_effort_v1.runner import _execute_call, build_call_plan, run_experiment
from experiments.sol_effort_v1.prompts import render_critic_prompt
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


def _count_session(input_tokens: int = 100) -> _Session:
    return _Session(_Response(json.dumps({
        "object": "response.input_tokens", "input_tokens": input_tokens,
    }).encode()))


def _preflight(client: OpenAIResponsesClient, *, role: str, effort: str,
               prompt: str, schema: dict, input_tokens: int = 100) -> dict:
    body = build_request_body(
        role=role, effort=effort, prompt=prompt, schema=schema,
        max_output_tokens=client.max_output_tokens,
    )
    return {
        "model": body["model"],
        "request_hash": sha256_json(body),
        "input_tokens": input_tokens,
        "max_output_tokens": client.max_output_tokens,
        "reservation_usd": str(calculate_prudent_reservation(
            input_tokens, client.max_output_tokens,
        )),
    }


def _billing_evidence(**overrides) -> dict:
    value = {
        "source_type": "OPENAI_BILLING_EXPORT",
        "external_reference": "billing-export-2026-10-28/call-123",
        "document_sha256": "a" * 64,
        "evidence_timestamp_utc": "2026-10-28T12:00:00Z",
        "reason": "Provider billing export identifies the settled request.",
        "verified_by": "independent-auditor",
    }
    value.update(overrides)
    return value


def _live_client(payload: dict, tmp_path: Path) -> tuple[OpenAIResponsesClient, CallJournal, str]:
    raw = json.dumps(payload).encode()
    journal = CallJournal(tmp_path / "journal")
    client = OpenAIResponsesClient(
        max_budget_usd=Decimal("1"), authorization="I_UNDERSTAND_LIVE_SHADOW_IS_PAID_AND_ISOLATED",
        api_key="test-only", session=_Session(_Response(raw)),
        token_preflight_authorization=TOKEN_PREFLIGHT_AUTHORIZATION_SENTINEL,
        token_count_session=_count_session(),
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
                      checkpoint=lambda **kwargs: (captured.append(kwargs), journal.record_http_response(**kwargs)),
                      token_preflight=_preflight(client, role="ANALYST", effort="high", prompt="p", schema=schema))
    assert exc.value.error_type == "SCHEMA"
    assert captured and journal.consumed_cost() > 0
    assert journal.load(call_id)["raw_sha256"] == sha256_bytes(json.dumps(payload).encode())


def test_incomplete_response_with_usage_is_not_a_financial_result(tmp_path: Path) -> None:
    payload = {"id": "r1", "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
               "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}}
    client, journal, call_id = _live_client(payload, tmp_path)
    with pytest.raises(ProviderCallError) as exc:
        client.invoke(call_id=call_id, role="ANALYST", ticker="X", effort="high", prompt="p", schema={"type": "object"},
                      checkpoint=journal.record_http_response,
                      token_preflight=_preflight(client, role="ANALYST", effort="high", prompt="p", schema={"type": "object"}))
    assert exc.value.error_type == "INCOMPLETE"
    assert journal.consumed_cost() > 0


@pytest.mark.parametrize("status_code", (400, 429, 500))
def test_http_error_without_usage_is_indeterminate_and_reserved(
    tmp_path: Path, status_code: int,
) -> None:
    dataset = load_dataset(FIXTURE)
    record = select_records(dataset, 1)[0]
    journal = CallJournal(tmp_path / "journal", max_budget_usd=Decimal("100"))
    session = _Session(_Response(b"provider-error", status_code=status_code))
    client = OpenAIResponsesClient(
        max_budget_usd=Decimal("100"),
        authorization="I_UNDERSTAND_LIVE_SHADOW_IS_PAID_AND_ISOLATED",
        api_key="test-only", session=session, journal=journal,
        token_preflight_authorization=TOKEN_PREFLIGHT_AUTHORIZATION_SENTINEL,
        token_count_session=_count_session(),
    )
    item = {
        "call_id": f"http-{status_code}", "record_id": record.record_id,
        "ticker": record.ticker, "role": "ANALYST", "effort": "high",
        "replicate_id": "r1", "sequence_index": 0,
        "order_group": "TEST", "depends_on": [],
    }
    with pytest.raises(JournalError, match="indeterminate"):
        _execute_call(
            client=client, journal=journal, item=item, record=record,
            prompt=record.analyst_prompt, schema=record.analyst_schema,
        )
    row = journal.load(item["call_id"])
    assert session.calls == 1
    assert row["status"] == "INDETERMINATE"
    assert row["cost_unknown"] is True
    assert row["reserved_usd"] != "0"
    retry_session = _Session(_Response(b"should-not-send"))
    retry_client = OpenAIResponsesClient(
        max_budget_usd=Decimal("100"),
        authorization="I_UNDERSTAND_LIVE_SHADOW_IS_PAID_AND_ISOLATED",
        api_key="test-only", session=retry_session, journal=journal,
        token_preflight_authorization=TOKEN_PREFLIGHT_AUTHORIZATION_SENTINEL,
        token_count_session=_count_session(),
    )
    with pytest.raises(JournalError, match="INDETERMINATE"):
        _execute_call(
            client=retry_client, journal=journal, item=item, record=record,
            prompt=record.analyst_prompt, schema=record.analyst_schema,
        )
    assert retry_session.calls == 0


def test_timeout_after_send_is_indeterminate_and_reserved(tmp_path: Path) -> None:
    dataset = load_dataset(FIXTURE)
    record = select_records(dataset, 1)[0]

    class TimeoutSession(_Session):
        def post(self, *args, **kwargs):
            self.calls += 1
            raise TimeoutError("timeout after request dispatch")

    journal = CallJournal(tmp_path / "journal", max_budget_usd=Decimal("100"))
    session = TimeoutSession(_Response(b"unused"))
    client = OpenAIResponsesClient(
        max_budget_usd=Decimal("100"),
        authorization="I_UNDERSTAND_LIVE_SHADOW_IS_PAID_AND_ISOLATED",
        api_key="test-only", session=session, journal=journal,
        token_preflight_authorization=TOKEN_PREFLIGHT_AUTHORIZATION_SENTINEL,
        token_count_session=_count_session(),
    )
    item = {
        "call_id": "timeout", "record_id": record.record_id,
        "ticker": record.ticker, "role": "ANALYST", "effort": "medium",
        "replicate_id": "r1", "sequence_index": 0,
        "order_group": "TEST", "depends_on": [],
    }
    with pytest.raises(JournalError, match="may have been billed"):
        _execute_call(
            client=client, journal=journal, item=item, record=record,
            prompt=record.analyst_prompt, schema=record.analyst_schema,
        )
    row = journal.load("timeout")
    assert session.calls == 1
    assert row["status"] == "INDETERMINATE"
    assert row["cost_unknown"] is True
    assert row["reserved_usd"] != "0"


def test_total_tokens_missing_is_normalized_in_client_and_journal(tmp_path: Path) -> None:
    payload = {
        "id": "missing-total", "status": "completed",
        "output": [{"content": [{"type": "output_text", "text": "{}"}]}],
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }
    journal = CallJournal(tmp_path / "journal")
    journal.start(call_id="missing-total", request_hash="h", metadata={})
    client = OpenAIResponsesClient(
        max_budget_usd=Decimal("1"),
        authorization="I_UNDERSTAND_LIVE_SHADOW_IS_PAID_AND_ISOLATED",
        api_key="test-only", session=_Session(_Response(json.dumps(payload).encode())),
        journal=journal,
    )
    response = client.invoke(
        call_id="missing-total", role="ANALYST", ticker="X", effort="high",
        prompt="p", schema={"type": "object"}, checkpoint=journal.record_http_response,
        token_preflight=_preflight(client, role="ANALYST", effort="high", prompt="p", schema={"type": "object"}),
    )
    assert response.total_tokens == 30
    assert journal.load("missing-total")["accounting"]["total_tokens"] == 30


def test_prudent_budget_includes_schema_and_full_output_limit() -> None:
    client = OpenAIResponsesClient(
        max_budget_usd=Decimal("100"),
        authorization="I_UNDERSTAND_LIVE_SHADOW_IS_PAID_AND_ISOLATED",
        api_key="test-only",
    )
    small_schema = {"type": "object"}
    large_schema = {"type": "object", "description": "x" * 10000}
    short = client.offline_estimate_max_cost(
        role="CRITIC", effort="medium", prompt="short", schema=small_schema,
    )
    long = client.offline_estimate_max_cost(
        role="CRITIC", effort="medium", prompt="x" * 10000, schema=large_schema,
    )
    body = build_request_body(
        role="CRITIC", effort="high", prompt="short", schema=small_schema,
        max_output_tokens=MAX_OUTPUT_TOKENS,
    )
    expected = calculate_prudent_reservation(
        prudent_input_token_bound(body), MAX_OUTPUT_TOKENS,
    )
    assert long > short
    assert client.offline_estimate_max_cost(
        role="CRITIC", effort="high", prompt="short", schema=small_schema,
    ) == expected


def test_prudent_budget_uses_reasoning_output_limit() -> None:
    client = OpenAIResponsesClient(
        max_budget_usd=Decimal("100"),
        authorization="I_UNDERSTAND_LIVE_SHADOW_IS_PAID_AND_ISOLATED",
        api_key="test-only", max_output_tokens=512,
    )
    body = build_request_body(
        role="ANALYST", effort="high", prompt="p", schema={"type": "object"},
        max_output_tokens=512,
    )
    assert client.offline_estimate_max_cost(
        role="ANALYST", effort="high", prompt="p", schema={"type": "object"},
    ) == calculate_prudent_reservation(prudent_input_token_bound(body), 512)


def test_provider_token_preflight_counts_exact_request_and_records_hash() -> None:
    count_session = _count_session(1234)
    generation_session = _Session(_Response(b"unused"))
    client = OpenAIResponsesClient(
        max_budget_usd=Decimal("10"),
        authorization="I_UNDERSTAND_LIVE_SHADOW_IS_PAID_AND_ISOLATED",
        api_key="test-only", session=generation_session,
        token_preflight_authorization=TOKEN_PREFLIGHT_AUTHORIZATION_SENTINEL,
        token_count_session=count_session,
    )
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    preflight = client.prepare_reservation(
        role="ANALYST", effort="high", prompt="prompt", schema=schema,
    )
    exact_body = build_request_body(
        role="ANALYST", effort="high", prompt="prompt", schema=schema,
        max_output_tokens=client.max_output_tokens,
    )
    assert count_session.last_kwargs["json"] == exact_body
    assert preflight["request_hash"] == sha256_json(exact_body)
    assert preflight["input_tokens"] == 1234
    assert Decimal(preflight["reservation_usd"]) == calculate_prudent_reservation(1234, MAX_OUTPUT_TOKENS)
    assert generation_session.calls == 0


def test_generation_rejects_request_modified_after_token_count() -> None:
    client = OpenAIResponsesClient(
        max_budget_usd=Decimal("10"),
        authorization="I_UNDERSTAND_LIVE_SHADOW_IS_PAID_AND_ISOLATED",
        api_key="test-only", session=_Session(_Response(b"unused")),
        token_preflight_authorization=TOKEN_PREFLIGHT_AUTHORIZATION_SENTINEL,
        token_count_session=_count_session(100),
    )
    schema = {"type": "object"}
    preflight = client.prepare_reservation(
        role="ANALYST", effort="medium", prompt="original", schema=schema,
    )
    with pytest.raises(LiveShadowDisabled, match="differs"):
        client.invoke(
            call_id="changed", role="ANALYST", ticker="X", effort="medium",
            prompt="modified", schema=schema, token_preflight=preflight,
        )
    assert client.session.calls == 0


def test_provider_count_above_272k_blocks_generation() -> None:
    generation_session = _Session(_Response(b"unused"))
    client = OpenAIResponsesClient(
        max_budget_usd=Decimal("10"),
        authorization="I_UNDERSTAND_LIVE_SHADOW_IS_PAID_AND_ISOLATED",
        api_key="test-only", session=generation_session,
        token_preflight_authorization=TOKEN_PREFLIGHT_AUTHORIZATION_SENTINEL,
        token_count_session=_count_session(272001),
    )
    with pytest.raises(LiveShadowDisabled, match="272000"):
        client.prepare_reservation(
            role="ANALYST", effort="high", prompt="p", schema={"type": "object"},
        )
    assert generation_session.calls == 0


def test_live_generation_requires_authorized_reliable_token_count() -> None:
    client = OpenAIResponsesClient(
        max_budget_usd=Decimal("10"),
        authorization="I_UNDERSTAND_LIVE_SHADOW_IS_PAID_AND_ISOLATED",
        api_key="test-only", session=_Session(_Response(b"unused")),
        token_count_session=_count_session(100),
    )
    with pytest.raises(LiveShadowDisabled, match="authorization"):
        client.prepare_reservation(
            role="ANALYST", effort="high", prompt="p", schema={"type": "object"},
        )


def test_budget_rejects_critic_before_http(tmp_path: Path) -> None:
    dataset = load_dataset(FIXTURE)
    record = select_records(dataset, 1)[0]
    prompt = render_critic_prompt(record, "draft")
    session = _Session(_Response(b"should-not-send"))
    client = OpenAIResponsesClient(
        max_budget_usd=Decimal("100"),
        authorization="I_UNDERSTAND_LIVE_SHADOW_IS_PAID_AND_ISOLATED",
        api_key="test-only", session=session,
        token_preflight_authorization=TOKEN_PREFLIGHT_AUTHORIZATION_SENTINEL,
        token_count_session=_count_session(),
    )
    reservation = calculate_prudent_reservation(100, client.max_output_tokens)
    journal = CallJournal(tmp_path / "journal", max_budget_usd=reservation / 2)
    item = {
        "call_id": "critic-budget", "record_id": record.record_id,
        "ticker": record.ticker, "role": "CRITIC", "effort": "high",
        "analyst_effort": "medium", "replicate_id": "r1",
        "sequence_index": 0, "order_group": "TEST", "depends_on": ["analyst"],
    }
    with pytest.raises(BudgetExceeded):
        _execute_call(
            client=client, journal=journal, item=item, record=record,
            prompt=prompt, schema=record.critic_schema,
        )
    assert session.calls == 0


def test_indeterminate_unknown_reservation_survives_resume(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    first = CallJournal(root)
    first.start(call_id="resume-unknown", request_hash="h", metadata={}, reservation_usd=Decimal("0.08"))
    first.record_accounting(call_id="resume-unknown", accounting={
        "cost_usd": None, "cost_unknown": True, "usage_unavailable": True,
    })
    first.finalize(call_id="resume-unknown", status="INDETERMINATE", error_type="RECOVERY")
    second = CallJournal(root)
    with pytest.raises(JournalError, match="INDETERMINATE"):
        second.start(call_id="resume-unknown", request_hash="h", metadata={})
    assert second.reserved_cost() == Decimal("0.08")
    reconciled = second.reconcile_cost(
        call_id="resume-unknown", cost_usd=Decimal("0.03"), evidence=_billing_evidence(),
    )
    assert reconciled["cost_unknown"] is False
    assert second.reserved_cost() == Decimal("0")


def test_reconciliation_requires_verifiable_evidence_and_keeps_reservation(tmp_path: Path) -> None:
    journal = CallJournal(tmp_path / "journal")
    journal.start(call_id="unknown", request_hash="h", metadata={}, reservation_usd=Decimal("0.08"))
    journal.record_accounting(call_id="unknown", accounting={"cost_usd": None, "cost_unknown": True})
    journal.finalize(call_id="unknown", status="INDETERMINATE")
    for evidence in (None, {}, {"source_type": "anything"}, _billing_evidence(document_sha256="bad")):
        with pytest.raises(JournalError, match="evidence|source|checksum|fields"):
            journal.reconcile_cost(call_id="unknown", cost_usd=Decimal("0"), evidence=evidence)
    assert journal.load("unknown")["cost_unknown"] is True
    assert journal.reserved_cost() == Decimal("0.08")


def test_reconciliation_is_append_only_idempotent_and_auditable(tmp_path: Path) -> None:
    journal = CallJournal(tmp_path / "journal")
    journal.start(call_id="unknown", request_hash="h", metadata={}, reservation_usd=Decimal("0.08"))
    journal.record_accounting(call_id="unknown", accounting={"cost_usd": None, "cost_unknown": True})
    journal.finalize(call_id="unknown", status="INDETERMINATE")
    evidence = _billing_evidence()
    journal.reconcile_cost(call_id="unknown", cost_usd=Decimal("0.03"), evidence=evidence)
    history = journal.reconciliations("unknown")
    assert len(history) == 1
    assert history[0]["previous_cost_usd"] is None
    assert history[0]["reconciled_cost_usd"] == "0.03"
    assert history[0]["released_reservation_usd"] == "0.08"
    assert history[0]["evidence"] == evidence
    journal.reconcile_cost(call_id="unknown", cost_usd=Decimal("0.03"), evidence=evidence)
    assert len(journal.reconciliations("unknown")) == 1
    with pytest.raises(JournalError, match="already exists"):
        journal.reconcile_cost(
            call_id="unknown", cost_usd=Decimal("0"),
            evidence=_billing_evidence(external_reference="billing-export-other"),
        )
    with journal._connection() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE cost_reconciliations SET reconciled_cost_usd='0' WHERE call_id='unknown'"
            )


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
                  checkpoint=lambda **kwargs: events.append("checkpoint"),
                  token_preflight=_preflight(client, role="ANALYST", effort="high", prompt="p", schema={"type": "object"}))
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
                      checkpoint=journal.record_http_response,
                      token_preflight=_preflight(client, role="ANALYST", effort="high", prompt="p", schema={"type": "object"}))
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
                      checkpoint=journal.record_http_response,
                      token_preflight=_preflight(client, role="ANALYST", effort="high", prompt="p", schema={"type": "object"}))
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
        client.invoke(call_id="new", role="ANALYST", ticker="X", effort="high", prompt="p", schema={"type": "object"},
                      token_preflight=_preflight(client, role="ANALYST", effort="high", prompt="p", schema={"type": "object"}))
    assert session.calls == 0


def test_responses_body_contains_explicit_reasoning_effort(tmp_path: Path) -> None:
    payload = {"id": "r1", "status": "completed", "output": [{"content": [{"type": "output_text", "text": "{}"}]}],
               "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
    session = _Session(_Response(json.dumps(payload).encode()))
    client = OpenAIResponsesClient(
        max_budget_usd=Decimal("1"), authorization="I_UNDERSTAND_LIVE_SHADOW_IS_PAID_AND_ISOLATED",
        api_key="test-only", session=session, max_output_tokens=512,
    )
    client.invoke(call_id="body", role="ANALYST", ticker="X", effort="high", prompt="p", schema={"type": "object"},
                  token_preflight=_preflight(client, role="ANALYST", effort="high", prompt="p", schema={"type": "object"}))
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


@pytest.mark.skipif(
    os.environ.get("TRINITY_RUN_PROCESS_TEST") != "1",
    reason="real subprocess budget test is run explicitly to avoid hanging the full pytest worker",
)
def test_two_real_processes_respect_shared_budget_for_different_calls(tmp_path: Path) -> None:
    root = tmp_path / "shared-budget"
    output = [tmp_path / "one.txt", tmp_path / "two.txt"]
    script = (
        "import sys\n"
        "from decimal import Decimal\n"
        "from experiments.sol_effort_v1.journal import CallJournal\n"
        "j=CallJournal(sys.argv[1])\n"
        "j.configure_budget(Decimal('0.10'))\n"
        "j.start(call_id=sys.argv[2],request_hash=sys.argv[2],metadata={},reservation_usd=Decimal('0.08'))\n"
        "open(sys.argv[3],'w').write('CLAIMED')\n"
    )
    code = "exec(" + repr(script) + ")"
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(root), name, str(path)],
            cwd=str(Path.cwd()), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        for name, path in (("one", output[0]), ("two", output[1]))
    ]
    codes = [process.wait(timeout=10) for process in processes]
    assert sorted(codes) == [0, 1]
    claimed = [path.exists() for path in output]
    journal = CallJournal(root)
    assert sum(claimed) == 1
    assert journal.reserved_cost() == Decimal("0.08")


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


def test_total_tokens_are_consistent_in_scorecard_without_reasoning_double_count() -> None:
    scorecard = build_scorecard({
        "calls": [{
            "role": "ANALYST", "effort": "high", "status": "SUCCEEDED",
            "input_tokens": 10, "output_tokens": 20, "reasoning_tokens": 7,
            "total_tokens": 30, "cost_usd": "0.001", "duration_seconds": 0,
        }],
        "arms": [],
    })
    call_type = scorecard["costs_by_call_type"]["analyst_high"]
    assert call_type["input_tokens"] == 10
    assert call_type["output_tokens"] == 20
    assert call_type["reasoning_tokens"] == 7
    assert call_type["total_tokens"] == 30
    assert scorecard["token_totals"] == {
        "input_tokens": 10, "output_tokens": 20,
        "reasoning_tokens": 7, "total_tokens": 30,
    }


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

