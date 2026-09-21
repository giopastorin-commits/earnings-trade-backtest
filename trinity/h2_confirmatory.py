"""Confirmatory H2A primary analysis specified by H2_PROTOCOL_V1.

Only the frozen EXACT-only primary sample and primary inferential procedures
are implemented here. No secondary outcomes or trading decisions are computed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
import math
import platform

import numpy as np
import pandas as pd


PROTOCOL = "H2_PROTOCOL_V1"
BOOTSTRAP_REPS = 10_000
BOOTSTRAP_SEED = 20_260_918
MIN_VALID_REPS = 9_500
_K = 2


class H2ConfirmatoryInputError(ValueError):
    """Raised for invalid dataset rows, never for statistical non-estimability."""


def build_primary_sample(
    rows: Sequence[Mapping[str, object]],
) -> list[Mapping[str, object]]:
    """Select, without imputation, precisely the protocol's primary rows.

    The returned rows are sorted by ticker and current release ID so that
    input ordering cannot change floating-point summation or RNG assignment.
    """

    release_ids: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise H2ConfirmatoryInputError(f"row {index} must be a mapping")
        release_id = row.get("current_release_id")
        if not isinstance(release_id, str) or not release_id:
            raise H2ConfirmatoryInputError(
                f"row {index} must have a non-empty current_release_id"
            )
        release_ids.append(release_id)
    duplicates = sorted(
        release_id
        for release_id, count in Counter(release_ids).items()
        if count > 1
    )
    if duplicates:
        raise H2ConfirmatoryInputError(
            f"duplicate current_release_id: {', '.join(duplicates)}"
        )

    sample = [
        row
        for row in rows
        if row.get("row_status") == "INCLUDED"
        and row.get("pairing_quality") == "EXACT"
        and row.get("h2_method") == "TFIDF_COSINE_V1"
        and row.get("h2_novelty") is not None
        and row.get("outcome_status_20") == "COMPLETE"
        and row.get("absolute_return_pct_20") is not None
        and row.get("timing_ambiguous") is False
        and row.get("reference_resolution") != "AMBIGUOUS"
    ]
    for row in sample:
        ticker = row.get("ticker")
        if not isinstance(ticker, str) or not ticker:
            raise H2ConfirmatoryInputError(
                f"selected release {row['current_release_id']} has no ticker"
            )
        _finite_number(row["h2_novelty"], "h2_novelty")
        _finite_number(row["absolute_return_pct_20"], "absolute_return_pct_20")
    return sorted(sample, key=lambda row: (str(row["ticker"]), str(row["current_release_id"])))


def analyze_h2a_primary(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Run the exact primary H2A statistic, cluster p-value, and cluster CI.

    The two independent NumPy PCG64 streams both use seed 20260918. The
    requested 10,000 draws are fixed. An early non-estimable result reports zero
    attempted draws rather than incorrectly labeling unattempted draws invalid.
    """

    sample = build_primary_sample(rows)
    n = len(sample)
    tickers = sorted({str(row["ticker"]) for row in sample})
    g = len(tickers)
    result: dict[str, object] = {
        "protocol": PROTOCOL,
        "rho": None,
        "primary_p_value": None,
        "ci_low": None,
        "ci_high": None,
        "observed_t": None,
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
        "python_version": platform.python_version(),
        "numpy_version": np.__version__,
        "pandas_version": pd.__version__,
    }

    if g < 2:
        result["reason"] = "FEWER_THAN_TWO_TICKER_CLUSTERS"
        return result
    if n <= _K:
        result["reason"] = "N_NOT_GREATER_THAN_K"
        return result

    ticker_positions = {ticker: position for position, ticker in enumerate(tickers)}
    cluster_codes = np.asarray(
        [ticker_positions[str(row["ticker"])] for row in sample], dtype=np.int64
    )
    x = np.asarray([float(row["h2_novelty"]) for row in sample], dtype=np.float64)
    y = np.asarray(
        [float(row["absolute_return_pct_20"]) for row in sample], dtype=np.float64
    )
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
    observed_t = _cluster_t(
        x_centered, x_ss, y_rank, cluster_codes, g, correction
    )
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
            exceedances += int(t_star >= observed_t)
    result["wild_attempted_reps"] = BOOTSTRAP_REPS
    result["wild_valid_reps"] = wild_valid
    result["wild_invalid_reps"] = BOOTSTRAP_REPS - wild_valid
    if wild_valid >= MIN_VALID_REPS:
        result["primary_p_value"] = (1 + exceedances) / (1 + wild_valid)

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
    if ci_valid >= MIN_VALID_REPS:
        bounds = np.quantile(valid_rhos, [0.025, 0.975], method="linear")
        result["ci_low"] = float(bounds[0])
        result["ci_high"] = float(bounds[1])

    reasons: list[str] = []
    if wild_valid < MIN_VALID_REPS:
        reasons.append("INSUFFICIENT_VALID_WILD_REPLICATES")
    if ci_valid < MIN_VALID_REPS:
        reasons.append("INSUFFICIENT_VALID_CI_REPLICATES")
    if reasons:
        result["reason"] = "; ".join(reasons)
    else:
        result["analysis_status"] = "ESTIMABLE"
    return result


def _finite_number(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise H2ConfirmatoryInputError(f"{field} must be a finite number")
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise H2ConfirmatoryInputError(f"{field} must be a finite number") from error
    if not math.isfinite(number):
        raise H2ConfirmatoryInputError(f"{field} must be a finite number")
    return number


def _average_ranks(values: np.ndarray) -> np.ndarray:
    """Return 1-based average ranks, preserving exact equality ties."""

    order = np.argsort(values, kind="stable")
    sorted_values = values[order]
    group_starts = np.flatnonzero(
        np.concatenate(([True], sorted_values[1:] != sorted_values[:-1]))
    )
    group_ends = np.concatenate((group_starts[1:], [len(values)]))
    rank_by_group = (group_starts + group_ends + 1) / 2.0
    group_for_position = np.repeat(
        np.arange(len(group_starts)), group_ends - group_starts
    )
    ranks = np.empty(len(values), dtype=np.float64)
    ranks[order] = rank_by_group[group_for_position]
    return ranks


def _rank_correlation(x_rank: np.ndarray, y_rank: np.ndarray) -> float | None:
    x_centered = x_rank - np.mean(x_rank)
    y_centered = y_rank - np.mean(y_rank)
    denominator = math.sqrt(
        float(np.dot(x_centered, x_centered))
        * float(np.dot(y_centered, y_centered))
    )
    if denominator == 0.0 or not math.isfinite(denominator):
        return None
    rho = float(np.dot(x_centered, y_centered) / denominator)
    return max(-1.0, min(1.0, rho)) if math.isfinite(rho) else None


def _cluster_t(
    x_centered: np.ndarray,
    x_ss: float,
    y_values: np.ndarray,
    cluster_codes: np.ndarray,
    g: int,
    correction: float,
) -> float | None:
    """OLS slope t-statistic using the two-parameter ticker-clustered CR1 SE."""

    y_centered = y_values - np.mean(y_values)
    beta = float(np.dot(x_centered, y_centered) / x_ss)
    residuals = y_centered - beta * x_centered
    scores = np.bincount(
        cluster_codes, weights=x_centered * residuals, minlength=g
    )
    variance = correction * float(np.dot(scores, scores)) / (x_ss * x_ss)
    if not math.isfinite(variance) or variance <= 0.0:
        return None
    standard_error = math.sqrt(variance)
    t_value = beta / standard_error
    return float(t_value) if math.isfinite(t_value) else None
