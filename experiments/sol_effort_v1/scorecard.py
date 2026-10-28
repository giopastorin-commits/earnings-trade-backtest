"""Separate technical, adjudication and factorial financial scorecard."""

from __future__ import annotations

from collections import defaultdict
from statistics import mean
from typing import Any, Mapping


HORIZONS = ("D5", "D20", "D60")
ARMS = ("A", "B", "C", "D")


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _paired_contrast(values: Mapping[str, float | None], formula: str) -> float | None:
    if any(values.get(arm) is None for arm in ARMS):
        return None
    a, b, c, d = (float(values[arm]) for arm in ARMS)
    return {"B-A": b - a, "C-A": c - a, "D-B": d - b,
            "D-C": d - c, "D-C-B+A": d - c - b + a}[formula]


def _financial_by_arm(arms: list[Mapping[str, Any]]) -> dict[str, Any]:
    by_arm: dict[str, Any] = {}
    for arm_id in ARMS:
        rows = [row for row in arms if row.get("arm_id") == arm_id]
        entry: dict[str, Any] = {"selection": {}, "executable_trade": {}}
        for horizon in HORIZONS:
            selected = []
            trade = []
            for row in rows:
                outcome = (row.get("outcomes") or {}).get(horizon)
                if not isinstance(outcome, Mapping) or outcome.get("status") != "COMPLETE":
                    continue
                if not row.get("membership", True):
                    continue
                stock = outcome.get("stock_return_pct")
                spy = outcome.get("spy_return_pct")
                if _number(stock) and _number(spy):
                    selected.append(float(stock) - float(spy))
                if _number(outcome.get("trade_return_pct")) and _number(outcome.get("trade_cost_pct", 0)):
                    trade.append(float(outcome["trade_return_pct"]) - float(outcome.get("trade_cost_pct", 0)))
            entry["selection"][horizon] = {
                "n": len(selected),
                "mean_active_return_pct": None if not selected else mean(selected),
                "complete_only": True,
            }
            entry["executable_trade"][horizon] = {
                "available": bool(trade),
                "n": len(trade),
                "mean_net_return_pct": None if not trade else mean(trade),
                "requires_trade_return_and_cost_fields": True,
            }
        by_arm[arm_id] = entry
    return by_arm


def _factorial_financial(arms: list[Mapping[str, Any]]) -> dict[str, Any]:
    rows_by_key: dict[tuple[str, str], dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for row in arms:
        rows_by_key[(str(row.get("record_id")), str(row.get("arm_id")))][str(row.get("arm_id"))] = row
    contrasts: dict[str, Any] = {}
    for horizon in HORIZONS:
        per_record: dict[str, dict[str, float | None]] = {}
        for record_id in sorted({key[0] for key in rows_by_key}):
            values: dict[str, float | None] = {}
            for arm_id in ARMS:
                row = rows_by_key.get((record_id, arm_id), {}).get(arm_id)
                outcome = None if row is None else (row.get("outcomes") or {}).get(horizon)
                if row is None or row.get("status") != "SUCCEEDED" or not isinstance(outcome, Mapping) or outcome.get("status") != "COMPLETE":
                    values[arm_id] = None
                    continue
                if not row.get("membership", True):
                    # This is a pre-declared portfolio contribution metric,
                    # not a numerical encoding of the categorical decision.
                    values[arm_id] = 0.0
                elif _number(outcome.get("stock_return_pct")) and _number(outcome.get("spy_return_pct")):
                    values[arm_id] = float(outcome["stock_return_pct"]) - float(outcome["spy_return_pct"])
                else:
                    values[arm_id] = None
            per_record[record_id] = values
        formulas = ("B-A", "C-A", "D-B", "D-C", "D-C-B+A")
        contrasts[horizon] = {
            formula: {
                "n_pairs": sum(_paired_contrast(values, formula) is not None for values in per_record.values()),
                "mean_pct": (lambda vals: None if not vals else mean(vals))(
                    [value for values in per_record.values() if (value := _paired_contrast(values, formula)) is not None]
                ),
                "metric": "paired per-record active-return contribution; unselected=0",
            }
            for formula in formulas
        }
    return contrasts


def build_scorecard(results: Mapping[str, Any]) -> dict[str, Any]:
    calls = list(results.get("calls") or [])
    arms = list(results.get("arms") or [])
    costs: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "calls": 0, "cost_usd": 0.0, "input_tokens": 0, "cached_input_tokens": 0,
        "cache_write_tokens": 0, "output_tokens": 0, "visible_output_tokens": 0,
        "reasoning_tokens": 0, "latencies": [], "failures": 0,
            "unknown_cost_calls": 0,
    })
    for call in calls:
        key = f"{call['role'].lower()}_{call['effort']}"
        entry = costs[key]
        entry["calls"] += 1
        if call.get("cost_unknown") or call.get("cost_usd") is None:
            entry["unknown_cost_calls"] += 1
        else:
            entry["cost_usd"] += float(call.get("cost_usd") or 0)
        for field in ("input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens", "visible_output_tokens", "reasoning_tokens"):
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
        if arm.get("decision"):
            decisions[arm["arm_id"]][arm["decision"]] += 1
    baseline = {item["record_id"]: item.get("decision") for item in arms if item["arm_id"] == "A"}
    differences = [{"record_id": item["record_id"], "arm_id": item["arm_id"],
                    "baseline_decision": baseline.get(item["record_id"]), "decision": item.get("decision")}
                   for item in arms if item["arm_id"] != "A" and item.get("decision") != baseline.get(item["record_id"])]

    financial_by_arm = _financial_by_arm(arms)
    available = any(
        value["selection"][horizon]["n"] > 0
        for value in financial_by_arm.values() for horizon in HORIZONS
    )
    financial = {
        "available": available,
        "reason": "Only COMPLETE outcomes and immutable arm membership included" if available else "No matured point-in-time outcomes supplied",
        "by_arm": financial_by_arm,
        "factorial_contrasts": _factorial_financial(arms) if available else {},
        "trade_returns": {"available": False, "reason": "No executable trade_return_pct and trade_cost_pct fields supplied"},
        "transaction_costs": {"included": False, "reason": "Not supplied in outcome contract"},
        "ai_costs": {
            "total_usd": sum(float(call.get("cost_usd") or 0) for call in calls if not call.get("cost_unknown")),
            "unknown_cost_calls": sum(bool(call.get("cost_unknown")) for call in calls),
            "included_separately": True,
        },
    }
    # Backward-compatible common-outcome view. It is deliberately not used
    # for arm comparisons; each arm's basket remains in by_arm above.
    common: dict[str, list[float]] = {horizon: [] for horizon in ("D5", "D20", "D60")}
    seen: set[tuple[str, str]] = set()
    for row in arms:
        for horizon in common:
            outcome = (row.get("outcomes") or {}).get(horizon)
            identity = (str(row.get("record_id")), horizon)
            if identity in seen or not isinstance(outcome, Mapping) or outcome.get("status") != "COMPLETE":
                continue
            if _number(outcome.get("stock_return_pct")) and _number(outcome.get("spy_return_pct")):
                seen.add(identity)
                common[horizon].append(float(outcome["stock_return_pct"]) - float(outcome["spy_return_pct"]))
    financial["horizons"] = {horizon: {"n": len(values), "mean_active_return_pct": None if not values else mean(values)} for horizon, values in common.items()}
    return {
        "scorecard_id": "TRINITY_SOL_EFFORT_EXPERIMENTAL_SCORECARD_V2",
        "official_protocol_untouched": True,
        "quality": {
            "records_selected": len({item["record_id"] for item in arms}),
            "arms_expected": 4, "calls_planned": int(results.get("planned_calls") or 0),
            "calls_total": len(calls),
            "calls_succeeded": sum(item.get("status") == "SUCCEEDED" for item in calls),
            "calls_failed": sum(item.get("status") != "SUCCEEDED" for item in calls),
            "schema_errors": sum(item.get("error_type") == "SCHEMA" for item in calls),
            "parse_errors": sum(item.get("error_type") == "PARSE" for item in calls),
            "indeterminate_calls": sum(item.get("status") == "INDETERMINATE" for item in calls),
            "skipped_dependency_calls": sum(item.get("status") == "SKIPPED_DEPENDENCY" for item in calls),
        },
        "costs_by_call_type": dict(costs),
        "decisions_by_arm": {key: dict(value) for key, value in decisions.items()},
        "decision_differences_vs_A": differences,
        "factorial_contrasts_require_numeric_metric": True,
        "financial_outcomes": financial,
        "factual_quality": {"available": False, "reason": "Requires independent blinded adjudication dataset"},
    }
