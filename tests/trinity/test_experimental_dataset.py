"""Tests for the TRINITY/H2 experimental dataset builder."""

from __future__ import annotations

from datetime import date, timedelta
import json
import unittest

from trinity.experimental_dataset import build_experimental_dataset


def _release(
    release_id: str,
    release_at: str,
    fiscal_year: int | None,
    fiscal_quarter: int | None,
    text: str | None = "Revenue increased while operating margin improved.",
    *,
    ticker: str = "ACME",
    period_type: str = "QUARTERLY",
    document_type: str = "EARNINGS_RELEASE",
) -> dict[str, object]:
    record: dict[str, object] = {
        "ticker": ticker,
        "release_id": release_id,
        "release_at": release_at,
        "fiscal_year": fiscal_year,
        "fiscal_quarter": fiscal_quarter,
        "period_type": period_type,
        "document_type": document_type,
    }
    if text is not None:
        record["text"] = text
    return record


def _weekdays(start: date, count: int) -> list[date]:
    sessions: list[date] = []
    current = start
    while len(sessions) < count:
        if current.weekday() < 5:
            sessions.append(current)
        current += timedelta(days=1)
    return sessions


def _bars(count: int = 11) -> list[dict[str, object]]:
    return [
        {
            "date": session.isoformat(),
            "open": 99.5 + index,
            "high": 101.0 + index,
            "low": 99.0 + index,
            "close": 100.0 + index,
            "volume": 1_000.0 + index * 10,
        }
        for index, session in enumerate(_weekdays(date(2024, 1, 1), count))
    ]


def _exact_releases() -> list[dict[str, object]]:
    return [
        _release(
            "previous",
            "2023-10-20",
            2023,
            4,
            "Revenue increased ten percent and margins improved.",
        ),
        _release(
            "current",
            "2024-01-08",
            2024,
            1,
            "Revenue increased twelve percent and margins improved.",
        ),
    ]


def _build(
    releases: list[dict[str, object]] | None = None,
    *,
    bars: dict[str, list[dict[str, object]]] | None = None,
    benchmark: list[dict[str, object]] | None = None,
    timing: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    return build_experimental_dataset(
        releases or _exact_releases(),
        bars if bars is not None else {"ACME": _bars()},
        benchmark_ohlcv=benchmark,
        timing_by_release_id=timing or {"current": "AFTER_MARKET"},
    )


def _row(rows: list[dict[str, object]], release_id: str) -> dict[str, object]:
    return next(row for row in rows if row["current_release_id"] == release_id)


class ExperimentalDatasetTests(unittest.TestCase):
    def test_exact_pairing_builds_complete_h2_row(self) -> None:
        current = _row(_build(), "current")

        self.assertEqual(current["pairing_status"], "MATCHED")
        self.assertEqual(current["pairing_quality"], "EXACT")
        self.assertEqual(current["previous_release_id"], "previous")
        self.assertIsInstance(current["h2_novelty"], float)
        self.assertEqual(current["h2_method"], "TFIDF_COSINE_V1")
        self.assertEqual(current["row_status"], "INCLUDED")

    def test_fallback_pairing_is_retained_and_marked(self) -> None:
        releases = _exact_releases()
        releases[0]["fiscal_quarter"] = None
        releases[1]["fiscal_quarter"] = None
        current = _row(_build(releases), "current")

        self.assertEqual(current["pairing_status"], "MATCHED")
        self.assertEqual(current["pairing_quality"], "FALLBACK")
        self.assertTrue(current["fallback_used"])
        self.assertIsNotNone(current["h2_novelty"])

    def test_no_match_is_retained_with_null_h2(self) -> None:
        release = _release("only", "2024-01-08", 2024, 1)
        row = _row(_build([release], timing={"only": "UNKNOWN"}), "only")

        self.assertEqual(row["pairing_status"], "NO_MATCH")
        self.assertEqual(row["pairing_quality"], "NO_MATCH")
        self.assertIsNone(row["h2_novelty"])
        self.assertEqual(row["row_status"], "EXCLUDED")
        self.assertIn("PAIRING_NO_MATCH", str(row["exclusion_reason"]))

    def test_ambiguous_pair_is_retained_with_null_h2(self) -> None:
        releases = [
            _release("previous-a", "2023-10-20", 2023, 4),
            _release("previous-b", "2023-10-20", 2023, 4),
            _release("current", "2024-01-08", 2024, 1),
        ]
        row = _row(_build(releases), "current")

        self.assertEqual(row["pairing_status"], "AMBIGUOUS")
        self.assertEqual(row["pairing_quality"], "AMBIGUOUS")
        self.assertIsNone(row["h2_similarity"])
        self.assertIn("PAIRING_AMBIGUOUS", str(row["exclusion_reason"]))

    def test_h2_is_calculated_only_for_matched_rows(self) -> None:
        rows = _build()

        self.assertIsNotNone(_row(rows, "current")["h2_novelty"])
        self.assertIsNone(_row(rows, "previous")["h2_novelty"])

    def test_partial_outcomes_are_flattened(self) -> None:
        row = _row(_build(), "current")

        self.assertEqual(row["outcome_status_1"], "COMPLETE")
        self.assertEqual(row["outcome_status_5"], "COMPLETE")
        self.assertEqual(row["outcome_status_20"], "PENDING")
        self.assertEqual(row["outcome_status_60"], "PENDING")
        self.assertIsNone(row["raw_return_pct_20"])

    def test_benchmark_metrics_are_present_when_benchmark_is_supplied(self) -> None:
        benchmark = [
            {"date": bar["date"], "close": 400.0 + index}
            for index, bar in enumerate(_bars())
        ]
        row = _row(_build(benchmark=benchmark), "current")

        self.assertIsNotNone(row["benchmark_return_pct_1"])
        self.assertIsNotNone(row["excess_return_pct_1"])

    def test_benchmark_metrics_are_null_when_benchmark_is_absent(self) -> None:
        row = _row(_build(), "current")

        self.assertIsNone(row["benchmark_return_pct_1"])
        self.assertIsNone(row["excess_return_pct_1"])

    def test_unknown_timing_is_auditable_and_pending(self) -> None:
        row = _row(_build(timing={"current": "UNKNOWN"}), "current")

        self.assertEqual(row["event_timing"], "UNKNOWN")
        self.assertTrue(row["timing_ambiguous"])
        self.assertEqual(row["reference_resolution"], "AMBIGUOUS")
        self.assertEqual(row["outcome_status_1"], "PENDING")

    def test_missing_ticker_ohlcv_excludes_only_affected_row(self) -> None:
        row = _row(_build(bars={}), "current")

        self.assertEqual(row["row_status"], "EXCLUDED")
        self.assertIn("MISSING_OHLCV", str(row["exclusion_reason"]))
        self.assertIsNone(row["outcome_status_1"])

    def test_missing_current_text_excludes_row_without_crashing(self) -> None:
        releases = _exact_releases()
        releases[1].pop("text")
        row = _row(_build(releases), "current")

        self.assertEqual(row["row_status"], "EXCLUDED")
        self.assertIsNone(row["h2_novelty"])
        self.assertIn("H2_INPUT_ERROR", str(row["exclusion_reason"]))

    def test_missing_previous_text_excludes_row_without_crashing(self) -> None:
        releases = _exact_releases()
        releases[0].pop("text")
        row = _row(_build(releases), "current")

        self.assertEqual(row["row_status"], "EXCLUDED")
        self.assertIsNone(row["h2_novelty"])
        self.assertIn("H2_INPUT_ERROR", str(row["exclusion_reason"]))

    def test_input_order_does_not_change_result(self) -> None:
        releases = _exact_releases()

        first = _build(releases)
        second = _build(list(reversed(releases)))

        self.assertEqual(first, second)

    def test_output_has_one_row_per_release(self) -> None:
        releases = _exact_releases()
        rows = _build(releases)

        ids = [row["current_release_id"] for row in rows]
        self.assertEqual(len(rows), len(releases))
        self.assertEqual(len(ids), len(set(ids)))

    def test_future_release_does_not_influence_current_pair(self) -> None:
        releases = _exact_releases()
        baseline = _row(_build(releases), "current")
        future = _release("future", "2024-04-20", 2024, 2)

        with_future = _row(_build([future, *releases]), "current")

        self.assertEqual(baseline, with_future)

    def test_output_is_json_serializable(self) -> None:
        rows = _build()

        encoded = json.dumps(rows, sort_keys=True)

        self.assertIsInstance(encoded, str)
        self.assertIn('"h2_method": "TFIDF_COSINE_V1"', encoded)


if __name__ == "__main__":
    unittest.main()
