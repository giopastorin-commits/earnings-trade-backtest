"""Synthetic-only checks for the frozen H2 V1 secondary analyses."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import numpy as np

from trinity import h2_secondary as secondary


def _rows(*, ties: bool = False) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for ticker_index in range(10):
        for release_index in range(3):
            index = ticker_index * 3 + release_index
            h2 = (index // 2 if ties else index) / 35.0
            noise = ((ticker_index * 7 + release_index * 3) % 9 - 4) * 0.13
            rows.append(
                {
                    "ticker": f"T{ticker_index:02d}",
                    "current_release_id": f"r{index:02d}",
                    "row_status": "INCLUDED",
                    "pairing_quality": "EXACT",
                    "h2_method": "TFIDF_COSINE_V1",
                    "h2_novelty": h2,
                    "timing_ambiguous": False,
                    "reference_resolution": "EVENT_SESSION_CLOSE",
                    "outcome_status_1": "COMPLETE",
                    "outcome_status_5": "COMPLETE",
                    "outcome_status_20": "COMPLETE",
                    "outcome_status_60": "COMPLETE",
                    "absolute_return_pct_1": 1.0 + 4 * h2 + noise,
                    "absolute_return_pct_5": 7.0 - 4 * h2 + noise,
                    "absolute_return_pct_60": 2.0 + 5 * h2 + noise,
                    "excess_return_pct_1": (-1) ** index * (1.0 + 2 * h2 + noise),
                    "excess_return_pct_5": (-1) ** index * (2.0 + 2 * h2 + noise),
                    "excess_return_pct_20": (-1) ** index * (3.0 + 2 * h2 + noise),
                    "excess_return_pct_60": (-1) ** index * (4.0 + 2 * h2 + noise),
                    "max_favorable_excursion_pct_20": 2.0 + 3 * h2 + noise,
                    "max_adverse_excursion_pct_20": -(1.0 + 2 * h2 + noise),
                    "realized_volatility_20": 5.0 + 6 * h2 + noise,
                }
            )
    return rows


def _result(analysis: dict[str, object], outcome: str) -> dict[str, object]:
    return next(
        item for item in analysis["outcomes"] if item["outcome"] == outcome  # type: ignore[union-attr]
    )


def _spec(outcome: str) -> secondary._OutcomeSpec:
    return next(spec for spec in secondary._OUTCOMES if spec.name == outcome)


class H2SecondaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.synthetic_rows = _rows()
        cls.analysis = secondary.analyze_h2a_secondary(cls.synthetic_rows)

    def test_all_ten_outcomes_are_estimable_and_bh_adjusted(self) -> None:
        analysis = self.analysis
        outcomes = analysis["outcomes"]

        self.assertEqual(analysis["family_size"], 10)
        self.assertEqual(len(outcomes), 10)
        self.assertTrue(all(row["analysis_status"] == "ESTIMABLE" for row in outcomes))
        self.assertTrue(all(row["raw_p_value"] is not None for row in outcomes))
        self.assertTrue(all(row["q_value"] is not None for row in outcomes))
        self.assertTrue(all(row["wild_attempted_reps"] == 10_000 for row in outcomes))
        self.assertTrue(all(row["ci_attempted_reps"] == 10_000 for row in outcomes))

    def test_positive_secondary_association(self) -> None:
        outcome = _result(self.analysis, "absolute_return_pct_1")

        self.assertGreater(outcome["rho"], 0)
        self.assertGreater(outcome["observed_t"], 0)
        self.assertGreaterEqual(outcome["raw_p_value"], 0)
        self.assertLessEqual(outcome["raw_p_value"], 1)

    def test_negative_secondary_association(self) -> None:
        outcome = _result(self.analysis, "absolute_return_pct_5")

        self.assertLess(outcome["rho"], 0)
        self.assertLess(outcome["observed_t"], 0)
        self.assertGreaterEqual(outcome["raw_p_value"], 0)
        self.assertLessEqual(outcome["raw_p_value"], 1)

    def test_ties_and_repeated_ticker_releases(self) -> None:
        sample = secondary._sample_for_outcome(
            _rows(ties=True), _spec("absolute_return_pct_1")
        )
        x = np.asarray([row[2] for row in sample])
        ranks = secondary._average_ranks(x)

        self.assertEqual(len(sample), 30)
        self.assertEqual(len({row[0] for row in sample}), 10)
        self.assertEqual(ranks[0], ranks[1])
        self.assertEqual(ranks[0], 1.5)
        result = secondary._analyze_outcome(sample, _spec("absolute_return_pct_1"))
        self.assertEqual(result["analysis_status"], "ESTIMABLE")

    def test_constant_h2_is_not_estimable(self) -> None:
        rows = _rows()
        for row in rows:
            row["h2_novelty"] = 0.5
        result = secondary._analyze_outcome(
            secondary._sample_for_outcome(rows, _spec("absolute_return_pct_1")),
            _spec("absolute_return_pct_1"),
        )

        self.assertEqual(result["analysis_status"], "NOT_ESTIMABLE")
        self.assertEqual(result["reason"], "CONSTANT_PREDICTOR_RANKS")

    def test_constant_outcome_is_not_estimable(self) -> None:
        rows = _rows()
        for row in rows:
            row["absolute_return_pct_1"] = 1.0
        spec = _spec("absolute_return_pct_1")
        result = secondary._analyze_outcome(secondary._sample_for_outcome(rows, spec), spec)

        self.assertEqual(result["analysis_status"], "NOT_ESTIMABLE")
        self.assertEqual(result["reason"], "CONSTANT_OUTCOME_RANKS")

    def test_single_cluster_is_not_estimable(self) -> None:
        spec = _spec("absolute_return_pct_1")
        rows = [row for row in _rows() if row["ticker"] == "T00"]
        result = secondary._analyze_outcome(secondary._sample_for_outcome(rows, spec), spec)

        self.assertEqual(result["analysis_status"], "NOT_ESTIMABLE")
        self.assertEqual(result["reason"], "FEWER_THAN_TWO_TICKER_CLUSTERS")

    def test_insufficient_dataset_is_not_estimable(self) -> None:
        spec = _spec("absolute_return_pct_1")
        rows = [row for row in _rows() if row["current_release_id"] in ("r00", "r03")]
        result = secondary._analyze_outcome(secondary._sample_for_outcome(rows, spec), spec)

        self.assertEqual(result["analysis_status"], "NOT_ESTIMABLE")
        self.assertEqual(result["reason"], "N_NOT_GREATER_THAN_K")

    def test_invalid_bootstrap_replicates_are_counted(self) -> None:
        actual_t = secondary._cluster_t
        actual_rho = secondary._rank_correlation
        t_calls = 0
        rho_calls = 0

        def sometimes_invalid_t(*args: object) -> float | None:
            nonlocal t_calls
            t_calls += 1
            return None if 2 <= t_calls <= 601 else actual_t(*args)  # type: ignore[arg-type]

        def sometimes_invalid_rho(*args: object) -> float | None:
            nonlocal rho_calls
            rho_calls += 1
            return None if 2 <= rho_calls <= 601 else actual_rho(*args)  # type: ignore[arg-type]

        spec = _spec("absolute_return_pct_1")
        sample = secondary._sample_for_outcome(self.synthetic_rows, spec)
        with patch.object(secondary, "_cluster_t", side_effect=sometimes_invalid_t), patch.object(
            secondary, "_rank_correlation", side_effect=sometimes_invalid_rho
        ):
            result = secondary._analyze_outcome(sample, spec)

        self.assertEqual(result["wild_attempted_reps"], 10_000)
        self.assertEqual(result["ci_attempted_reps"], 10_000)
        self.assertGreaterEqual(result["wild_invalid_reps"], 600)
        self.assertGreaterEqual(result["ci_invalid_reps"], 600)
        self.assertEqual(result["analysis_status"], "NOT_ESTIMABLE")
        self.assertIsNone(result["raw_p_value"])
        self.assertIsNone(result["q_value"])
        self.assertIn("INSUFFICIENT_VALID_WILD_REPLICATES", result["reason"])
        self.assertIn("INSUFFICIENT_VALID_CI_REPLICATES", result["reason"])

    def test_valid_wild_p_is_kept_and_enters_bh_when_ci_is_invalid(self) -> None:
        actual_rho = secondary._rank_correlation
        rho_calls = 0

        def sometimes_invalid_rho(*args: object) -> float | None:
            nonlocal rho_calls
            rho_calls += 1
            return None if 2 <= rho_calls <= 601 else actual_rho(*args)  # type: ignore[arg-type]

        spec = _spec("absolute_return_pct_1")
        sample = secondary._sample_for_outcome(self.synthetic_rows, spec)
        with patch.object(secondary, "_rank_correlation", side_effect=sometimes_invalid_rho):
            result = secondary._analyze_outcome(sample, spec)

        self.assertEqual(result["wild_valid_reps"], 10_000)
        self.assertEqual(result["ci_valid_reps"], 9_400)
        self.assertEqual(result["analysis_status"], "NOT_ESTIMABLE")
        self.assertIsNotNone(result["raw_p_value"])
        self.assertIsNone(result["ci_low"])
        self.assertIsNone(result["ci_high"])
        self.assertEqual(result["reason"], "INSUFFICIENT_VALID_CI_REPLICATES")

        family = [result] + [
            {"outcome": f"missing_{i}", "raw_p_value": None, "q_value": None}
            for i in range(9)
        ]
        secondary._apply_bh(family)
        self.assertEqual(result["q_value"], min(1.0, 10 * result["raw_p_value"]))

    def test_valid_ci_is_kept_when_wild_p_is_invalid(self) -> None:
        actual_t = secondary._cluster_t
        t_calls = 0

        def sometimes_invalid_t(*args: object) -> float | None:
            nonlocal t_calls
            t_calls += 1
            return None if 2 <= t_calls <= 601 else actual_t(*args)  # type: ignore[arg-type]

        spec = _spec("absolute_return_pct_1")
        sample = secondary._sample_for_outcome(self.synthetic_rows, spec)
        with patch.object(secondary, "_cluster_t", side_effect=sometimes_invalid_t):
            result = secondary._analyze_outcome(sample, spec)

        self.assertEqual(result["wild_valid_reps"], 9_400)
        self.assertGreaterEqual(result["ci_valid_reps"], 9_500)
        self.assertEqual(result["analysis_status"], "NOT_ESTIMABLE")
        self.assertIsNone(result["raw_p_value"])
        self.assertIsNotNone(result["ci_low"])
        self.assertIsNotNone(result["ci_high"])
        self.assertEqual(result["reason"], "INSUFFICIENT_VALID_WILD_REPLICATES")

        secondary._apply_bh([result])
        self.assertIsNone(result["q_value"])

    def test_two_sided_p_counts_negative_extreme_t(self) -> None:
        spec = _spec("absolute_return_pct_1")
        sample = secondary._sample_for_outcome(self.synthetic_rows, spec)
        calls = 0

        def controlled_t(*args: object) -> float:
            nonlocal calls
            calls += 1
            return 2.0 if calls == 1 else (-3.0 if calls <= 1_001 else 0.0)

        with patch.object(secondary, "_cluster_t", side_effect=controlled_t):
            result = secondary._analyze_outcome(sample, spec)

        self.assertEqual(result["analysis_status"], "ESTIMABLE")
        self.assertEqual(result["raw_p_value"], 1_001 / 10_001)

    def test_input_order_does_not_change_results(self) -> None:
        repeated = secondary.analyze_h2a_secondary(list(reversed(self.synthetic_rows)))
        first = self.analysis

        self.assertEqual(repeated["family_size"], first["family_size"])
        for left, right in zip(first["outcomes"], repeated["outcomes"]):
            for key in (
                "outcome", "N", "n_tickers", "wild_valid_reps", "wild_invalid_reps",
                "ci_valid_reps", "ci_invalid_reps", "analysis_status", "reason",
            ):
                self.assertEqual(left[key], right[key], key)
            for key in ("rho", "observed_t", "raw_p_value", "ci_low", "ci_high", "q_value"):
                self.assertAlmostEqual(left[key], right[key], delta=1e-12)

    def test_abs_excess_returns_are_transformed_only_after_validation(self) -> None:
        rows = _rows()
        rows[0]["excess_return_pct_1"] = -2.5
        rows[1]["excess_return_pct_20"] = -3.5

        sample_1 = secondary._sample_for_outcome(rows, _spec("abs(excess_return_pct_1)"))
        sample_20 = secondary._sample_for_outcome(rows, _spec("abs(excess_return_pct_20)"))

        self.assertEqual(sample_1[0][3], 2.5)
        self.assertEqual(sample_20[1][3], 3.5)

    def test_abs_adverse_excursion_is_transformed(self) -> None:
        rows = _rows()
        rows[0]["max_adverse_excursion_pct_20"] = -8.25

        sample = secondary._sample_for_outcome(
            rows, _spec("abs(max_adverse_excursion_pct_20)")
        )

        self.assertEqual(sample[0][3], 8.25)

    def test_none_is_excluded_only_for_relevant_outcome(self) -> None:
        rows = _rows()
        rows[0]["excess_return_pct_1"] = None
        rows[1]["h2_novelty"] = None
        rows[2]["outcome_status_20"] = "PENDING"

        sample_1 = secondary._sample_for_outcome(rows, _spec("abs(excess_return_pct_1)"))
        sample_5 = secondary._sample_for_outcome(rows, _spec("absolute_return_pct_5"))
        sample_20 = secondary._sample_for_outcome(rows, _spec("abs(excess_return_pct_20)"))

        self.assertEqual(len(sample_1), 28)
        self.assertEqual(len(sample_5), 29)
        self.assertEqual(len(sample_20), 28)

    def test_nonfinite_or_non_numeric_selected_values_raise_input_error(self) -> None:
        spec = _spec("abs(excess_return_pct_1)")
        for field, bad in (
            ("h2_novelty", float("nan")),
            ("h2_novelty", float("inf")),
            ("h2_novelty", True),
            ("h2_novelty", "not-a-number"),
            ("h2_novelty", 10**400),
            ("excess_return_pct_1", float("nan")),
            ("excess_return_pct_1", -float("inf")),
            ("excess_return_pct_1", False),
        ):
            with self.subTest(field=field, bad=str(bad)):
                rows = _rows()
                rows[0][field] = bad
                with self.assertRaises(secondary.H2SecondaryInputError):
                    secondary._sample_for_outcome(rows, spec)

    def test_invalid_value_outside_other_eligibility_does_not_raise(self) -> None:
        rows = _rows()
        rows[0]["h2_novelty"] = float("nan")
        rows[0]["outcome_status_1"] = "PENDING"

        sample = secondary._sample_for_outcome(
            rows, _spec("absolute_return_pct_1")
        )

        self.assertEqual(len(sample), 29)

    def test_bh_keeps_family_ten_when_some_are_not_estimable(self) -> None:
        results = [
            {"outcome": f"o{i}", "analysis_status": "NOT_ESTIMABLE", "raw_p_value": None, "q_value": None}
            for i in range(10)
        ]
        results[0].update(analysis_status="ESTIMABLE", raw_p_value=0.01)
        results[1].update(analysis_status="ESTIMABLE", raw_p_value=0.04)

        secondary._apply_bh(results)

        self.assertAlmostEqual(results[0]["q_value"], 0.1)
        self.assertAlmostEqual(results[1]["q_value"], 0.2)
        self.assertTrue(all(row["q_value"] is None for row in results[2:]))

    def test_bh_all_ten_and_monotonicity(self) -> None:
        results = [
            {"outcome": f"o{i}", "analysis_status": "ESTIMABLE", "raw_p_value": p, "q_value": None}
            for i, p in enumerate((0.001, 0.03, 0.02, 0.08, 0.05, 0.4, 0.6, 0.7, 0.9, 1.0))
        ]

        secondary._apply_bh(results)

        ordered = sorted(results, key=lambda row: row["raw_p_value"])
        self.assertEqual(len(ordered), 10)
        self.assertAlmostEqual(ordered[0]["q_value"], 0.01)
        self.assertTrue(all(a["q_value"] <= b["q_value"] for a, b in zip(ordered, ordered[1:])))
        self.assertTrue(all(0 <= row["q_value"] <= 1 for row in results))

    def test_key_secondary_and_versions_are_reported(self) -> None:
        result = self.analysis
        key_outcomes = [row for row in result["outcomes"] if row["key_secondary"]]

        self.assertEqual(len(key_outcomes), 1)
        self.assertEqual(key_outcomes[0]["outcome"], "abs(excess_return_pct_20)")
        self.assertEqual(result["protocol"], "H2_PROTOCOL_V1")
        self.assertIsInstance(result["python_version"], str)
        self.assertIsInstance(result["numpy_version"], str)
        self.assertIsInstance(result["pandas_version"], str)
        self.assertIsInstance(json.dumps(result), str)


if __name__ == "__main__":
    unittest.main()
