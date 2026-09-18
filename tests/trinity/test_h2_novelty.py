"""Unit tests for the deterministic TRINITY/H2 textual novelty measure."""

from __future__ import annotations

import unittest

from trinity.h2_novelty import (
    H2InputError,
    calculate_h2_novelty,
    normalize_text,
)


CURRENT_RELEASE = "Revenue increased 12 percent while operating margin improved."
PREVIOUS_RELEASE = "Revenue increased 10 percent while operating margin improved."


class H2NoveltyTests(unittest.TestCase):
    def test_identical_texts_have_near_zero_novelty(self) -> None:
        result = calculate_h2_novelty(CURRENT_RELEASE, CURRENT_RELEASE)

        self.assertAlmostEqual(result["similarity"], 1.0)
        self.assertAlmostEqual(result["h2_novelty"], 0.0)

    def test_very_different_texts_have_greater_novelty(self) -> None:
        similar = calculate_h2_novelty(CURRENT_RELEASE, PREVIOUS_RELEASE)
        different = calculate_h2_novelty(
            "Factories expanded capacity across three European production sites.",
            "Cloud subscriptions declined after enterprise contract renewals slowed.",
        )

        self.assertGreater(different["h2_novelty"], similar["h2_novelty"])

    def test_normalization_is_case_insensitive(self) -> None:
        self.assertEqual(
            normalize_text("REVENUE Growth Remained STRONG"),
            "revenue growth remained strong",
        )

    def test_normalization_collapses_whitespace(self) -> None:
        self.assertEqual(
            normalize_text("  revenue\n\t growth   remained  strong "),
            "revenue growth remained strong",
        )

    def test_normalization_removes_simple_html(self) -> None:
        self.assertEqual(
            normalize_text("<p>Revenue <strong>grew</strong> 12%.</p>"),
            "revenue grew 12%.",
        )

    def test_normalization_preserves_numbers(self) -> None:
        normalized = normalize_text("Revenue rose 12.5% to $450 million in 2026.")

        self.assertIn("12.5", normalized)
        self.assertIn("450", normalized)
        self.assertIn("2026", normalized)

    def test_empty_text_raises(self) -> None:
        with self.assertRaisesRegex(H2InputError, "non-whitespace"):
            calculate_h2_novelty("", PREVIOUS_RELEASE)

    def test_whitespace_only_text_raises(self) -> None:
        with self.assertRaisesRegex(H2InputError, "non-whitespace"):
            calculate_h2_novelty(" \n\t ", PREVIOUS_RELEASE)

    def test_non_string_input_raises(self) -> None:
        with self.assertRaisesRegex(H2InputError, "must be a string"):
            calculate_h2_novelty(123, PREVIOUS_RELEASE)  # type: ignore[arg-type]

    def test_too_short_document_raises(self) -> None:
        with self.assertRaisesRegex(H2InputError, "too short"):
            calculate_h2_novelty("Revenue increased", PREVIOUS_RELEASE)

    def test_repeated_calls_are_deterministic(self) -> None:
        first = calculate_h2_novelty(CURRENT_RELEASE, PREVIOUS_RELEASE)
        second = calculate_h2_novelty(CURRENT_RELEASE, PREVIOUS_RELEASE)

        self.assertEqual(first, second)

    def test_similarity_is_in_unit_interval(self) -> None:
        result = calculate_h2_novelty(CURRENT_RELEASE, PREVIOUS_RELEASE)

        self.assertGreaterEqual(result["similarity"], 0.0)
        self.assertLessEqual(result["similarity"], 1.0)

    def test_novelty_is_in_unit_interval(self) -> None:
        result = calculate_h2_novelty(CURRENT_RELEASE, PREVIOUS_RELEASE)

        self.assertGreaterEqual(result["h2_novelty"], 0.0)
        self.assertLessEqual(result["h2_novelty"], 1.0)

    def test_method_identifier(self) -> None:
        result = calculate_h2_novelty(CURRENT_RELEASE, PREVIOUS_RELEASE)

        self.assertEqual(result["method"], "TFIDF_COSINE_V1")


if __name__ == "__main__":
    unittest.main()
