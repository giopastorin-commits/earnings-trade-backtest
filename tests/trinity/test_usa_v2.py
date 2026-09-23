"""Contract tests for the frozen, read-only USA thesis slice."""

import unittest
import hashlib
from pathlib import Path

from trinity.italia_v1 import analyze_company
from trinity.usa_v2 import (
    AS_OF, COMPANIES, NEWS, PRICES, _issuer_release, _literal_metrics,
    _numeric_audit, _clean_citations, _guidance_ranges, load_company,
)


class FakeProvider:
    model_version = "test:fake"

    def analyze(self, facts):
        source = facts["evidence"][0]["identifier"]
        return {
            "fundamental_analysis": f"I prospetti completi non sono disponibili [{source}].",
            "earnings_and_news_analysis": f"Le fonti restano limitate [{source}].",
            "price_context": f"Prezzo di chiusura verificato [{source}].",
            "bull_case": f"Richiede nuove prove [{source}].",
            "bear_case": f"La prova e limitata [{source}].",
            "catalysts": [], "risks": [f"Dati incompleti [{source}]."],
            "thesis_invalidation": f"Nuovi documenti cambierebbero la valutazione [{source}].",
            "proposed_status": "PASS", "confidence": "LOW",
        }

    def critique(self, facts, draft):
        return {"notes": ["La prova non sostiene una tesi forte."], "status": "PASS", "confidence": "LOW"}


class USAV2Tests(unittest.TestCase):
    def test_rejects_aggregator_without_issuer_wire(self):
        article = {"title": "Apple reports third quarter results", "content": "Commentary about Apple",
                   "symbols": ["AAPL.US"]}
        self.assertFalse(_issuer_release(article, "AAPL"))

    def test_literal_metrics_need_explicit_currency_scale(self):
        text = "Revenue was $109.4 billion. Margin was 26%. Net income of 555 unknown units."
        metrics = _literal_metrics(text, "e1")
        self.assertEqual(len(metrics), 1)
        self.assertEqual((metrics[0]["value"], metrics[0]["unit"]), (109.4, "USD_BILLIONS"))
        self.assertEqual(metrics[0]["evidence_identifier"], "e1")

    def test_metric_does_not_cross_into_next_label_or_confuse_change_with_level(self):
        text = ("Sales $9.3 billion, underlying sales up 4% Operating profit $2.6 billion; "
                "adjusted operating profit $2.7 billion. Operating income up $2.1 billion. "
                "AWS net sales increased to a $169 billion annualized revenue run rate.")
        metrics = _literal_metrics(text, "e1")
        self.assertEqual([(m["metric_label"], m["value"], m["measurement_type"]) for m in metrics], [
            ("sales", 9.3, "REPORTED_AMOUNT"),
            ("operating profit", 2.6, "REPORTED_AMOUNT"),
            ("adjusted operating profit", 2.7, "REPORTED_AMOUNT"),
            ("operating income", 2.1, "CHANGE_AMOUNT"),
            ("net sales", 169.0, "ANNUALIZED_RUN_RATE"),
        ])

    def test_numeric_audit_rejects_uncited_number(self):
        facts = {"evidence": [{"identifier": "e1", "excerpt": "Revenue $10 billion"}],
                 "price": {"current_price": 100, "return_5d": 2, "return_20d": 3, "return_60d": 4}}
        draft = {"fundamental_analysis": "Revenue was $11 billion [e1]."}
        self.assertIn("11", " ".join(_numeric_audit(facts, draft)))

    def test_numeric_audit_accepts_decimal_comma_and_quarter_ordinal(self):
        facts = {"evidence": [{"identifier": "e1", "excerpt": "Second Quarter revenue $109.4 billion"}],
                 "price": {"current_price": 100, "return_5d": 2, "return_20d": 3, "return_60d": 4}}
        draft = {"fundamental_analysis": "Nel 2° trimestre, ricavi di USD 109,4 miliardi [e1]."}
        self.assertEqual(_numeric_audit(facts, draft), [])

    def test_numeric_audit_accepts_unit_attached_to_number_and_zero_threshold(self):
        facts = {"evidence": [{"identifier": "e1", "excerpt": "capacity of two gigawatts"}],
                 "price": {"current_price": 100, "return_5d": 2, "return_20d": 3, "return_60d": 4}}
        draft = {"catalysts": ["Capacita prevista di 2 GW [e1]."],
                 "thesis_invalidation": "Invalidazione se la crescita diventa 0% [e1]."}
        self.assertEqual(_numeric_audit(facts, draft), [])

    def test_combined_and_pseudo_citations_are_cleaned(self):
        text = "Fonte [e1; e2] e dato mancante [financial_facts]."
        self.assertEqual(_clean_citations(text, {"e1", "e2"}),
                         "Fonte [e1] [e2] e dato mancante.")

    def test_per_share_guidance_ranges_are_literal_and_sourced(self):
        text = ("2026 GUIDANCE Earnings (per diluted share) Previous Current "
                "Core FFO attributable to holders $6.00 to $6.20 $6.07 to $6.23")
        ranges = _guidance_ranges(text, "e1")
        self.assertEqual(len(ranges), 1)
        self.assertEqual(ranges[0]["previous_range"], [6.0, 6.2])
        self.assertEqual(ranges[0]["current_range"], [6.07, 6.23])
        self.assertEqual((ranges[0]["unit"], ranges[0]["evidence_identifier"]),
                         ("USD_PER_SHARE", "e1"))

    @unittest.skipUnless(PRICES.is_dir() and NEWS.is_dir(), "frozen EODHD cache unavailable")
    def test_all_ten_cache_and_end_to_end_contract(self):
        for ticker in COMPANIES:
            with self.subTest(ticker=ticker):
                pack = load_company(ticker, AS_OF)
                self.assertEqual(pack.company_input["price"]["published_at"], "2026-09-11")
                self.assertGreaterEqual(len(pack.company_input["evidence"]), 2)
                self.assertGreater(pack.triage["rejected_non_issuer_news"], 0)
                for event in pack.company_input["events"]:
                    for metric in event["facts"]["reported_metrics"]:
                        self.assertIn(metric["unit"], {"USD_MILLIONS", "USD_BILLIONS", "USD_TRILLIONS"})
                        self.assertIn("$", metric["source_excerpt"])
                thesis = analyze_company(pack.company_input, AS_OF, FakeProvider())
                self.assertEqual(thesis.ticker, ticker)
                self.assertEqual(thesis.status, "PASS")
                self.assertEqual(thesis.confidence, "LOW")

    @unittest.skipUnless(PRICES.is_dir() and NEWS.is_dir(), "frozen EODHD cache unavailable")
    def test_evidence_hashes_resolve_to_original_cache_records(self):
        evidence = load_company("AAPL").company_input["evidence"]
        price = evidence[0]
        self.assertEqual(hashlib.sha256(Path(price["raw_path"]).read_bytes()).hexdigest(),
                         price["content_sha256"])
        release = evidence[1]
        path, line_number = release["raw_path"].rsplit(":", 1)
        line = Path(path).read_text(encoding="utf-8").splitlines(keepends=True)[int(line_number) - 1]
        self.assertEqual(hashlib.sha256(line.encode("utf-8")).hexdigest(),
                         release["content_sha256"])

    def test_future_cutoff_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "exceeds frozen"):
            load_company("AAPL", "2026-09-13")


if __name__ == "__main__":
    unittest.main()
