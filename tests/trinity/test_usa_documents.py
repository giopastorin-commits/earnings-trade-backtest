"""Deterministic document coverage tests for TRINITY USA V2."""

from datetime import date
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from trinity.usa_documents import (
    DEFAULT_CACHE, SEC_COMPANIES, SECAcquisitionError, _selected_filings,
    _submission_documents, extract_xbrl_facts, load_document_coverage,
)
from trinity.usa_v2 import AS_OF, load_company


class USADocumentCoverageTests(unittest.TestCase):
    def test_selects_periodic_two_earnings_and_material_filing(self):
        rows = [
            {"form": "8-K", "filingDate": "2026-08-20", "items": "8.01"},
            {"form": "10-Q", "filingDate": "2026-07-31", "items": ""},
            {"form": "8-K", "filingDate": "2026-07-30", "items": "2.02,9.01"},
            {"form": "8-K", "filingDate": "2026-04-30", "items": "2.02,9.01"},
            {"form": "8-K", "filingDate": "2026-01-30", "items": "2.02,9.01"},
        ]
        selected = _selected_filings(rows)
        self.assertEqual([role for role, _ in selected],
                         ["PERIODIC_REPORT", "EARNINGS_CURRENT", "EARNINGS_PREVIOUS", "MATERIAL_EVENT"])

    def test_complete_submission_extracts_primary_and_generic_ex99(self):
        raw = (b"<DOCUMENT><TYPE>8-K\n<FILENAME>issuer.htm\n<TEXT><p>8-K</p></TEXT></DOCUMENT>"
               b"<DOCUMENT><TYPE>EX-99\n<FILENAME>release.htm\n<TEXT><p>Results</p></TEXT></DOCUMENT>")
        documents = _submission_documents(raw)
        self.assertEqual([(item[0], item[1]) for item in documents],
                         [("8-K", "issuer.htm"), ("EX-99", "release.htm")])

    def test_xbrl_exact_accession_and_derived_facts(self):
        accession = "0000000001-26-000001"
        def duration(current, previous):
            return {"USD": [
                {"accn": accession, "form": "10-Q", "start": "2026-04-01", "end": "2026-06-30", "val": current},
                {"accn": accession, "form": "10-Q", "start": "2025-04-01", "end": "2025-06-30", "val": previous},
            ]}
        facts = {"facts": {"us-gaap": {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": duration(120, 100)},
            "OperatingIncomeLoss": {"units": duration(24, 20)},
            "NetIncomeLoss": {"units": duration(18, 15)},
            "NetCashProvidedByUsedInOperatingActivities": {"units": duration(30, 25)},
            "PaymentsToAcquirePropertyPlantAndEquipment": {"units": duration(8, 7)},
            "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [
                {"accn": accession, "form": "10-Q", "end": "2026-06-30", "val": 50},
            ]}},
        }}}
        result = extract_xbrl_facts(facts, accession, "sec:T:1:10-q", "INDUSTRIAL")
        mapped = {item["metric"]: item for item in result}
        self.assertEqual(mapped["revenue_growth"]["value"], 20.0)
        self.assertEqual(mapped["operating_margin"]["value"], 20.0)
        self.assertEqual(mapped["free_cash_flow"]["value"], 22)
        self.assertTrue(all(item["evidence_identifier"] == "sec:T:1:10-q" for item in result))

    def test_cached_document_hash_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = b"official"
            document = root / "doc.htm"
            document.write_bytes(raw)
            manifest = {"as_of": AS_OF, "companies": {"AAPL": {"documents": [{
                "local_path": str(document), "content_sha256": hashlib.sha256(raw).hexdigest(),
                "published_at": "2026-07-31", "filing_date": "2026-07-31",
                "evidence_id": "sec:AAPL:test:10-q",
            }]}}}
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            self.assertIsNotNone(load_document_coverage("AAPL", AS_OF, cache_dir=root))
            document.write_bytes(b"changed")
            with self.assertRaisesRegex(SECAcquisitionError, "hash mismatch"):
                load_document_coverage("AAPL", AS_OF, cache_dir=root)

    @unittest.skipUnless((DEFAULT_CACHE / "manifest.json").is_file(), "SEC cache unavailable")
    def test_all_ten_have_primary_periodic_and_earnings_8k(self):
        for ticker in SEC_COMPANIES:
            with self.subTest(ticker=ticker):
                company = load_document_coverage(ticker, AS_OF)
                kinds = [item["document_kind"] for item in company["documents"]]
                self.assertIn("SEC_PERIODIC_REPORT", kinds)
                self.assertIn("SEC_8_K_EARNINGS", kinds)
                self.assertTrue(all(item["source"].startswith("SEC EDGAR")
                                    for item in company["documents"]))
                for item in company["documents"]:
                    self.assertLessEqual(date.fromisoformat(item["filing_date"]), date.fromisoformat(AS_OF))
                    for field in ("url", "accepted_at", "retrieved_at", "content_sha256", "evidence_id"):
                        self.assertTrue(item[field])

    @unittest.skipUnless((DEFAULT_CACHE / "manifest.json").is_file(), "SEC cache unavailable")
    def test_primary_release_replaces_cached_result_news_and_guidance_changes_are_deterministic(self):
        for ticker in SEC_COMPANIES:
            with self.subTest(ticker=ticker):
                pack = load_company(ticker)
                self.assertTrue(any(item["source"] == "SEC EDGAR" for item in pack.company_input["evidence"]))
                sec_release = any(item["document_kind"] == "SEC_EARNINGS_RELEASE"
                                  for item in pack.company_input["evidence"])
                if sec_release:
                    result_ids = {event["facts"]["evidence_identifier"] for event in pack.company_input["events"]
                                  if event["kind"] == "RESULTS"}
                    if ticker == "XOM":
                        self.assertTrue(any(identifier.startswith("news:") for identifier in result_ids))
                    else:
                        self.assertTrue(all(identifier.startswith("sec:") for identifier in result_ids))
        for ticker in ("PLD", "LIN"):
            pack = load_company(ticker)
            changes = [change for event in pack.company_input["events"]
                       for change in event["facts"].get("guidance_changes", [])]
            self.assertEqual(len(changes), 1)
            self.assertEqual(changes[0]["direction"], "RAISED")
            self.assertEqual(changes[0]["unit"], "USD_PER_SHARE")


if __name__ == "__main__":
    unittest.main()
