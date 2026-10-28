"""Six-call-per-ticker factorial runner."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import sha256_bytes, sha256_json, write_json
from .config import ARMS, MODEL, Effort
from .dataset import TickerInput
from .manifest import build_manifest
from .prompts import render_critic_prompt
from .scorecard import build_scorecard
from .schema import ResponseValidationError


def _call_id(
    record: TickerInput,
    role: str,
    effort: Effort,
    draft_effort: str = "",
) -> str:
    material = f"{record.record_id}|{role}|{effort}|{draft_effort}"
    return "call-" + sha256_bytes(material.encode("utf-8"))[:24]


def build_call_plan(records: tuple[TickerInput, ...]) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    for record in records:
        analyst_ids = {
            effort: _call_id(record, "ANALYST", effort)
            for effort in ("medium", "high")
        }
        for effort in ("medium", "high"):
            plan.append({
                "call_id": analyst_ids[effort],
                "record_id": record.record_id,
                "ticker": record.ticker,
                "role": "ANALYST",
                "effort": effort,
                "depends_on": [],
            })
        for draft_effort in ("medium", "high"):
            for effort in ("medium", "high"):
                plan.append({
                    "call_id": _call_id(record, "CRITIC", effort, draft_effort),
                    "record_id": record.record_id,
                    "ticker": record.ticker,
                    "role": "CRITIC",
                    "effort": effort,
                    "analyst_effort": draft_effort,
                    "depends_on": [analyst_ids[draft_effort]],
                })
    return plan


def _call_record(
    *,
    plan_item: dict[str, Any],
    record: TickerInput,
    response: Any,
    prompt: str,
    schema: dict[str, Any],
    status: str = "SUCCEEDED",
    error_type: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    result = {
        **plan_item,
        "status": status,
        "model": MODEL,
        "input_hash": record.input_hash,
        "prompt_hash": sha256_bytes(prompt.encode("utf-8")),
        "schema_hash": sha256_json(schema),
        "prompt": prompt,
        "schema": schema,
        "output_text": None,
        "parsed": None,
        "draft_hash": None,
        "response_id": None,
        "raw_sha256": None,
        "output_hash": None,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "duration_seconds": 0.0,
        "cost_usd": "0",
        "error_type": error_type,
        "error": error,
    }
    if response is not None:
        result.update({
            "output_text": response.output_text,
            "parsed": response.parsed,
            "response_id": response.response_id,
            "raw_sha256": response.raw_sha256,
            "output_hash": sha256_bytes(response.output_text.encode("utf-8")),
            "input_tokens": response.input_tokens,
            "cached_input_tokens": response.cached_input_tokens,
            "cache_write_tokens": response.cache_write_tokens,
            "output_tokens": response.output_tokens,
            "reasoning_tokens": response.reasoning_tokens,
            "total_tokens": response.total_tokens,
            "duration_seconds": response.duration_seconds,
            "cost_usd": response.cost_usd,
        })
        if plan_item["role"] == "ANALYST":
            result["draft_hash"] = result["output_hash"]
    return result


def run_experiment(
    *,
    records: tuple[TickerInput, ...],
    dataset,
    mode: str,
    output_dir: str | Path,
    client: Any | None = None,
) -> dict[str, Any]:
    if mode not in {"dry-run", "mock", "live-shadow"}:
        raise ValueError("mode must be dry-run, mock, or live-shadow")
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest(dataset, records)
    plan = build_call_plan(records)
    write_json(output / "manifest.json", manifest)
    write_json(output / "call_plan.json", plan)
    if mode == "dry-run":
        results = {
            "experiment_id": "TRINITY_SOL_EFFORT_COMPARISON_V1",
            "mode": mode,
            "manifest_hash": manifest["manifest_hash"],
            "calls": [],
            "arms": [],
            "status": "PLANNED",
        }
        write_json(output / "results.json", results)
        write_json(output / "scorecard.json", build_scorecard(results))
        return results
    if client is None:
        raise ValueError("mock and live-shadow modes require a client")

    calls: list[dict[str, Any]] = []
    analyst_results: dict[tuple[str, Effort], dict[str, Any]] = {}
    for record in records:
        for effort in ("medium", "high"):
            item = next(
                value
                for value in plan
                if value["record_id"] == record.record_id
                and value["role"] == "ANALYST"
                and value["effort"] == effort
            )
            try:
                response = client.invoke(
                    call_id=item["call_id"],
                    role="ANALYST",
                    ticker=record.ticker,
                    effort=effort,
                    prompt=record.analyst_prompt,
                    schema=record.analyst_schema,
                )
                saved = _call_record(
                    plan_item=item,
                    record=record,
                    response=response,
                    prompt=record.analyst_prompt,
                    schema=record.analyst_schema,
                )
            except ResponseValidationError as exc:
                saved = _call_record(
                    plan_item=item,
                    record=record,
                    response=None,
                    prompt=record.analyst_prompt,
                    schema=record.analyst_schema,
                    status="FAILED",
                    error_type="SCHEMA",
                    error=type(exc).__name__,
                )
            except Exception as exc:
                saved = _call_record(
                    plan_item=item,
                    record=record,
                    response=None,
                    prompt=record.analyst_prompt,
                    schema=record.analyst_schema,
                    status="FAILED",
                    error_type="TRANSPORT",
                    error=type(exc).__name__,
                )
            calls.append(saved)
            if saved["status"] == "SUCCEEDED":
                analyst_results[(record.record_id, effort)] = saved

    for record in records:
        for draft_effort in ("medium", "high"):
            draft = analyst_results.get((record.record_id, draft_effort))
            if draft is None:
                continue
            critic_prompt = render_critic_prompt(record, draft["output_text"])
            for effort in ("medium", "high"):
                item = next(
                    value
                    for value in plan
                    if value["record_id"] == record.record_id
                    and value["role"] == "CRITIC"
                    and value["effort"] == effort
                    and value["analyst_effort"] == draft_effort
                )
                try:
                    response = client.invoke(
                        call_id=item["call_id"],
                        role="CRITIC",
                        ticker=record.ticker,
                        effort=effort,
                        prompt=critic_prompt,
                        schema=record.critic_schema,
                    )
                    saved = _call_record(
                        plan_item=item,
                        record=record,
                        response=response,
                        prompt=critic_prompt,
                        schema=record.critic_schema,
                    )
                except ResponseValidationError as exc:
                    saved = _call_record(
                        plan_item=item,
                        record=record,
                        response=None,
                        prompt=critic_prompt,
                        schema=record.critic_schema,
                        status="FAILED",
                        error_type="SCHEMA",
                        error=type(exc).__name__,
                    )
                except Exception as exc:
                    saved = _call_record(
                        plan_item=item,
                        record=record,
                        response=None,
                        prompt=critic_prompt,
                        schema=record.critic_schema,
                        status="FAILED",
                        error_type="TRANSPORT",
                        error=type(exc).__name__,
                    )
                saved["analyst_output_hash"] = draft["output_hash"]
                calls.append(saved)

    arm_rows: list[dict[str, Any]] = []
    for record in records:
        for arm in ARMS:
            analyst = next(
                (
                    item for item in calls
                    if item["record_id"] == record.record_id
                    and item["role"] == "ANALYST"
                    and item["effort"] == arm.analyst_effort
                ),
                None,
            )
            critic = next(
                (
                    item for item in calls
                    if item["record_id"] == record.record_id
                    and item["role"] == "CRITIC"
                    and item["effort"] == arm.critic_effort
                    and item["analyst_effort"] == arm.analyst_effort
                ),
                None,
            )
            final = None if critic is None else critic.get("parsed")
            arm_rows.append({
                "record_id": record.record_id,
                "ticker": record.ticker,
                "arm_id": arm.arm_id,
                "analyst_call_id": None if analyst is None else analyst["call_id"],
                "critic_call_id": None if critic is None else critic["call_id"],
                "analyst_output_hash": None if analyst is None else analyst.get("output_hash"),
                "critic_output_hash": None if critic is None else critic.get("output_hash"),
                "decision": None if not isinstance(final, dict) else (
                    final.get("decision")
                    or final.get("status")
                    or final.get("proposed_status")
                ),
                "outcomes": record.outcomes,
                "status": (
                    "SUCCEEDED"
                    if analyst and critic
                    and analyst["status"] == "SUCCEEDED"
                    and critic["status"] == "SUCCEEDED"
                    else "INCOMPLETE"
                ),
            })
    results = {
        "experiment_id": "TRINITY_SOL_EFFORT_COMPARISON_V1",
        "mode": mode,
        "manifest_hash": manifest["manifest_hash"],
        "calls": calls,
        "arms": arm_rows,
        "status": (
            "COMPLETE"
            if all(item["status"] == "SUCCEEDED" for item in arm_rows)
            else "PARTIAL"
        ),
    }
    write_json(output / "results.json", results)
    write_json(output / "scorecard.json", build_scorecard(results))
    return results

