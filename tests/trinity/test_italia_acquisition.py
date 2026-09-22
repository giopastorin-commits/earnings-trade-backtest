"""Network-free tests for the Technoprobe acquisition boundary."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests

from trinity.italia_acquisition import (
    AcquisitionError, CachedHTTP, PRESS_URL, REPORT_URL, acquire_technoprobe,
    discover_documents, extract_results, fetch_prices,
)
from trinity.italia_real import load_real_company
from trinity.italia_v1 import build_facts


NOW = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
RESULT_URL = "https://www.technoprobe.com/wp-content/uploads/2026/08/PR-H1-2026.pdf"
REPORT_PDF = "https://www.technoprobe.com/wp-content/uploads/2026/08/Half-Year-Report.pdf"
OLD_URL = "https://www.technoprobe.com/wp-content/uploads/2026/07/agreement.pdf"
PARTNER_URL = "https://www.technoprobe.com/wp-content/uploads/2026/08/partnership.pdf"
FUTURE_URL = "https://www.technoprobe.com/wp-content/uploads/2026/10/future.pdf"
PRESS = f'''<ul>
<li><p class="download-title">Future Results - October 1st, 2026</p><a href="{FUTURE_URL}">PDF</a></li>
<li><p class="download-title">Board of Directors approves the Consolidated Financial Results at June 30, 2026 - August 5th, 2026</p><a href="{RESULT_URL}">PDF</a></li>
<li><p class="download-title">Completion of the strategic partnership in China - 14th August 2026</p><a href="{PARTNER_URL}">PDF</a></li>
<li><p class="download-title">Shareholders' Agreement - July 20th, 2026</p><a href="{OLD_URL}">PDF</a></li>
</ul>'''.encode()
REPORT = f'''<div class="block-block_listing_download"><strong>August 5th, 2026</strong>
<p class="download-title">Half Year Financial Report as of 30.06.2026</p>
<a href="{REPORT_PDF}">PDF</a></div>'''.encode()
TEXT = """Consolidated Revenues of €464.1 million, up 42.4% compared with 2025
Revenue 464,051 325,860 42.4%
Operating profit 170,487 74,083 130.1%
Net profit 143,108 34,408 315.9%
Ebitda* 206,151 106,362 93.8%
Net Financial Position** 685,882 684,217 0.2%
Revenue increased from €950–1,050 million to €1,050–1,100 million
EBITDA margin has been raised from 44%–46% to 46%–48%
increased from €250 million to €350 million
"""


class Response:
    def __init__(self, content: bytes, error: bool = False):
        self.content = content
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise requests.HTTPError("synthetic 500")


class Session:
    def __init__(self, *, error: bool = False, missing_report: bool = False):
        start = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
        timestamps = [int((start + timedelta(days=i)).timestamp()) for i in range(113)]
        self.payload = {"chart": {"result": [{"meta": {"symbol": "TPRO.MI", "exchangeName": "MIL", "currency": "EUR"},
                                    "timestamp": timestamps,
                                    "indicators": {"quote": [{"open": [float(i + 10) for i in range(113)],
                                                               "high": [float(i + 11) for i in range(113)],
                                                               "low": [float(i + 9) for i in range(113)],
                                                               "close": [float(i + 10) for i in range(113)],
                                                               "volume": [1000 + i for i in range(113)]}]}}]}}
        self.error = error
        self.missing_report = missing_report
        self.calls = []

    def get(self, url, timeout=30):
        self.calls.append(url)
        if self.error:
            return Response(b"error", True)
        if "finance.yahoo.com" in url:
            return Response(json.dumps(self.payload).encode())
        if url == PRESS_URL:
            return Response(PRESS)
        if url == REPORT_URL:
            return Response(b"<html></html>" if self.missing_report else REPORT)
        if url in (RESULT_URL, REPORT_PDF, OLD_URL, PARTNER_URL):
            return Response(b"%PDF synthetic")
        raise AssertionError("unexpected URL: " + url)


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def cache(self, session):
        return CachedHTTP(self.tmp.name, session, now=NOW)

    def test_prices_use_only_completed_sessions_and_closes(self):
        result = fetch_prices(self.cache(Session()), "2026-09-21")
        self.assertEqual(result["published_at"], "2026-09-20")
        self.assertEqual(result["current_price"], 121.0)
        self.assertEqual(result["volume"], 1111)
        self.assertAlmostEqual(result["return_5d"], 100 * (121 / 116 - 1))
        self.assertIsNotNone(result["return_20d"])
        self.assertIsNotNone(result["return_60d"])
        self.assertEqual(result["market"], "MIL")
        self.assertEqual(result["currency"], "EUR")
        self.assertTrue(Path(result["audit"]["raw_path"]).is_file())

    def test_foreign_ticker_never_falls_back(self):
        session = Session()
        with self.assertRaises(AcquisitionError):
            fetch_prices(self.cache(session), "2026-09-18", ticker="K8B.F")
        self.assertFalse(session.calls)

    def test_http_error_has_no_fallback(self):
        with self.assertRaisesRegex(AcquisitionError, "fetch failed"):
            fetch_prices(self.cache(Session(error=True)), "2026-09-18")

    def test_invalid_or_missing_prices_fail(self):
        session = Session()
        session.payload["chart"]["result"][0]["indicators"]["quote"][0]["close"][0] = None
        with self.assertRaises(AcquisitionError):
            fetch_prices(self.cache(session), "2026-09-18")

    def test_discovery_excludes_future_and_keeps_date_precision(self):
        docs = discover_documents(PRESS, REPORT, "2026-09-18")
        self.assertEqual(len(docs), 4)
        self.assertIn(PARTNER_URL, [d["url"] for d in docs])
        self.assertNotIn(FUTURE_URL, [d["url"] for d in docs])
        self.assertTrue(all(d["published_at_precision"] == "DATE_ONLY" for d in docs))

    def test_missing_report_fails(self):
        with self.assertRaisesRegex(AcquisitionError, "financial report"):
            discover_documents(PRESS, b"<html></html>", "2026-09-18")

    def test_exact_extraction_and_missing_fields(self):
        fundamentals, extras = extract_results(TEXT, "E1")
        self.assertEqual(fundamentals["revenue"], 464051000)
        self.assertEqual(fundamentals["net_debt"], -685882000)
        self.assertEqual(fundamentals["free_cash_flow"], None)
        self.assertEqual(extras["ebitda"], 206151000)
        self.assertIn("€1,050–1,100", extras["guidance"])
        missing, _ = extract_results("No verified table", "E1")
        self.assertTrue(all(missing[k] is None for k in ("revenue", "net_income", "net_debt")))

    def test_end_to_end_mocked_sources_and_provenance(self):
        session = Session()
        with patch("trinity.italia_acquisition.extract_pdf_text", return_value=TEXT):
            company, summary = acquire_technoprobe("2026-09-18", cache_dir=self.tmp.name,
                                                    session=session, now=NOW)
        facts = build_facts(company, "2026-09-18")
        self.assertEqual(facts["fundamentals"]["revenue"], 464051000)
        self.assertEqual(len(facts["evidence"]), 5)
        self.assertEqual(len(summary["documents"]), 4)
        self.assertEqual(summary["emarket_storage"], "NOT_ACCESSED")
        self.assertTrue(all(Path(e["raw_path"]).exists() for e in facts["evidence"]))
        self.assertTrue(all(e["published_at"] <= "2026-09-18" for e in facts["evidence"]))
        self.assertFalse(any(FUTURE_URL in url for url in session.calls))

    def test_cache_avoids_repeat_network(self):
        session = Session()
        with patch("trinity.italia_acquisition.extract_pdf_text", return_value=TEXT):
            acquire_technoprobe("2026-09-18", cache_dir=self.tmp.name, session=session, now=NOW)
            count = len(session.calls)
            acquire_technoprobe("2026-09-18", cache_dir=self.tmp.name, session=session, now=NOW)
        self.assertEqual(len(session.calls), count)

    def test_loader_live_route(self):
        with patch("trinity.italia_acquisition.acquire_technoprobe", return_value=({"ticker": "TPRO"}, {})) as mock:
            self.assertEqual(load_real_company("TPRO.MI", "2026-09-18")["ticker"], "TPRO")
            mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
