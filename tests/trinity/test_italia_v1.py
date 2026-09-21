"""Synthetic-only tests for the TRINITY Italia V1 vertical slice."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import tempfile
import unittest

from trinity.italia_v1 import (
    ItaliaV1InputError,
    ThesisRecord,
    analyze_company,
    build_facts,
    load_thesis,
    save_thesis,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "italia_v1_companies.fixture"


def load_fixture() -> list[dict[str, object]]:
    """Load three entirely fictional Italian companies."""
    with FIXTURE_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


class FakeAnalystCritic:
    model_version = "fake-italia-v1"

    def __init__(self, *, unsupported_number: bool = False) -> None:
        self.unsupported_number = unsupported_number
        self.calls: list[str] = []

    def analyze(self, facts: dict[str, object]) -> dict[str, object]:
        self.calls.append("analyst")
        ticker = facts["identity"]["ticker"]
        return {
            "fundamental_analysis": "Figures are reported as supplied; no ratios are derived.",
            "earnings_and_news_analysis": "Only dated events in facts are considered.",
            "price_context": "Price fields are used only when present.",
            "bull_case": (
                "Revenue will rise 999%." if self.unsupported_number
                else "The supplied positive facts may support improvement."
            ),
            "bear_case": "Execution or missing evidence may weaken the thesis.",
            "catalysts": ["Next disclosed results"] if facts["events"] else [],
            "risks": ["Insufficient evidence"] if not facts["evidence"] else ["Execution risk"],
            "thesis_invalidation": "Reassess if the disclosed facts deteriorate.",
            "proposed_status": "PASS" if ticker == "CIMA" else "WATCH",
            "confidence": "LOW" if ticker == "CIMA" else "MEDIUM",
        }

    def critique(self, facts: dict[str, object], draft: dict[str, object]) -> dict[str, object]:
        self.calls.append("critic")
        notes: list[str] = []
        fact_numbers = set(re.findall(r"\b\d+(?:\.\d+)?\b", json.dumps(facts)))
        claim_numbers = set(re.findall(r"\b\d+(?:\.\d+)?\b", draft["bull_case"]))
        for number in sorted(claim_numbers - fact_numbers):
            notes.append(f"Unsupported numeric claim: {number}")
        if not facts["evidence"]:
            notes.append("Insufficient evidence")
            return {"notes": notes, "status": "PASS", "confidence": "LOW"}
        if notes:
            return {"notes": notes, "status": "PASS", "confidence": "LOW"}
        if facts["identity"]["ticker"] == "ALBA":
            return {"notes": [], "status": "INVESTIGATE", "confidence": "HIGH"}
        return {"notes": [], "status": "WATCH", "confidence": "MEDIUM"}


class ItaliaV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.positive, self.mixed, self.sparse = load_fixture()

    def test_fixture_contains_three_fictional_scenarios(self) -> None:
        self.assertEqual([row["ticker"] for row in load_fixture()], ["ALBA", "BOREA", "CIMA"])
        self.assertGreater(self.positive["fundamentals"]["free_cash_flow"], 0)
        self.assertLess(self.mixed["fundamentals"]["free_cash_flow"], 0)
        self.assertEqual(self.sparse["evidence"], [])

    def test_end_to_end_fake_analyst_critic(self) -> None:
        provider = FakeAnalystCritic()
        thesis = analyze_company(self.positive, as_of="2026-09-21", provider=provider)

        self.assertIsInstance(thesis, ThesisRecord)
        self.assertEqual(provider.calls, ["analyst", "critic"])
        self.assertEqual(thesis.status, "INVESTIGATE")
        self.assertEqual(thesis.confidence, "HIGH")
        self.assertEqual(thesis.ticker, "ALBA")
        self.assertEqual(thesis.facts["fundamentals"]["revenue_growth"], 9.1)
        self.assertEqual(thesis.evidence[0]["identifier"], "ALBA-Q2-2026")
        self.assertEqual(thesis.model_version, "fake-italia-v1")
        self.assertTrue(thesis.prompt_version)
        for field in (
            "fundamental_analysis", "earnings_and_news_analysis", "price_context",
            "bull_case", "bear_case", "thesis_invalidation",
        ):
            self.assertTrue(getattr(thesis, field))
        self.assertTrue(thesis.catalysts)
        self.assertTrue(thesis.risks)

    def test_mixed_company_and_insufficient_evidence(self) -> None:
        mixed = analyze_company(self.mixed, "2026-09-21", FakeAnalystCritic())
        sparse = analyze_company(self.sparse, "2026-09-21", FakeAnalystCritic())

        self.assertEqual((mixed.status, mixed.confidence), ("WATCH", "MEDIUM"))
        self.assertEqual((sparse.status, sparse.confidence), ("PASS", "LOW"))
        self.assertIn("Insufficient evidence", sparse.critic_notes)
        self.assertIsNone(sparse.facts["price"]["current_price"])
        self.assertIsNone(sparse.facts["fundamentals"]["revenue"])
        self.assertIn("fundamentals.revenue", sparse.facts["missing_fields"])

    def test_critic_flags_number_not_in_facts(self) -> None:
        thesis = analyze_company(
            self.positive, "2026-09-21", FakeAnalystCritic(unsupported_number=True)
        )
        self.assertIn("Unsupported numeric claim: 999", thesis.critic_notes)
        self.assertEqual((thesis.status, thesis.confidence), ("PASS", "LOW"))

    def test_provider_cannot_mutate_audited_facts(self) -> None:
        class MutatingProvider(FakeAnalystCritic):
            def analyze(self, facts: dict[str, object]) -> dict[str, object]:
                facts["fundamentals"]["revenue"] = 999
                return super().analyze(facts)

            def critique(
                self, facts: dict[str, object], draft: dict[str, object]
            ) -> dict[str, object]:
                facts["evidence"].clear()
                return super().critique(facts, draft)

        thesis = analyze_company(self.positive, "2026-09-21", MutatingProvider())
        self.assertEqual(thesis.facts["fundamentals"]["revenue"], 420000000.0)
        self.assertEqual(len(thesis.evidence), 1)

    def test_thesis_record_validation_rejects_bad_status_and_confidence(self) -> None:
        thesis = analyze_company(self.positive, "2026-09-21", FakeAnalystCritic())
        for status in ("BUY", "SELL", "HOLD", ""):
            with self.subTest(status=status), self.assertRaises(ItaliaV1InputError):
                replace(thesis, status=status)
        for confidence in ("90", "VERY_HIGH", ""):
            with self.subTest(confidence=confidence), self.assertRaises(ItaliaV1InputError):
                replace(thesis, confidence=confidence)

    def test_record_identity_and_evidence_are_validated(self) -> None:
        thesis = analyze_company(self.positive, "2026-09-21", FakeAnalystCritic())
        with self.assertRaises(ItaliaV1InputError):
            replace(thesis, ticker="OTHER")
        with self.assertRaises(ItaliaV1InputError):
            replace(thesis, evidence=[])
        with self.assertRaises(ItaliaV1InputError):
            replace(thesis, created_at="2026-09-21T12:00:00")

    def test_future_dated_sections_events_and_evidence_fail_before_llm(self) -> None:
        for section in ("price", "fundamentals", "events", "evidence"):
            with self.subTest(section=section):
                data = load_fixture()[0]
                if section in ("events", "evidence"):
                    data[section][0]["published_at"] = "2026-09-22"
                else:
                    data[section]["published_at"] = "2026-09-22"
                provider = FakeAnalystCritic()
                with self.assertRaisesRegex(ItaliaV1InputError, "after as_of"):
                    analyze_company(data, "2026-09-21", provider)
                self.assertEqual(provider.calls, [])

    def test_dated_values_require_provenance_and_matching_as_of(self) -> None:
        data = load_fixture()[0]
        del data["price"]["published_at"]
        with self.assertRaises(ItaliaV1InputError):
            build_facts(data, "2026-09-21")
        with self.assertRaisesRegex(ItaliaV1InputError, "differs"):
            build_facts(self.positive, "2026-09-20")

    def test_intraday_timestamp_is_rejected_without_implicit_cutoff(self) -> None:
        with self.assertRaisesRegex(ItaliaV1InputError, "not a timestamp"):
            build_facts(self.positive, datetime(2026, 9, 21, tzinfo=timezone.utc))
        data = load_fixture()[0]
        data["evidence"][0]["published_at"] = "2026-09-21T12:00:00+02:00"
        with self.assertRaises(ItaliaV1InputError):
            build_facts(data, "2026-09-21")

    def test_nonfinite_numbers_are_rejected_and_missing_values_are_not_derived(self) -> None:
        data = load_fixture()[0]
        data["fundamentals"]["operating_margin"] = None
        facts = build_facts(data, "2026-09-21")
        self.assertIsNone(facts["fundamentals"]["operating_margin"])
        self.assertIn("fundamentals.operating_margin", facts["missing_fields"])
        data["price"]["return_5d"] = float("nan")
        with self.assertRaises(ItaliaV1InputError):
            build_facts(data, "2026-09-21")

    def test_json_save_and_load_round_trip(self) -> None:
        thesis = analyze_company(self.positive, "2026-09-21", FakeAnalystCritic())
        with tempfile.TemporaryDirectory() as directory:
            path = save_thesis(thesis, directory)
            self.assertEqual(path.name, f"{thesis.thesis_id}.json")
            loaded = load_thesis(thesis.thesis_id, directory)
            self.assertEqual(loaded, thesis)
            with self.assertRaises(FileExistsError):
                save_thesis(thesis, directory)

    def test_invalid_loaded_record_fails_validation(self) -> None:
        thesis = analyze_company(self.positive, "2026-09-21", FakeAnalystCritic())
        with tempfile.TemporaryDirectory() as directory:
            path = save_thesis(thesis, directory)
            payload = thesis.to_dict()
            payload["status"] = "BUY"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ItaliaV1InputError):
                load_thesis(thesis.thesis_id, directory)

    def test_loaded_facts_are_rechecked_for_future_dates(self) -> None:
        thesis = analyze_company(self.positive, "2026-09-21", FakeAnalystCritic())
        with tempfile.TemporaryDirectory() as directory:
            path = save_thesis(thesis, directory)
            payload = thesis.to_dict()
            payload["facts"]["price"]["published_at"] = "2026-09-22"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ItaliaV1InputError, "after as_of"):
                load_thesis(thesis.thesis_id, directory)


if __name__ == "__main__":
    unittest.main()
