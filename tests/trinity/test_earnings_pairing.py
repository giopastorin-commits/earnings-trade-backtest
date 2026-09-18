"""Tests for deterministic earnings-release pairing."""

from __future__ import annotations

import unittest

from trinity.earnings_pairing import (
    EarningsPairingInputError,
    pair_earnings_release,
)


def _release(
    release_id: str,
    release_at: str,
    fiscal_year: int | None,
    fiscal_quarter: int | None,
    *,
    ticker: str = "ACME",
    period_type: str = "QUARTERLY",
    document_type: str = "EARNINGS_RELEASE",
) -> dict[str, object]:
    return {
        "ticker": ticker,
        "release_id": release_id,
        "release_at": release_at,
        "fiscal_year": fiscal_year,
        "fiscal_quarter": fiscal_quarter,
        "period_type": period_type,
        "document_type": document_type,
    }


class EarningsPairingTests(unittest.TestCase):
    def test_q2_selects_q1_of_same_year(self) -> None:
        current = _release("q2-2026", "2026-07-20", 2026, 2)
        history = [
            _release("q2-2025", "2025-07-20", 2025, 2),
            _release("q1-2026", "2026-04-20", 2026, 1),
        ]

        result = pair_earnings_release(current, history)

        self.assertEqual(result["previous_release_id"], "q1-2026")
        self.assertEqual(result["pairing_method"], "EXACT_PREVIOUS_FISCAL_PERIOD")
        self.assertEqual(result["fiscal_continuity"], "EXACT")

    def test_q1_selects_q4_across_fiscal_year(self) -> None:
        current = _release("q1-2026", "2026-04-20", 2026, 1)
        history = [_release("q4-2025", "2026-01-20", 2025, 4)]

        result = pair_earnings_release(current, history)

        self.assertEqual(result["previous_release_id"], "q4-2025")
        self.assertEqual(result["fiscal_continuity"], "EXACT")

    def test_annual_selects_previous_annual(self) -> None:
        current = _release(
            "fy-2026", "2027-02-10", 2026, None, period_type="ANNUAL"
        )
        history = [
            _release("fy-2025", "2026-02-10", 2025, None, period_type="ANNUAL")
        ]

        result = pair_earnings_release(current, history)

        self.assertEqual(result["previous_release_id"], "fy-2025")
        self.assertEqual(result["pairing_method"], "EXACT_PREVIOUS_FISCAL_PERIOD")

    def test_annual_does_not_select_quarterly(self) -> None:
        current = _release(
            "fy-2026", "2027-02-10", 2026, None, period_type="ANNUAL"
        )
        history = [_release("q4-2026", "2027-01-20", 2026, 4)]

        result = pair_earnings_release(current, history)

        self.assertEqual(result["pairing_status"], "NO_MATCH")

    def test_transcript_does_not_select_earnings_release(self) -> None:
        current = _release(
            "transcript", "2026-07-20", 2026, 2, document_type="TRANSCRIPT"
        )
        history = [_release("release", "2026-04-20", 2026, 1)]

        self.assertEqual(
            pair_earnings_release(current, history)["pairing_status"], "NO_MATCH"
        )

    def test_earnings_release_does_not_select_10q(self) -> None:
        current = _release("release", "2026-07-20", 2026, 2)
        history = [
            _release("10q", "2026-04-20", 2026, 1, document_type="10-Q")
        ]

        self.assertEqual(
            pair_earnings_release(current, history)["pairing_status"], "NO_MATCH"
        )

    def test_chronological_fallback_when_quarter_is_missing(self) -> None:
        current = _release("current", "2026-07-20", 2026, None)
        history = [
            _release("older", "2026-01-20", 2025, None),
            _release("latest", "2026-04-20", 2026, None),
        ]

        result = pair_earnings_release(current, history)

        self.assertEqual(result["previous_release_id"], "latest")
        self.assertEqual(result["pairing_method"], "CHRONOLOGICAL_COMPATIBLE_FALLBACK")
        self.assertEqual(result["fiscal_continuity"], "UNKNOWN")
        self.assertTrue(result["fallback_used"])

    def test_no_previous_compatible_returns_no_match(self) -> None:
        current = _release("current", "2026-07-20", 2026, 2)

        result = pair_earnings_release(current, [])

        self.assertEqual(result["pairing_status"], "NO_MATCH")
        self.assertEqual(result["pairing_method"], "NONE")
        self.assertIsNone(result["previous_release_id"])

    def test_other_tickers_are_excluded(self) -> None:
        current = _release("current", "2026-07-20", 2026, 2)
        history = [
            _release("other", "2026-04-20", 2026, 1, ticker="OTHER")
        ]

        result = pair_earnings_release(current, history)

        self.assertEqual(result["pairing_status"], "NO_MATCH")
        self.assertEqual(result["candidate_count"], 0)

    def test_future_document_is_excluded(self) -> None:
        current = _release("current", "2026-07-20", 2026, 2)
        history = [_release("future", "2026-10-20", 2026, 3)]

        result = pair_earnings_release(current, history)

        self.assertEqual(result["pairing_status"], "NO_MATCH")
        self.assertEqual(result["candidate_count"], 0)

    def test_duplicate_release_id_raises(self) -> None:
        current = _release("current", "2026-07-20", 2026, 2)
        duplicate = _release("duplicate", "2026-04-20", 2026, 1)

        with self.assertRaisesRegex(EarningsPairingInputError, "duplicate release_id"):
            pair_earnings_release(current, [duplicate, duplicate])

    def test_same_timestamp_and_priority_is_ambiguous(self) -> None:
        current = _release("current", "2026-07-20", 2026, 2)
        history = [
            _release("candidate-a", "2026-04-20", 2026, 1),
            _release("candidate-b", "2026-04-20", 2026, 1),
        ]

        result = pair_earnings_release(current, history)

        self.assertEqual(result["pairing_status"], "AMBIGUOUS")
        self.assertEqual(result["pairing_method"], "AMBIGUOUS")
        self.assertIsNone(result["previous_release_id"])

    def test_result_is_deterministic_for_different_input_order(self) -> None:
        current = _release("current", "2026-07-20", 2026, 2)
        q4 = _release("q4", "2026-01-20", 2025, 4)
        q1 = _release("q1", "2026-04-20", 2026, 1)

        first = pair_earnings_release(current, [q4, q1])
        second = pair_earnings_release(current, [q1, q4])

        self.assertEqual(first, second)

    def test_candidate_count_counts_only_compatible_prior_documents(self) -> None:
        current = _release("current", "2026-07-20", 2026, 2)
        history = [
            _release("q1", "2026-04-20", 2026, 1),
            _release("q4", "2026-01-20", 2025, 4),
            _release("q3", "2025-10-20", 2025, 3),
            _release("other", "2026-04-20", 2026, 1, ticker="OTHER"),
            _release("future", "2026-10-20", 2026, 3),
        ]

        result = pair_earnings_release(current, history)

        self.assertEqual(result["candidate_count"], 3)

    def test_days_between_releases(self) -> None:
        current = _release("current", "2026-04-21", 2026, 1)
        history = [_release("previous", "2026-01-20", 2025, 4)]

        result = pair_earnings_release(current, history)

        self.assertEqual(result["days_between_releases"], 91)

    def test_future_releases_do_not_influence_pair(self) -> None:
        current = _release("current", "2026-07-20", 2026, 2)
        previous = _release("q1", "2026-04-20", 2026, 1)
        baseline = pair_earnings_release(current, [previous])
        with_future = pair_earnings_release(
            current,
            [
                previous,
                _release("q3", "2026-10-20", 2026, 3),
                _release("q4", "2027-01-20", 2026, 4),
            ],
        )

        self.assertEqual(baseline, with_future)

    def test_current_release_in_history_is_excluded(self) -> None:
        current = _release("current", "2026-07-20", 2026, 2)
        previous = _release("q1", "2026-04-20", 2026, 1)

        result = pair_earnings_release(current, [current, previous])

        self.assertEqual(result["previous_release_id"], "q1")
        self.assertEqual(result["candidate_count"], 1)


if __name__ == "__main__":
    unittest.main()
