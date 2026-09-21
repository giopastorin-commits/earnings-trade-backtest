"""Frozen H2 V1 secondary Spearman analyses and ten-test BH correction.

The module consumes already-built experimental rows. It never changes the
primary engine, analyzes project data on import, or applies trading rules.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
import platform

import numpy as np
import pandas as pd

from .h2_confirmatory import (
    BOOTSTRAP_REPS,
    BOOTSTRAP_SEED,
    H2ConfirmatoryInputError,
    MIN_VALID_REPS,
    PROTOCOL,
    _average_ranks,
    _cluster_t,
    _finite_number,
    _rank_correlation,
)


FAMILY_SIZE = 10
_K = 2


class H2SecondaryInputError(H2ConfirmatoryInputError):
    """Raised when a structurally eligible secondary row violates integrity."""


@dataclass(frozen=True)
class _OutcomeSpec:
    name: str
    source_field: str
    horizon: int
    absolute: bool = False
    key_secondary: bool = False


_OUTCOMES = (
    _OutcomeSpec("absolute_return_pct_1", "absolute_return_pct_1", 1),
    _OutcomeSpec("absolute_return_pct_5", "absolute_return_pct_5", 5),
    _OutcomeSpec("absolute_return_pct_60", "absolute_return_pct_60", 60),
    _OutcomeSpec("abs(excess_return_pct_1)", "excess_return_pct_1", 1, True),
    _OutcomeSpec("abs(excess_return_pct_5)", "excess_return_pct_5", 5, True),
    _OutcomeSpec("abs(excess_return_pct_20)", "excess_return_pct_20", 20, True, True),
    _OutcomeSpec("abs(excess_return_pct_60)", "excess_return_pct_60", 60, True),
    _OutcomeSpec(
        "max_favorable_excursion_pct_20", "max_favorable_excursion_pct_20", 20
    ),
    _OutcomeSpec(
        "abs(max_adverse_excursion_pct_20)",
        "max_adverse_excursion_pct_20",
        20,
        True,
    ),
    _OutcomeSpec("realized_volatility_20", "realized_volatility_20", 20),
)


def analyze_h2a_secondary(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Analyze all ten pre-specified secondary outcomes without data fetching.

    Each outcome has its own horizon-specific EXACT-only sample and two fresh,
    independent PCG64 streams. Unavailable tests retain a result row with null
    raw p-value and q-value. BH always uses a family size of ten.
    """

    _validate_release_ids(rows)
    outcome_results: list[dict[str, object]] = []
    for spec in _OUTCOMES:
        sample = _sample_for_outcome(rows, spec)
        outcome_results.append(_analyze_outcome(sample, spec))
    _apply_bh(outcome_results)
    return {
        "protocol": PROTOCOL,
        "family_size": FAMILY_SIZE,
        "requested_bootstrap_reps": BOOTSTRAP_REPS,
        "bootstrap_seed": BOOTSTRAP_SEED,
        "python_version": platform.python_version(),
        "numpy_version": np.__version__,
        "pandas_version": pd.__version__,
        "outcomes": outcome_results,
    }


def _validate_release_ids(rows: Sequence[Mapping[str, object]]) -> None:
    release_ids: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise H2SecondaryInputError(f"row {index} must be a mapping")
        release_id = row.get("current_release_id")
        if not isinstance(release_id, str) or not release_id:
            raise H2SecondaryInputError(
                f"row {index} must have a non-empty current_release_id"
            )
        release_ids.append(release_id)
    duplicates = sorted(
        release_id
        for release_id, count in Counter(release_ids).items()
        if count > 1
    )
    if duplicates:
        raise H2SecondaryInputError(
            f"duplicate current_release_id: {', '.join(duplicates)}"
        )


def _sample_for_outcome(
    rows: Sequence[Mapping[str, object]], spec: _OutcomeSpec
) -> list[tuple[str, str, float, float]]:
    sample: list[tuple[str, str, float, float]] = []
    status_field = f"outcome_status_{spec.horizon}"
    for row in rows:
        if not (
            row.get("row_status") == "INCLUDED"
            and row.get("pairing_quality") == "EXACT"
            and row.get("h2_method") == "TFIDF_COSINE_V1"
            and row.get("h2_novelty") is not None
            and row.get("timing_ambiguous") is False
            and row.get("reference_resolution") != "AMBIGUOUS"
            and row.get(status_field) == "COMPLETE"
            and row.get(spec.source_field) is not None
        ):
            continue
        release_id = str(row["current_release_id"])
        ticker = row.get("ticker")
        if not isinstance(ticker, str) or not ticker:
            raise H2SecondaryInputError(
                f"selected release {release_id} has no ticker"
            )
        h2 = _validated_number(row["h2_novelty"], "h2_novelty", release_id)
        outcome = _validated_number(row[spec.source_field], spec.source_field, release_id)
        if spec.absolute:
            outcome = abs(outcome)
            if not math.isfinite(outcome):
                raise H2SecondaryInputError(
                    f"selected release {release_id} has non-finite transformed {spec.name}"
                )
        sample.append((ticker, release_id, h2, outcome))
    return sorted(sample, key=lambda item: (item[0], item[1]))


def _validated_number(value: object, field: str, release_id: str) -> float:
    try:
        return _finite_number(value, field)
    except (H2ConfirmatoryInputError, OverflowError) as error:
        raise H2SecondaryInputError(
            f"selected release {release_id}: {field} must be a finite number"
        ) from error


def _empty_result(spec: _OutcomeSpec, n: int, g: int) -> dict[str, object]:
    return {
        "outcome": spec.name,
        "source_field": spec.source_field,
        "horizon_sessions": spec.horizon,
        "key_secondary": spec.key_secondary,
        "rho": None,
        "observed_t": None,
        "raw_p_value": None,
        "q_value": None,
        "ci_low": None,
        "ci_high": None,
        "N": n,
        "n_tickers": g,
        "requested_bootstrap_reps": BOOTSTRAP_REPS,
        "wild_attempted_reps": 0,
        "wild_valid_reps": 0,
        "wild_invalid_reps": 0,
        "ci_attempted_reps": 0,
        "ci_valid_reps": 0,
        "ci_invalid_reps": 0,
        "analysis_status": "NOT_ESTIMABLE",
        "reason": None,
    }


def _analyze_outcome(
    sample: Sequence[tuple[str, str, float, float]], spec: _OutcomeSpec
) -> dict[str, object]:
    n = len(sample)
    tickers = sorted({item[0] for item in sample})
    g = len(tickers)
    result = _empty_result(spec, n, g)
    if g < 2:
        result["reason"] = "FEWER_THAN_TWO_TICKER_CLUSTERS"
        return result
    if n <= _K:
        result["reason"] = "N_NOT_GREATER_THAN_K"
        return result

    ticker_positions = {ticker: index for index, ticker in enumerate(tickers)}
    cluster_codes = np.asarray(
        [ticker_positions[item[0]] for item in sample], dtype=np.int64
    )
    x = np.asarray([item[2] for item in sample], dtype=np.float64)
    y = np.asarray([item[3] for item in sample], dtype=np.float64)
    x_rank = _average_ranks(x)
    y_rank = _average_ranks(y)
    if np.all(x_rank == x_rank[0]):
        result["reason"] = "CONSTANT_PREDICTOR_RANKS"
        return result
    if np.all(y_rank == y_rank[0]):
        result["reason"] = "CONSTANT_OUTCOME_RANKS"
        return result

    rho = _rank_correlation(x_rank, y_rank)
    result["rho"] = rho
    x_centered = x_rank - np.mean(x_rank)
    x_ss = float(np.dot(x_centered, x_centered))
    if not math.isfinite(x_ss) or x_ss == 0.0:
        result["reason"] = "SINGULAR_RANK_REGRESSION"
        return result
    correction = (g / (g - 1)) * ((n - 1) / (n - _K))
    observed_t = _cluster_t(x_centered, x_ss, y_rank, cluster_codes, g, correction)
    if observed_t is None:
        result["reason"] = "INVALID_OBSERVED_CLUSTER_STANDARD_ERROR"
        return result
    result["observed_t"] = observed_t

    wild_rng = np.random.Generator(np.random.PCG64(BOOTSTRAP_SEED))
    restricted_fitted = float(np.mean(y_rank))
    restricted_residuals = y_rank - restricted_fitted
    exceedances = 0
    wild_valid = 0
    for _ in range(BOOTSTRAP_REPS):
        draw = wild_rng.integers(0, 2, size=g, dtype=np.int64)
        weights = 2 * draw - 1
        y_star = restricted_fitted + weights[cluster_codes] * restricted_residuals
        t_star = _cluster_t(x_centered, x_ss, y_star, cluster_codes, g, correction)
        if t_star is not None:
            wild_valid += 1
            exceedances += int(abs(t_star) >= abs(observed_t))
    result["wild_attempted_reps"] = BOOTSTRAP_REPS
    result["wild_valid_reps"] = wild_valid
    result["wild_invalid_reps"] = BOOTSTRAP_REPS - wild_valid

    ci_rng = np.random.Generator(np.random.PCG64(BOOTSTRAP_SEED))
    indices_by_ticker = [np.flatnonzero(cluster_codes == index) for index in range(g)]
    valid_rhos: list[float] = []
    for _ in range(BOOTSTRAP_REPS):
        draw = ci_rng.integers(0, g, size=g, dtype=np.int64)
        sampled_indices = np.concatenate([indices_by_ticker[index] for index in draw])
        sampled_x_rank = _average_ranks(x[sampled_indices])
        sampled_y_rank = _average_ranks(y[sampled_indices])
        sampled_rho = _rank_correlation(sampled_x_rank, sampled_y_rank)
        if sampled_rho is not None:
            valid_rhos.append(sampled_rho)
    ci_valid = len(valid_rhos)
    result["ci_attempted_reps"] = BOOTSTRAP_REPS
    result["ci_valid_reps"] = ci_valid
    result["ci_invalid_reps"] = BOOTSTRAP_REPS - ci_valid

    reasons: list[str] = []
    if wild_valid < MIN_VALID_REPS:
        reasons.append("INSUFFICIENT_VALID_WILD_REPLICATES")
    else:
        result["raw_p_value"] = (1 + exceedances) / (1 + wild_valid)
    if ci_valid < MIN_VALID_REPS:
        reasons.append("INSUFFICIENT_VALID_CI_REPLICATES")
    else:
        bounds = np.quantile(valid_rhos, [0.025, 0.975], method="linear")
        result["ci_low"] = float(bounds[0])
        result["ci_high"] = float(bounds[1])
    if reasons:
        result["reason"] = "; ".join(reasons)
        return result

    result["analysis_status"] = "ESTIMABLE"
    return result


def _apply_bh(results: Sequence[dict[str, object]]) -> None:
    """Apply monotone BH to estimable raw p-values with frozen family m=10."""

    available = sorted(
        (
            result
            for result in results
            if result["raw_p_value"] is not None
        ),
        key=lambda result: (float(result["raw_p_value"]), str(result["outcome"])),
    )
    running_min = 1.0
    for rank in range(len(available), 0, -1):
        result = available[rank - 1]
        candidate = min(1.0, FAMILY_SIZE * float(result["raw_p_value"]) / rank)
        running_min = min(running_min, candidate)
        result["q_value"] = running_min
