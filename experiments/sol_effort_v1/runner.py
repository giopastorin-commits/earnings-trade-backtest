"""Six-call-per-ticker factorial runner with crash-safe resumption."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from .canonical import sha256_bytes, sha256_json, write_json
from .adjudication import build_adjudication_dataset
from .client import MAX_OUTPUT_TOKENS, build_request_body, ProviderCallError
from .config import ARMS, MODEL, Effort
from .dataset import TickerInput
from .journal import CallJournal, JournalError
from .manifest import build_manifest
from .prompts import render_critic_prompt
from .scorecard import build_scorecard
from .schema import ResponseValidationError


def _call_id(record: TickerInput, role: str, effort: Effort, draft_effort: str = "", replicate_id: str = "r1") -> str:
    material = f"{replicate_id}|{record.record_id}|{role}|{effort}|{draft_effort}"
    return "call-" + sha256_bytes(material.encode("utf-8"))[:24]


def request_payload(*, role: str, ticker: str, effort: Effort, prompt: str, schema: dict[str, Any], max_output_tokens: int = MAX_OUTPUT_TOKENS) -> dict[str, Any]:
    del ticker
    return build_request_body(role=role, effort=effort, prompt=prompt,
                              schema=schema, max_output_tokens=max_output_tokens)


def _counterbalanced_reverse(record: TickerInput, replicate_id: str) -> bool:
    return int(sha256_bytes(f"{replicate_id}|{record.record_id}".encode())[:2], 16) % 2 == 1


def build_call_plan(records: tuple[TickerInput, ...], *, replicate_id: str = "r1") -> list[dict[str, Any]]:
    """Create a preregistered topological order, counterbalanced by record."""
    plan: list[dict[str, Any]] = []
    for record in records:
        reverse = _counterbalanced_reverse(record, replicate_id)
        analyst_efforts = ["high", "medium"] if reverse else ["medium", "high"]
        critic_pairs = [(draft, critic) for draft in ("medium", "high") for critic in ("medium", "high")]
        if reverse:
            critic_pairs.reverse()
        analyst_ids = {effort: _call_id(record, "ANALYST", effort, replicate_id=replicate_id) for effort in ("medium", "high")}
        sequence = 0
        for effort in analyst_efforts:
            plan.append({"call_id": analyst_ids[effort], "record_id": record.record_id, "ticker": record.ticker,
                         "role": "ANALYST", "effort": effort, "replicate_id": replicate_id,
                         "sequence_index": sequence, "order_group": "COUNTERBALANCED", "depends_on": []})
            sequence += 1
        for draft_effort, effort in critic_pairs:
            plan.append({"call_id": _call_id(record, "CRITIC", effort, draft_effort, replicate_id),
                         "record_id": record.record_id, "ticker": record.ticker, "role": "CRITIC",
                         "effort": effort, "analyst_effort": draft_effort, "replicate_id": replicate_id,
                         "sequence_index": sequence, "order_group": "COUNTERBALANCED",
                         "depends_on": [analyst_ids[draft_effort]]})
            sequence += 1
    return plan


def _call_record(*, plan_item: dict[str, Any], record: TickerInput, response: Any,
                 prompt: str, schema: dict[str, Any], request_hash: str,
                 status: str = "SUCCEEDED", error_type: str | None = None,
                 error: str | None = None, accounting: dict[str, Any] | None = None) -> dict[str, Any]:
    accounting = dict(accounting or {})
    result = {**plan_item, "status": status, "model": MODEL, "input_hash": record.input_hash,
              "request_hash": request_hash, "prompt_hash": sha256_bytes(prompt.encode("utf-8")),
              "schema_hash": sha256_json(schema), "prompt": prompt, "schema": schema,
              "output_text": None, "parsed": None, "draft_hash": None, "response_id": None,
              "raw_sha256": None, "output_hash": None,
              "input_tokens": int(accounting.get("input_tokens") or 0),
              "cached_input_tokens": int(accounting.get("cached_input_tokens") or 0),
              "cache_write_tokens": int(accounting.get("cache_write_tokens") or 0),
              "output_tokens": int(accounting.get("output_tokens") or 0),
              "visible_output_tokens": int(accounting.get("visible_output_tokens") or 0),
              "reasoning_tokens": int(accounting.get("reasoning_tokens") or 0),
              "total_tokens": int(accounting.get("total_tokens") or 0),
              "duration_seconds": 0.0, "cost_usd": str(accounting.get("cost_usd") or "0"),
              "error_type": error_type, "error": error}
    if response is not None:
        result.update({"output_text": response.output_text, "parsed": response.parsed,
                       "response_id": response.response_id, "raw_sha256": response.raw_sha256,
                       "output_hash": sha256_bytes(response.output_text.encode("utf-8")),
                       "input_tokens": response.input_tokens, "cached_input_tokens": response.cached_input_tokens,
                       "cache_write_tokens": response.cache_write_tokens, "output_tokens": response.output_tokens,
                       "visible_output_tokens": getattr(response, "visible_output_tokens", 0),
                       "reasoning_tokens": response.reasoning_tokens, "total_tokens": response.total_tokens,
                       "duration_seconds": response.duration_seconds, "cost_usd": response.cost_usd})
        if plan_item["role"] == "ANALYST":
            result["draft_hash"] = result["output_hash"]
    return result


def _invoke(client: Any, kwargs: dict[str, Any], journal: CallJournal) -> Any:
    if "checkpoint" in inspect.signature(client.invoke).parameters:
        kwargs["checkpoint"] = journal.record_http_response
    return client.invoke(**kwargs)


def _execute_call(*, client: Any, journal: CallJournal, item: dict[str, Any], record: TickerInput,
                  prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
    request_hash = sha256_json(request_payload(role=item["role"], ticker=record.ticker,
                                                effort=item["effort"], prompt=prompt, schema=schema,
                                                max_output_tokens=getattr(client, "max_output_tokens", MAX_OUTPUT_TOKENS)))
    metadata = {"record_id": record.record_id, "role": item["role"], "ticker": record.ticker,
                "effort": item["effort"], "prompt_hash": sha256_bytes(prompt.encode("utf-8")),
                "schema_hash": sha256_json(schema)}
    existing = journal.start(call_id=item["call_id"], request_hash=request_hash, metadata=metadata)
    if existing.get("status") in {"SUCCEEDED", "FAILED"}:
        saved = existing.get("result")
        if isinstance(saved, dict):
            return saved
        raise JournalError(f"terminal journal record lacks result: {item['call_id']}")
    if existing.get("response_received"):
        journal.finalize(call_id=item["call_id"], status="INDETERMINATE", error_type="RECOVERY",
                         error="response was checkpointed but finalization was interrupted")
        raise JournalError(f"call {item['call_id']} requires explicit indeterminate resolution")
    try:
        response = _invoke(client, {"call_id": item["call_id"], "role": item["role"], "ticker": record.ticker,
                                    "effort": item["effort"], "prompt": prompt, "schema": schema}, journal)
        saved = _call_record(plan_item=item, record=record, response=response, prompt=prompt,
                             schema=schema, request_hash=request_hash)
        journal.finalize(call_id=item["call_id"], status="SUCCEEDED", result=saved)
        return saved
    except ProviderCallError as exc:
        accounting = dict(exc.accounting)
        saved = _call_record(plan_item=item, record=record, response=None, prompt=prompt, schema=schema,
                             request_hash=request_hash, status=exc.call_status, error_type=exc.error_type,
                             error=str(exc), accounting=accounting)
        saved["raw_sha256"] = exc.raw_sha256 or (journal.load(item["call_id"]) or {}).get("raw_sha256")
        journal.finalize(call_id=item["call_id"], status=exc.call_status, result=saved,
                         error_type=exc.error_type, error=str(exc))
        if exc.call_status == "INDETERMINATE":
            raise JournalError(f"call {item['call_id']} is indeterminate; no automatic retry") from exc
        return saved
    except ResponseValidationError as exc:
        saved = _call_record(plan_item=item, record=record, response=None, prompt=prompt, schema=schema,
                             request_hash=request_hash, status="FAILED", error_type="SCHEMA", error=str(exc))
        journal.finalize(call_id=item["call_id"], status="FAILED", result=saved, error_type="SCHEMA", error=str(exc))
        return saved
    except Exception as exc:
        status = "INDETERMINATE" if getattr(client, "is_live_shadow", False) else "FAILED"
        error_type = "INDETERMINATE" if status == "INDETERMINATE" else "TRANSPORT"
        saved = _call_record(plan_item=item, record=record, response=None, prompt=prompt, schema=schema,
                             request_hash=request_hash, status=status, error_type=error_type, error=type(exc).__name__)
        journal.finalize(call_id=item["call_id"], status=status, result=saved, error_type=error_type, error=type(exc).__name__)
        if status == "INDETERMINATE":
            raise JournalError(f"call {item['call_id']} may have been billed; retry blocked") from exc
        return saved


def _arm_rows(records: tuple[TickerInput, ...], calls: list[dict[str, Any]], dataset: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in records:
        outcomes = dict(getattr(dataset, "outcomes", {}).get(record.record_id, {}))
        for arm in ARMS:
            analyst = next((x for x in calls if x["record_id"] == record.record_id and x["role"] == "ANALYST" and x["effort"] == arm.analyst_effort), None)
            critic = next((x for x in calls if x["record_id"] == record.record_id and x["role"] == "CRITIC" and x["effort"] == arm.critic_effort and x.get("analyst_effort") == arm.analyst_effort), None)
            final = None if critic is None else critic.get("parsed")
            decision = None if not isinstance(final, dict) else (final.get("decision") or final.get("status") or final.get("proposed_status"))
            rows.append({"record_id": record.record_id, "ticker": record.ticker, "week": record.week, "arm_id": arm.arm_id,
                         "analyst_call_id": None if analyst is None else analyst["call_id"],
                         "critic_call_id": None if critic is None else critic["call_id"],
                         "analyst_output_hash": None if analyst is None else analyst.get("output_hash"),
                         "critic_output_hash": None if critic is None else critic.get("output_hash"),
                         "decision": decision, "membership": decision in {"PASS", "INVESTIGATE"},
                         "membership_rule": "decision in {PASS, INVESTIGATE}", "outcomes": outcomes,
                         "status": "SUCCEEDED" if analyst and critic and analyst["status"] == "SUCCEEDED" and critic["status"] == "SUCCEEDED" else "INCOMPLETE"})
    return rows


def run_experiment(*, records: tuple[TickerInput, ...], dataset: Any, mode: str,
                   output_dir: str | Path, client: Any | None = None, replicate_id: str = "r1") -> dict[str, Any]:
    if mode not in {"dry-run", "mock", "live-shadow"}:
        raise ValueError("mode must be dry-run, mock, or live-shadow")
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    plan = build_call_plan(records, replicate_id=replicate_id)
    manifest = build_manifest(dataset, records, plan=plan, replicate_id=replicate_id,
                              max_output_tokens=getattr(client, "max_output_tokens", MAX_OUTPUT_TOKENS))
    write_json(output / "manifest.json", manifest)
    write_json(output / "call_plan.json", plan)
    if mode == "dry-run":
        results = {"experiment_id": "TRINITY_SOL_EFFORT_COMPARISON_V1", "mode": mode, "manifest_hash": manifest["manifest_hash"], "calls": [], "arms": [], "status": "PLANNED"}
        write_json(output / "results.json", results)
        write_json(output / "scorecard.json", build_scorecard(results))
        return results
    if client is None:
        raise ValueError("mock and live-shadow modes require a client")
    journal = CallJournal(output / "journal")
    if hasattr(client, "journal"):
        client.journal = journal
    calls: list[dict[str, Any]] = []
    analysts: dict[tuple[str, Effort], dict[str, Any]] = {}
    for item in plan:
        record = next(r for r in records if r.record_id == item["record_id"])
        if item["role"] == "ANALYST":
            saved = _execute_call(client=client, journal=journal, item=item, record=record, prompt=record.analyst_prompt, schema=record.analyst_schema)
            calls.append(saved)
            if saved["status"] == "SUCCEEDED":
                analysts[(record.record_id, item["effort"])] = saved
        else:
            draft = analysts.get((record.record_id, item["analyst_effort"]))
            if draft is None:
                continue
            prompt = render_critic_prompt(record, draft["output_text"])
            saved = _execute_call(client=client, journal=journal, item=item, record=record, prompt=prompt, schema=record.critic_schema)
            saved["analyst_output_hash"] = draft["output_hash"]
            calls.append(saved)
    arms = _arm_rows(records, calls, dataset)
    final_manifest = dict(manifest)
    final_manifest["completed_calls"] = [
        {"call_id": call["call_id"], "request_hash": call.get("request_hash"),
         "response_hash": call.get("raw_sha256"), "status": call.get("status"),
         "cost_usd": call.get("cost_usd", "0")}
        for call in calls
    ]
    final_manifest.pop("manifest_hash", None)
    final_manifest["manifest_hash"] = sha256_json(final_manifest)
    write_json(output / "manifest.json", final_manifest)
    results = {"experiment_id": "TRINITY_SOL_EFFORT_COMPARISON_V1", "mode": mode, "manifest_hash": final_manifest["manifest_hash"],
               "replicate_id": replicate_id, "calls": calls, "arms": arms,
               "status": "COMPLETE" if all(item["status"] == "SUCCEEDED" for item in arms) else "PARTIAL",
               "journal_cost_usd": str(journal.consumed_cost())}
    write_json(output / "results.json", results)
    write_json(output / "scorecard.json", build_scorecard(results))
    adjudication, blind_key = build_adjudication_dataset(results)
    write_json(output / "adjudication.json", adjudication)
    write_json(output / "adjudication_key.json", blind_key)
    return results
