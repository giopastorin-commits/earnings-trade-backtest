"""Unit and issuer-boundary checks for the four-company data gate."""

from datetime import datetime, timezone
import json
import tempfile
import unittest

from trinity.italia_acquisition import AcquisitionError, discover_documents, fetch_prices
from trinity.italia_company_config import COMPANIES
from trinity.italia_financial_facts import extract_financial_facts
from trinity.italia_v1 import analyze_company, load_thesis, save_thesis


class FakePriceCache:
    now = datetime(2026, 9, 22, tzinfo=timezone.utc)

    def get(self, url, kind):
        ticker = url.split("/chart/")[1].split("?")[0]
        stamps = [int(datetime(2026, 6, 1 + i, tzinfo=timezone.utc).timestamp())
                  for i in range(3)]
        result = {"meta": {"symbol": ticker, "exchangeName": "MIL", "currency": "EUR"},
                  "timestamp": stamps, "indicators": {"quote": [{
                      "open": [10, 11, 12], "high": [11, 12, 13],
                      "low": [9, 10, 11], "close": [10, 11, 12], "volume": [1, 2, 3]}]}}
        return json.dumps({"chart": {"result": [result]}}).encode(), {"url": url}


class GeneralizationTests(unittest.TestCase):
    def test_configured_milan_prices_and_foreign_rejection(self):
        for ticker in COMPANIES:
            with self.subTest(ticker=ticker):
                self.assertEqual(fetch_prices(FakePriceCache(), "2026-09-18", ticker=ticker)["current_price"], 12)
        with self.assertRaises(AcquisitionError):
            fetch_prices(FakePriceCache(), "2026-09-18", ticker="PRY.F")

    def test_declarative_discovery_requires_official_index_link(self):
        config = COMPANIES["DLG.MI"]
        page = b"".join(f'<a href="{d.url}">PDF</a>'.encode() for d in config.documents)
        pages = {d.index_url: page for d in config.documents}
        docs = discover_documents(pages, None, "2026-09-18", config=config)
        self.assertEqual({d["document_type"] for d in docs}, {"RESULTS", "FINANCIAL_REPORT"})
        with self.assertRaises(AcquisitionError):
            discover_documents({config.documents[0].index_url: b""}, None,
                               "2026-09-18", config=config)

    def test_explicit_units_convert_and_missing_units_fail_closed(self):
        config = COMPANIES["ENEL.MI"]
        text = "Revenues: 40,919 million euros\nOrdinary EBITDA: 11,838 million euros"
        facts, failures = extract_financial_facts(text, config, "E1")
        self.assertEqual(facts["revenue"]["value"], 40_919_000_000)
        self.assertEqual(facts["ebitda"]["metric_label"], "Ordinary EBITDA")
        self.assertEqual(facts["revenue"]["source_unit"], "EUR_MILLIONS")
        missing, errors = extract_financial_facts("Revenues: 40,919", config, "E1")
        self.assertIsNone(missing["revenue"])
        self.assertTrue(errors)

    def test_bank_has_no_industrial_metrics(self):
        text = ("NET INCOME OF €5,554M IN H1 2026\n"
                "COMMON EQUITY TIER 1 RATIO WAS 13.1%")
        facts, failures = extract_financial_facts(text, COMPANIES["ISP.MI"], "E1")
        self.assertFalse([item for item in failures if not item.startswith("guidance:")])
        self.assertEqual(facts["net_income"]["value"], 5_554_000_000)
        self.assertEqual(facts["cet1_ratio"]["value"], 13.1)
        self.assertIsNone(facts["ebitda"])
        self.assertIsNone(facts["operating_margin"])

    def test_normalized_facts_survive_thesis_round_trip(self):
        class Provider:
            model_version = "test-provider"

            def analyze(self, facts):
                return {"fundamental_analysis": "Verified value.",
                        "earnings_and_news_analysis": "Dated results.",
                        "price_context": "No price supplied.",
                        "bull_case": "Conditional improvement.",
                        "bear_case": "Execution risk.", "catalysts": [], "risks": [],
                        "thesis_invalidation": "Reassess on new results.",
                        "proposed_status": "WATCH", "confidence": "LOW"}

            def critique(self, facts, draft):
                return {"notes": ["Claims stay within facts."],
                        "status": "WATCH", "confidence": "LOW"}

        company = {"company_name": "Example", "ticker": "EX", "as_of": "2026-09-18",
                   "schema_type": "BANK", "price": {}, "fundamentals": {}, "events": [],
                   "evidence": [{"source": "Official IR", "published_at": "2026-07-29",
                                 "identifier": "E1", "url": "https://example.com/report"}],
                   "financial_facts": {"cet1_ratio": {
                       "value": 13.1, "unit": "PERCENT", "source_unit": "PERCENT",
                       "period": "H1 2026", "evidence_identifier": "E1",
                       "extraction_method": "PDF_TEXT_EXACT_PATTERN",
                       "source_excerpt": "CET1 ratio 13.1%"}}}
        thesis = analyze_company(company, "2026-09-18", Provider())
        with tempfile.TemporaryDirectory() as directory:
            save_thesis(thesis, directory)
            loaded = load_thesis(thesis.thesis_id, directory)
        self.assertEqual(loaded.facts["financial_facts"]["cet1_ratio"]["value"], 13.1)


if __name__ == "__main__":
    unittest.main()
