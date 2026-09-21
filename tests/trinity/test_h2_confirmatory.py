"""Synthetic-only tests for the frozen H2A confirmatory primary analysis."""

from __future__ import annotations

import json
import math
import unittest
from unittest.mock import patch

import numpy as np

from trinity import h2_confirmatory as confirmatory


def _row(
    ticker: str,
    release_id: str,
    h2: float,
    outcome: float,
    **overrides: object,
) -> dict[str, object]:
    row: dict[str, object] = {
        "ticker": ticker,
        "current_release_id": release_id,
        "row_status": "INCLUDED",
        "pairing_quality": "EXACT",
        "h2_method": "TFIDF_COSINE_V1",
        "h2_novelty": h2,
        "outcome_status_20": "COMPLETE",
        "absolute_return_pct_20": outcome,
        "timing_ambiguous": False,
        "reference_resolution": "EVENT_SESSION_CLOSE",
    }
    row.update(overrides)
    return row


def _synthetic_rows(*, negative: bool = False, ties: bool = False) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for ticker_index in range(10):
        for release_index in range(3):
            position = ticker_index * 3 + release_index
            h2 = float(position // 2 if ties else position) / 35.0
            noise = ((ticker_index * 7 + release_index * 3) % 9 - 4) * 0.17
            outcome = (7.0 - 4.0 * h2 if negative else 1.0 + 4.0 * h2) + noise
            rows.append(
                _row(
                    f"T{ticker_index:02d}",
                    f"release-{position:02d}",
                    h2,
                    outcome,
                )
            )
    return rows


class H2ConfirmatoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.positive_rows = _synthetic_rows()
        cls.positive_result = confirmatory.analyze_h2a_primary(cls.positive_rows)
        cls.negative_result = confirmatory.analyze_h2a_primary(
            _synthetic_rows(negative=True)
        )

    def test_positive_synthetic_association(self) -> None:
        result = self.positive_result

        self.assertEqual(result["analysis_status"], "ESTIMABLE")
        self.assertGreater(result["rho"], 0)
        self.assertGreater(result["observed_t"], 0)
        self.assertGreaterEqual(result["primary_p_value"], 0)
        self.assertLessEqual(result["primary_p_value"], 1)
        self.assertEqual(result["N"], 30)
        self.assertEqual(result["n_tickers"], 10)

    def test_negative_synthetic_association(self) -> None:
        result = self.negative_result

        self.assertEqual(result["analysis_status"], "ESTIMABLE")
        self.assertLess(result["rho"], 0)
        self.assertLess(result["observed_t"], 0)
        self.assertGreater(result["primary_p_value"], 0.5)

    def test_average_ranks_for_ties(self) -> None:
        values = np.asarray([4.0, 2.0, 4.0, 2.0, 9.0])

        ranks = confirmatory._average_ranks(values)

        np.testing.assert_array_equal(ranks, [3.5, 1.5, 3.5, 1.5, 5.0])
        result = confirmatory.analyze_h2a_primary(_synthetic_rows(ties=True))
        self.assertEqual(result["analysis_status"], "ESTIMABLE")
        self.assertTrue(math.isfinite(result["rho"]))

    def test_multiple_releases_per_ticker_are_clustered(self) -> None:
        result = self.positive_result

        self.assertEqual(result["N"], 30)
        self.assertEqual(result["n_tickers"], 10)
        self.assertEqual(result["wild_attempted_reps"], 10_000)
        self.assertEqual(result["ci_attempted_reps"], 10_000)
        self.assertEqual(result["wild_valid_reps"] + result["wild_invalid_reps"], 10_000)
        self.assertEqual(result["ci_valid_reps"] + result["ci_invalid_reps"], 10_000)

    def test_constant_predictor_is_not_estimable(self) -> None:
        rows = _synthetic_rows()
        for row in rows:
            row["h2_novelty"] = 0.5

        result = confirmatory.analyze_h2a_primary(rows)

        self.assertEqual(result["analysis_status"], "NOT_ESTIMABLE")
        self.assertEqual(result["reason"], "CONSTANT_PREDICTOR_RANKS")
        self.assertIsNone(result["primary_p_value"])

    def test_constant_outcome_is_not_estimable(self) -> None:
        rows = _synthetic_rows()
        for row in rows:
            row["absolute_return_pct_20"] = 3.0

        result = confirmatory.analyze_h2a_primary(rows)

        self.assertEqual(result["analysis_status"], "NOT_ESTIMABLE")
        self.assertEqual(result["reason"], "CONSTANT_OUTCOME_RANKS")

    def test_single_cluster_is_not_estimable(self) -> None:
        rows = [_row("ONLY", f"r{i}", i / 10.0, float(i + 1)) for i in range(4)]

        result = confirmatory.analyze_h2a_primary(rows)

        self.assertEqual(result["analysis_status"], "NOT_ESTIMABLE")
        self.assertEqual(result["reason"], "FEWER_THAN_TWO_TICKER_CLUSTERS")

    def test_insufficient_dataset_is_not_estimable(self) -> None:
        rows = [_row("A", "a", 0.2, 1.0), _row("B", "b", 0.8, 2.0)]

        result = confirmatory.analyze_h2a_primary(rows)

        self.assertEqual(result["analysis_status"], "NOT_ESTIMABLE")
        self.assertEqual(result["reason"], "N_NOT_GREATER_THAN_K")

    def test_invalid_bootstrap_replicates_are_counted(self) -> None:
        actual_t = confirmatory._cluster_t
        actual_rho = confirmatory._rank_correlation
        t_calls = 0
        rho_calls = 0

        def _sometimes_invalid_t(*args: object) -> float | None:
            nonlocal t_calls
            t_calls += 1
            if 2 <= t_calls <= 601:
                return None
            return actual_t(*args)  # type: ignore[arg-type]

        def _sometimes_invalid_rho(*args: object) -> float | None:
            nonlocal rho_calls
            rho_calls += 1
            if 2 <= rho_calls <= 601:
                return None
            return actual_rho(*args)  # type: ignore[arg-type]

        with patch.object(confirmatory, "_cluster_t", side_effect=_sometimes_invalid_t), patch.object(
            confirmatory, "_rank_correlation", side_effect=_sometimes_invalid_rho
        ):
            result = confirmatory.analyze_h2a_primary(self.positive_rows)

        self.assertEqual(result["wild_attempted_reps"], 10_000)
        self.assertEqual(result["ci_attempted_reps"], 10_000)
        self.assertGreaterEqual(result["wild_invalid_reps"], 600)
        self.assertGreaterEqual(result["ci_invalid_reps"], 600)
        self.assertEqual(result["analysis_status"], "NOT_ESTIMABLE")
        self.assertIsNone(result["primary_p_value"])
        self.assertIsNone(result["ci_low"])
        self.assertIn("INSUFFICIENT_VALID_WILD_REPLICATES", result["reason"])
        self.assertIn("INSUFFICIENT_VALID_CI_REPLICATES", result["reason"])

    def test_same_seed_is_deterministic_and_row_order_irrelevant(self) -> None:
        repeated = confirmatory.analyze_h2a_primary(list(reversed(self.positive_rows)))
        expected = self.positive_result

        discrete_fields = (
            "N",
            "n_tickers",
            "requested_bootstrap_reps",
            "wild_attempted_reps",
            "wild_valid_reps",
            "wild_invalid_reps",
            "ci_attempted_reps",
            "ci_valid_reps",
            "ci_invalid_reps",
            "analysis_status",
            "reason",
        )
        for field in discrete_fields:
            self.assertEqual(repeated[field], expected[field], field)
        for field in ("rho", "observed_t", "primary_p_value", "ci_low", "ci_high"):
            self.assertAlmostEqual(repeated[field], expected[field], delta=1e-12)

    def test_primary_sample_excludes_fallback_ambiguous_and_pending(self) -> None:
        valid = _row("A", "valid", 0.2, 2.0)
        rows = [
            valid,
            _row("B", "fallback", 0.3, 3.0, pairing_quality="FALLBACK"),
            _row("C", "ambiguous", 0.4, 4.0, pairing_quality="AMBIGUOUS"),
            _row("D", "pending", 0.5, 5.0, outcome_status_20="PENDING"),
            _row("E", "unknown-timing", 0.6, 6.0, timing_ambiguous=True),
            _row("F", "excluded", 0.7, 7.0, row_status="EXCLUDED"),
            _row("G", "wrong-method", 0.8, 8.0, h2_method="OTHER"),
            _row("H", "missing-h2", 0.9, 9.0, h2_novelty=None),
            _row("I", "missing-outcome", 0.9, 9.0, absolute_return_pct_20=None),
            _row("J", "ambiguous-reference", 0.9, 9.0, reference_resolution="AMBIGUOUS"),
        ]

        sample = confirmatory.build_primary_sample(rows)

        self.assertEqual([row["current_release_id"] for row in sample], ["valid"])

    def test_cr1_matches_direct_sandwich_matrix(self) -> None:
        x = np.asarray([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        y = np.asarray([2.0, 1.0, 3.0, 5.0, 4.0, 7.0])
        cluster_codes = np.asarray([0, 0, 1, 1, 2, 2])
        n, g = len(x), 3
        design = np.column_stack((np.ones(n), x))
        coefficients = np.linalg.solve(design.T @ design, design.T @ y)
        residuals = y - design @ coefficients
        bread = np.linalg.inv(design.T @ design)
        meat = np.zeros((2, 2))
        for cluster in range(g):
            score = design[cluster_codes == cluster].T @ residuals[cluster_codes == cluster]
            meat += np.outer(score, score)
        correction = (g / (g - 1)) * ((n - 1) / (n - 2))
        direct_se = math.sqrt((correction * bread @ meat @ bread)[1, 1])

        observed = confirmatory._cluster_t(
            x - x.mean(),
            float(np.dot(x - x.mean(), x - x.mean())),
            y,
            cluster_codes,
            g,
            correction,
        )

        self.assertAlmostEqual(observed, coefficients[1] / direct_se, delta=1e-12)

    def test_result_records_versions_and_is_json_serializable(self) -> None:
        result = self.positive_result

        self.assertEqual(result["protocol"], "H2_PROTOCOL_V1")
        self.assertIsInstance(result["python_version"], str)
        self.assertIsInstance(result["numpy_version"], str)
        self.assertIsInstance(result["pandas_version"], str)
        self.assertIsInstance(json.dumps(result, sort_keys=True), str)


if __name__ == "__main__":
    unittest.main()
