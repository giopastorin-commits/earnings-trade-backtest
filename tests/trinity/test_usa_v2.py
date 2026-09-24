"""Contract tests for the frozen, read-only USA thesis slice."""

import unittest
import hashlib
from pathlib import Path

from trinity.italia_v1 import analyze_company
from trinity.usa_v2 import (
    AS_OF, COMPANIES, NEWS, PRICES, _issuer_release, _literal_metrics,
    USAProvider, _numeric_audit, _clean_citations, _guidance_ranges, load_company,
    validate_claim_refs,
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
    @staticmethod
    def _claim_facts(*facts):
        return {
            "identity": {"ticker": "LIN"},
            "price": {"current_price": None, "return_5d": None, "return_20d": None,
                      "return_60d": None, "acquisition": {"fact_metadata": []}},
            "evidence": [{"identifier": "e1"}],
            "events": [{"facts": {"structured_sec_facts": list(facts)}}],
        }

    @staticmethod
    def _fact(fact_id, value, unit="USD", *, ticker="LIN", evidence_id="e1",
              period="2026Q2", scale=1):
        return {"fact_id": fact_id, "ticker": ticker, "metric": fact_id.rsplit(":", 1)[-1],
                "value": value, "unit": unit, "period": period,
                "evidence_id": evidence_id, "extraction_method": "test", "scale": scale,
                "display_precision": 4}

    @staticmethod
    def _analysis(text, refs):
        return {
            "fundamental_analysis": text,
            "earnings_and_news_analysis": "Nessun claim quantitativo.",
            "price_context": "Nessun claim quantitativo.",
            "bull_case": "Nessun claim quantitativo.",
            "bear_case": "Nessun claim quantitativo.",
            "catalysts": [], "risks": [],
            "thesis_invalidation": "Nessun claim quantitativo.",
            "claim_refs": refs,
        }

    @staticmethod
    def _ref(text, fact_ids, *, claim_id="claim:LIN:fundamental:1", ticker="LIN"):
        return {"claim_id": claim_id, "field": "fundamental_analysis", "text": text,
                "fact_ids": fact_ids, "materiality": "MATERIAL", "period": "2026Q2"}

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

    def test_structured_claim_accepts_percent_rounding(self):
        fact_id = "fact:LIN:2026Q2:operating_margin"
        text = "Il margine operativo e stato 27,49%."
        facts = self._claim_facts(self._fact(fact_id, 27.4949, "PERCENT"))
        result = validate_claim_refs(facts, self._analysis(text, [self._ref(text, [fact_id])]))
        self.assertEqual(len(result.valid_claim_refs), 1)
        self.assertEqual(result.rejected_claim_refs, [])

    def test_structured_claim_accepts_absolute_usd_as_billions(self):
        fact_id = "fact:LIN:2026Q2:cash"
        text = "La cassa e pari a 309.811 billion USD."
        facts = self._claim_facts(self._fact(fact_id, 309811000000, scale=1))
        result = validate_claim_refs(facts, self._analysis(text, [self._ref(text, [fact_id])]))
        self.assertEqual(len(result.valid_claim_refs), 1)

    def test_structured_claim_accepts_millions_as_absolute_value(self):
        fact_id = "fact:LIN:2026Q2:revenue"
        text = "I ricavi sono USD 25,310,000."
        facts = self._claim_facts(self._fact(fact_id, 25.31, "USD_MILLIONS", scale=1_000_000))
        result = validate_claim_refs(facts, self._analysis(text, [self._ref(text, [fact_id])]))
        self.assertEqual(len(result.valid_claim_refs), 1)

    def test_structured_claim_ignores_calendar_dates_and_quarter_ordinals(self):
        fact_id = "fact:LIN:2026Q2:revenue"
        text = "Nel 2Â° trimestre terminato il 30 giugno 2026, i ricavi sono USD 25,31 miliardi."
        facts = self._claim_facts(self._fact(fact_id, 25310000000))
        result = validate_claim_refs(facts, self._analysis(text, [self._ref(text, [fact_id])]))
        self.assertEqual(len(result.valid_claim_refs), 1)

    def test_structured_claim_accepts_explicit_loss_magnitude(self):
        fact_id = "fact:LIN:2026-09-11:return_20d"
        text = "Il titolo ha perso il 7,4058% su 20 giorni."
        fact = self._fact(fact_id, -7.4058, "PERCENT", period="2026-09-11")
        ref = self._ref(text, [fact_id])
        ref["period"] = "2026-09-11"
        result = validate_claim_refs(self._claim_facts(fact), self._analysis(text, [ref]))
        self.assertEqual(len(result.valid_claim_refs), 1)

    def test_structured_claim_ignores_iso_date_and_exhibit_number(self):
        fact_id = "fact:LIN:2026Q2:current_price"
        text = "Alla data 2026-09-11, Exhibit 99 riporta USD 82,31."
        fact = self._fact(fact_id, 82.31, period="2026Q2")
        result = validate_claim_refs(
            self._claim_facts(fact), self._analysis(text, [self._ref(text, [fact_id])]))
        self.assertEqual(result.rejected_claim_refs, [])

    def test_structured_claim_accepts_negative_word_for_loss_magnitude(self):
        fact_id = "fact:LIN:2026-09-11:return_5d"
        text = "Rendimento negativo del 4,6152% su 5 giorni."
        fact = self._fact(fact_id, -4.6152, "PERCENT", period="2026-09-11")
        ref = self._ref(text, [fact_id])
        ref["period"] = "2026-09-11"
        result = validate_claim_refs(self._claim_facts(fact), self._analysis(text, [ref]))
        self.assertEqual(result.rejected_claim_refs, [])

    def test_structured_claim_rejects_unregistered_subtraction(self):
        first = self._fact("fact:LIN:2026Q2:revenue_current", 111.2, "USD_BILLIONS",
                           scale=1_000_000_000)
        second = self._fact("fact:LIN:2026Q1:revenue_previous", 109.4, "USD_BILLIONS",
                            period="2026Q1", scale=1_000_000_000)
        text = "Il calo sequenziale e stato 1,8 miliardi USD."
        facts = self._claim_facts(first, second)
        ref = self._ref(text, [first["fact_id"], second["fact_id"]])
        result = validate_claim_refs(facts, self._analysis(text, [ref]))
        self.assertIn("UNREGISTERED_DERIVATION_OR_VALUE",
                      result.rejected_claim_refs[0]["validation_errors"])

    def test_structured_claim_rejects_unregistered_sum(self):
        current = self._fact("fact:LIN:2026Q2:current_debt", 11007000000)
        long_term = self._fact("fact:LIN:2026Q2:long_term_debt", 71340000000)
        text = "Il debito complessivo e USD 82,347 miliardi."
        facts = self._claim_facts(current, long_term)
        result = validate_claim_refs(
            facts, self._analysis(text, [self._ref(text, [current["fact_id"], long_term["fact_id"]])]))
        self.assertEqual(len(result.rejected_claim_refs), 1)

    def test_structured_claim_rejects_unregistered_guidance_delta(self):
        current = self._fact("fact:LIN:2026:guidance_current", [17.7, 17.9], "USD_PER_SHARE",
                             period="2026")
        previous = self._fact("fact:LIN:2026:guidance_previous", [17.6, 17.9], "USD_PER_SHARE",
                              period="2026")
        text = "Il limite inferiore e aumentato di USD 0,10 per azione."
        ref = self._ref(text, [current["fact_id"], previous["fact_id"]])
        ref["period"] = "2026"
        result = validate_claim_refs(self._claim_facts(current, previous), self._analysis(text, [ref]))
        self.assertEqual(len(result.rejected_claim_refs), 1)

    def test_structured_claim_rejects_unknown_fact_id(self):
        text = "I ricavi sono USD 10 miliardi."
        result = validate_claim_refs(self._claim_facts(), self._analysis(
            text, [self._ref(text, ["fact:LIN:2026Q2:missing"])]))
        self.assertIn("UNKNOWN_FACT_ID", result.rejected_claim_refs[0]["validation_errors"])

    def test_structured_claim_rejects_unknown_evidence_id(self):
        fact = self._fact("fact:LIN:2026Q2:revenue", 10, "USD_BILLIONS",
                          evidence_id="missing", scale=1_000_000_000)
        text = "I ricavi sono USD 10 miliardi."
        result = validate_claim_refs(self._claim_facts(fact), self._analysis(
            text, [self._ref(text, [fact["fact_id"]])]))
        self.assertIn("UNKNOWN_EVIDENCE_ID", result.rejected_claim_refs[0]["validation_errors"])

    def test_structured_claim_rejects_fact_from_other_ticker(self):
        fact = self._fact("fact:AAPL:2026Q2:revenue", 10, "USD_BILLIONS", ticker="AAPL",
                          scale=1_000_000_000)
        text = "I ricavi sono USD 10 miliardi."
        result = validate_claim_refs(self._claim_facts(fact), self._analysis(
            text, [self._ref(text, [fact["fact_id"]])]))
        self.assertIn("FOREIGN_COMPANY_FACT", result.rejected_claim_refs[0]["validation_errors"])

    def test_invalid_claim_does_not_remove_valid_claim_in_same_field(self):
        fact = self._fact("fact:LIN:2026Q2:revenue", 10, "USD_BILLIONS",
                          scale=1_000_000_000)
        valid_text = "I ricavi sono USD 10 miliardi."
        invalid_text = "Il debito e USD 999 miliardi."
        refs = [self._ref(valid_text, [fact["fact_id"]], claim_id="claim:LIN:fundamental:1"),
                self._ref(invalid_text, [fact["fact_id"]], claim_id="claim:LIN:fundamental:2")]
        result = validate_claim_refs(
            self._claim_facts(fact), self._analysis(valid_text + " " + invalid_text, refs))
        self.assertIn(valid_text, result.revised_analysis["fundamental_analysis"])
        self.assertNotIn(invalid_text, result.revised_analysis["fundamental_analysis"])

    def test_critic_watch_medium_is_not_degraded_for_valid_presentation(self):
        fact_id = "fact:LIN:2026Q2:operating_margin"
        text = "Il margine operativo e stato 27,49%."
        facts = self._claim_facts(self._fact(fact_id, 27.4949, "PERCENT"))
        response = {"notes": [], "status": "WATCH", "confidence": "MEDIUM",
                    "revised_analysis": self._analysis(text, [self._ref(text, [fact_id])])}

        class StaticProvider(USAProvider):
            def _request(self, prompt, schema):
                return response

        result = StaticProvider().critique(facts, {})
        self.assertEqual((result["status"], result["confidence"]), ("WATCH", "MEDIUM"))

    def test_structured_claim_never_accepts_invented_number(self):
        fact_id = "fact:LIN:2026Q2:revenue"
        fact = self._fact(fact_id, 10, "USD_BILLIONS", scale=1_000_000_000)
        text = "I ricavi sono USD 999 miliardi."
        result = validate_claim_refs(self._claim_facts(fact), self._analysis(
            text, [self._ref(text, [fact_id])]))
        self.assertEqual(result.valid_claim_refs, [])
        self.assertIn("UNREGISTERED_DERIVATION_OR_VALUE",
                      result.rejected_claim_refs[0]["validation_errors"])

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
                    for metric in event["facts"].get("reported_metrics", []):
                        self.assertIn(metric["unit"], {"USD_MILLIONS", "USD_BILLIONS", "USD_TRILLIONS"})
                        self.assertIn("$", metric["source_excerpt"])
                thesis = analyze_company(pack.company_input, AS_OF, FakeProvider())
                self.assertEqual(thesis.ticker, ticker)
                self.assertEqual(thesis.status, "PASS")
                self.assertEqual(thesis.confidence, "LOW")

    @unittest.skipUnless(PRICES.is_dir() and NEWS.is_dir(), "frozen EODHD cache unavailable")
    def test_all_numeric_facts_have_stable_identity_and_existing_evidence(self):
        for ticker in COMPANIES:
            with self.subTest(ticker=ticker):
                pack = load_company(ticker, AS_OF)
                evidence_ids = {item["identifier"] for item in pack.company_input["evidence"]}
                fact_ids = []
                for metadata in pack.company_input["price"]["acquisition"]["fact_metadata"]:
                    self.assertEqual(metadata["ticker"], ticker)
                    self.assertIn(metadata["evidence_id"], evidence_ids)
                    fact_ids.append(metadata["fact_id"])
                for event in pack.company_input["events"]:
                    for key in ("reported_metrics", "guidance_ranges", "guidance_changes",
                                "structured_sec_facts"):
                        for fact in event["facts"].get(key, []):
                            for required in ("fact_id", "ticker", "metric", "value", "unit",
                                             "period", "evidence_id", "extraction_method"):
                                self.assertIn(required, fact)
                            self.assertEqual(fact["ticker"], ticker)
                            self.assertIn(fact["evidence_id"], evidence_ids)
                            fact_ids.append(fact["fact_id"])
                self.assertEqual(len(fact_ids), len(set(fact_ids)))

    @unittest.skipUnless(PRICES.is_dir() and NEWS.is_dir(), "frozen EODHD cache unavailable")
    def test_evidence_hashes_resolve_to_original_cache_records(self):
        evidence = load_company("AAPL").company_input["evidence"]
        price = evidence[0]
        self.assertEqual(hashlib.sha256(Path(price["raw_path"]).read_bytes()).hexdigest(),
                         price["content_sha256"])
        release = evidence[1]
        local = Path(release["raw_path"])
        if local.is_file():
            content = local.read_bytes()
        else:
            path, line_number = release["raw_path"].rsplit(":", 1)
            line = Path(path).read_text(encoding="utf-8").splitlines(keepends=True)[int(line_number) - 1]
            content = line.encode("utf-8")
        self.assertEqual(hashlib.sha256(content).hexdigest(), release["content_sha256"])

    def test_future_cutoff_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "exceeds frozen"):
            load_company("AAPL", "2026-09-13")


if __name__ == "__main__":
    unittest.main()
