"""Separate, fail-closed scorecard for Sol effort experiments."""

from __future__ import annotations

from collections import defaultdict
from statistics import mean
from typing import Any, Mapping


HORIZONS = ("D5", "D20", "D60")


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def build_scorecard(results: Mapping[str, Any]) -> dict[str, Any]:
    calls = list(results.get("calls") or [])
    arms = list(results.get("arms") or [])
    costs: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "calls": 0,
            "cost_usd": 0.0,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "latencies": [],
            "failures": 0,
        }
    )
    for call in calls:
        key = f"{call['role'].lower()}_{call['effort']}"
        entry = costs[key]
        entry["calls"] += 1
        entry["cost_usd"] += float(call.get("cost_usd") or 0)
        for field in (
            "input_tokens", "cached_input_tokens", "cache_write_tokens",
            "output_tokens", "reasoning_tokens",
        ):
            entry[field] += int(call.get(field) or 0)
        entry["latencies"].append(float(call.get("duration_seconds") or 0))
        if call.get("status") != "SUCCEEDED":
            entry["failures"] += 1
    for entry in costs.values():
        latencies = sorted(entry.pop("latencies"))
        entry["mean_latency_seconds"] = None if not latencies else mean(latencies)
        entry["max_latency_seconds"] = None if not latencies else max(latencies)

    decisions: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for arm in arms:
        decision = arm.get("decision")
        if decision:
            decisions[arm["arm_id"]][decision] += 1

    baseline = {
        item["record_id"]: item.get("decision")
        for item in arms if item["arm_id"] == "A"
    }
    decision_differences = []
    for item in arms:
        if item["arm_id"] != "A" and item.get("decision") != baseline.get(item["record_id"]):
            decision_differences.append({
                "record_id": item["record_id"],
                "arm_id": item["arm_id"],
                "baseline_decision": baseline.get(item["record_id"]),
                "decision": item.get("decision"),
            })

    financial: dict[str, Any] = {
        "available": False,
        "reason": "No matured point-in-time outcomes supplied",
        "horizons": {
            horizon: {"n": 0, "mean_active_return_pct": None}
            for horizon in HORIZONS
        },
    }
    matured: dict[str, list[float]] = {horizon: [] for horizon in HORIZONS}
    seen_outcomes: set[tuple[str, str]] = set()
    for arm in arms:
        outcomes = arm.get("outcomes") or {}
        for horizon in HORIZONS:
            identity = (str(arm.get("record_id")), horizon)
            if identity in seen_outcomes:
                continue
            item = outcomes.get(horizon)
            if (
                isinstance(item, Mapping)
                and item.get("status") == "COMPLETE"
                and _number(item.get("stock_return_pct"))
                and _number(item.get("spy_return_pct"))
            ):
                seen_outcomes.add(identity)
                matured[horizon].append(
                    float(item["stock_return_pct"])
                    - float(item["spy_return_pct"])
                )
    if any(matured.values()):
        financial["available"] = True
        financial["reason"] = "Only COMPLETE outcomes included"
        financial["horizons"] = {
            horizon: {
                "n": len(values),
                "mean_active_return_pct": None if not values else mean(values),
            }
            for horizon, values in matured.items()
        }

    return {
        "scorecard_id": "TRINITY_SOL_EFFORT_EXPERIMENTAL_SCORECARD_V1",
        "official_protocol_untouched": True,
        "quality": {
            "records_selected": len({item["record_id"] for item in arms}),
            "calls_total": len(calls),
            "calls_succeeded": sum(item.get("status") == "SUCCEEDED" for item in calls),
            "calls_failed": sum(item.get("status") != "SUCCEEDED" for item in calls),
            "schema_errors": sum(item.get("error_type") == "SCHEMA" for item in calls),
            "parse_errors": sum(item.get("error_type") == "PARSE" for item in calls),
        },
        "costs_by_call_type": dict(costs),
        "decisions_by_arm": {key: dict(value) for key, value in decisions.items()},
        "decision_differences_vs_A": decision_differences,
        "financial_outcomes": financial,
    }

