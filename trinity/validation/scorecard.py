"""Conservative monthly aggregation for the frozen V1 protocol."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import random
from statistics import fmean, median
from typing import Iterable, Mapping

from .contracts import OFFICIAL_ELIGIBLE, ValidationContractError
from .protocol_v1 import PROTOCOL, PROTOCOL_HASH, PROTOCOL_ID


@dataclass(frozen=True)
class Scorecard:
    protocol_version: str
    protocol_hash: str
    status: str
    n_weeks: int
    primary_delta_ar20: float | None
    luna_uplift_20d: float | None
    sol_uplift_20d: float | None
    confidence_interval_95: tuple[float, float] | None
    diagnostics: Mapping[str, object]


def missed_winner_rate_20d(rows: Iterable[Mapping[str, object]]) -> dict[str, object]:
    """DIAGNOSTIC only: trace Scanner winners through the frozen funnel."""

    values = official_complete_outcomes(rows)
    winners = [row for row in values
               if row.get("horizon_sessions", 20) == 20
               and float(row.get("active_return_20d") or float("-inf")) >= 10.0]
    denominator = len(winners)
    return {
        "metric": "missed_winner_rate_20d", "metric_class": "DIAGNOSTIC",
        "threshold_active_return_pct": 10.0, "scanner_winners": denominator,
        "luna_promoted": sum(bool(row.get("luna_promoted")) for row in winners),
        "sol_evaluated": sum(bool(row.get("sol_evaluated")) for row in winners),
        "sol_promoted": sum(bool(row.get("sol_promoted")) for row in winners),
        "missed_by_luna_rate": None if denominator == 0 else
            1.0 - sum(bool(row.get("luna_promoted")) for row in winners) / denominator,
        "missed_by_sol_rate": None if denominator == 0 else
            1.0 - sum(bool(row.get("sol_promoted")) for row in winners) / denominator,
    }


def official_complete_outcomes(
    rows: Iterable[Mapping[str, object]],
) -> tuple[Mapping[str, object], ...]:
    """Select only immutable COMPLETE finals; DQF is never coerced to zero."""

    values = tuple(rows)
    _require_official(values)
    complete: list[Mapping[str, object]] = []
    for row in values:
        status = row.get("status", row.get("maturity_status"))
        if status == "COMPLETE":
            complete.append(row)
        elif status != "DATA_QUALITY_FAILURE":
            raise ValidationContractError("official outcome analytics require final outcome records")
    return tuple(complete)


def _require_official(rows: tuple[Mapping[str, object], ...]) -> None:
    ineligible = [row.get("run_id", "UNKNOWN") for row in rows
                  if row.get("eligibility") != OFFICIAL_ELIGIBLE]
    if ineligible:
        raise ValidationContractError(
            "official analytics received non-OFFICIAL_ELIGIBLE observations: "
            + ", ".join(str(value) for value in ineligible)
        )


def _block_bootstrap_ci(values: list[float]) -> tuple[float, float]:
    """Deterministic circular block bootstrap for the frozen 20-session endpoint."""

    replicates = int(PROTOCOL["bootstrap_replicates"])
    block = int(PROTOCOL["bootstrap_mean_block_weeks"]["20"])
    seed = int(hashlib.sha256((PROTOCOL_HASH + ":AR20").encode()).hexdigest()[:16], 16)
    rng = random.Random(seed)
    n = len(values)
    samples: list[float] = []
    for _ in range(replicates):
        draw: list[float] = []
        while len(draw) < n:
            start = rng.randrange(n)
            draw.extend(values[(start + offset) % n] for offset in range(block))
        samples.append(fmean(draw[:n]))
    samples.sort()
    alpha = (1.0 - float(PROTOCOL["confidence_interval"])) / 2.0
    low = samples[int(alpha * (replicates - 1))]
    high = samples[int((1.0 - alpha) * (replicates - 1))]
    return low, high


def build_scorecard(weekly: Iterable[Mapping[str, object]], *,
                    diagnostics: Mapping[str, object] | None = None) -> Scorecard:
    rows = tuple(weekly)
    _require_official(rows)
    complete = [row for row in rows if all(row.get(key) is not None for key in (
        "scanner_ar20", "luna_ar20", "sol_ar20"
    ))]
    if not complete:
        return Scorecard(PROTOCOL_ID, PROTOCOL_HASH, "INSUFFICIENT_SAMPLE", len(rows),
                         None, None, None, None, diagnostics or {})
    primary = [float(row["sol_ar20"]) - float(row["scanner_ar20"]) for row in complete]
    luna = [float(row["luna_ar20"]) - float(row["scanner_ar20"]) for row in complete]
    sol = [float(row["sol_ar20"]) - float(row["luna_ar20"]) for row in complete]
    # V1 deliberately withholds formal inference until the 12-month review window.
    formal = len(complete) >= int(PROTOCOL["formal_inference_minimum_weeks"])
    status = "FORMAL_REVIEW_DUE" if formal else "INSUFFICIENT_SAMPLE"
    return Scorecard(PROTOCOL_ID, PROTOCOL_HASH, status, len(rows), fmean(primary),
                     fmean(luna), fmean(sol), _block_bootstrap_ci(primary) if formal else None,
                     diagnostics or {
                         "median_primary_delta": median(primary)
                     })
