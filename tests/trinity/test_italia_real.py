"""Offline contract tests for the single-company real-data path."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from trinity.italia_real import CodexCLIProvider, LLMProviderError, SNAPSHOT, load_real_company
from trinity.italia_v1 import ItaliaV1InputError, ThesisRecord, analyze_company, build_facts


AS_OF = "2026-09-18"
ANALYST = {
    "fundamental_analysis": "Ricavi H1 riportati [PR-H1-2026].",
    "earnings_and_news_analysis": "Guidance rialzata [PR-H1-2026].",
    "price_context": "Prezzo osservato [IT0005482333-2026-09-18].",
    "bull_case": "Crescita supportata dai risultati [PR-H1-2026].",
    "bear_case": "Capex e execution risk [PR-H1-2026].",
    "catalysts": ["Prossimi risultati [PR-H1-2026]."],
    "risks": ["Esecuzione piano capex [PR-H1-2026]."],
    "thesis_invalidation": "Peggioramento dei risultati pubblicati [PR-H1-2026].",
    "proposed_status": "WATCH",
    "confidence": "MEDIUM",
}
CRITIC = {"notes": ["La tesi è condizionata alla conferma dei risultati."],
          "status": "WATCH", "confidence": "LOW"}


class RealItaliaTests(unittest.TestCase):
    def test_snapshot_maps_to_company_input_and_deterministic_facts(self) -> None:
        company = load_real_company("TECHNOPROBE", AS_OF)
        facts = build_facts(company, AS_OF)
        self.assertEqual(facts["identity"]["ticker"], "TPRO")
        self.assertEqual(facts["identity"]["isin"], "IT0005482333")
        self.assertEqual(facts["price"]["current_price"], 29.46)
        self.assertEqual(facts["fundamentals"]["revenue"], 464051000.0)
        self.assertAlmostEqual(
            facts["fundamentals"]["operating_margin"], 100 * 170487 / 464051
        )
        self.assertEqual(facts["fundamentals"]["net_debt"], -685882000.0)
        self.assertEqual(facts, build_facts(load_real_company("TECHNOPROBE", AS_OF), AS_OF))

    def test_missing_fields_are_not_invented(self) -> None:
        facts = build_facts(load_real_company("TECHNOPROBE", AS_OF), AS_OF)
        self.assertIsNone(facts["price"]["return_5d"])
        self.assertIsNone(facts["price"]["return_20d"])
        self.assertIsNone(facts["price"]["return_60d"])
        self.assertIsNone(facts["fundamentals"]["free_cash_flow"])
        self.assertIn("fundamentals.free_cash_flow", facts["missing_fields"])

    def test_missing_provider_financial_fields_remain_null(self) -> None:
        snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        for field in ("operating_profit", "net_income", "net_cash", "revenue_growth_pct_reported"):
            snapshot["financial_report"].pop(field)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.fixture"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            facts = build_facts(
                load_real_company("TECHNOPROBE", AS_OF, snapshot_path=path), AS_OF
            )
        for field in ("operating_margin", "net_income", "net_debt", "revenue_growth"):
            self.assertIsNone(facts["fundamentals"][field])

    def test_evidence_provenance_and_event_links(self) -> None:
        facts = build_facts(load_real_company("TECHNOPROBE", AS_OF), AS_OF)
        evidence = facts["evidence"]
        self.assertEqual(len(evidence), 3)
        ids = {entry["identifier"] for entry in evidence}
        self.assertTrue(all(entry["url"].startswith("https://") for entry in evidence))
        self.assertTrue(all(entry["published_at"] <= AS_OF for entry in evidence))
        self.assertTrue(all(event["facts"]["evidence_identifier"] in ids
                            for event in facts["events"]))

    def test_future_dated_snapshot_fails_before_llm(self) -> None:
        snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        snapshot["evidence"][0]["published_at"] = "2026-09-19"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.fixture"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            with self.assertRaisesRegex(ItaliaV1InputError, "after as_of"):
                load_real_company("TECHNOPROBE", AS_OF, snapshot_path=path)

    def test_stale_price_and_wrong_company_are_rejected(self) -> None:
        with self.assertRaisesRegex(ItaliaV1InputError, "mismatch"):
            load_real_company("TECHNOPROBE", "2026-09-21")
        with self.assertRaises(ItaliaV1InputError):
            load_real_company("OTHER", AS_OF)

    def test_mocked_real_provider_parses_analyst_and_critic(self) -> None:
        outputs = [ANALYST, CRITIC]
        prompts: list[str] = []

        def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            prompts.append(kwargs["input"])
            self.assertIn("--sandbox", command)
            self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
            self.assertIn("--ephemeral", command)
            self.assertIn("--output-schema", command)
            schema = json.loads(Path(command[command.index("--output-schema") + 1]).read_text())
            self.assertEqual(set(schema["required"]), set(outputs[len(prompts) - 1]))
            Path(command[command.index("--output-last-message") + 1]).write_text(
                json.dumps(outputs[len(prompts) - 1]), encoding="utf-8"
            )
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch("trinity.italia_real.subprocess.run", side_effect=fake_run):
            thesis = analyze_company(
                load_real_company("TECHNOPROBE", AS_OF), AS_OF, CodexCLIProvider()
            )
        self.assertIsInstance(thesis, ThesisRecord)
        self.assertEqual((thesis.status, thesis.confidence), ("WATCH", "LOW"))
        self.assertEqual(thesis.critic_notes, CRITIC["notes"])
        self.assertEqual(thesis.bull_case, ANALYST["bull_case"])
        self.assertEqual(len(prompts), 2)
        self.assertIn("FACTS:", prompts[0])
        self.assertIn("DRAFT:", prompts[1])

    def test_bad_structured_analyst_or_critic_fails(self) -> None:
        company = load_real_company("TECHNOPROBE", AS_OF)
        for outputs in ([{"proposed_status": "WATCH"}], [ANALYST, {"notes": []}]):
            with self.subTest(outputs=outputs):
                with patch.object(CodexCLIProvider, "_request", side_effect=outputs):
                    with self.assertRaises(ItaliaV1InputError):
                        analyze_company(company, AS_OF, CodexCLIProvider())

    def test_provider_failure_has_no_silent_fallback(self) -> None:
        with patch("trinity.italia_real.subprocess.run", side_effect=FileNotFoundError()):
            with self.assertRaises(LLMProviderError):
                CodexCLIProvider().analyze({"identity": {"ticker": "TPRO"}})
        with patch(
            "trinity.italia_real.subprocess.run",
            return_value=subprocess.CompletedProcess([], 1, "", "error"),
        ):
            with self.assertRaisesRegex(LLMProviderError, "without fallback"):
                CodexCLIProvider().analyze({"identity": {"ticker": "TPRO"}})

    def test_incoherent_critic_pass_high_is_rejected(self) -> None:
        with patch.object(
            CodexCLIProvider, "_request",
            return_value={"notes": ["Evidence incomplete"], "status": "PASS", "confidence": "HIGH"},
        ):
            with self.assertRaisesRegex(LLMProviderError, "PASS requires LOW"):
                CodexCLIProvider().critique({}, ANALYST)


if __name__ == "__main__":
    unittest.main()
