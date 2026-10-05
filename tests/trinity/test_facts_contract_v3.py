from __future__ import annotations

import hashlib
import json

import pytest

from trinity.italia_v1 import (
    ItaliaV1InputError, build_facts, build_facts_v2, build_facts_v3,
)
from trinity.ledger import ArtifactIntegrityError, canonical_decimal
from trinity.ledger.contracts_v14 import _validate_event_v3
from trinity.usa_v2 import USAProvider, calibrate_decision, with_expectation_comparisons


MRK_Q1 = (
    "Full-Year 2026 Financial Outlook. Narrows and Raises the Midpoint of Worldwide Sales "
    "Range; Now Expects Sales To Be Between $65.8 Billion and $67.0 Billion. Narrows and "
    "Raises Expected Non-GAAP EPS Range To Be Between $5.04 and $5.16. Outlook Does Not "
    "Reflect Any Impact From Proposed Acquisition of Terns Pharmaceuticals, Inc."
)
MRK_Q2 = (
    "Full-Year 2026 Financial Outlook. Narrows and Raises Expected Worldwide Sales Range "
    "To Be Between $66.3 Billion and $67.3 Billion. Now Expects Non-GAAP EPS To Be Between "
    "$2.66 and $2.76; Outlook Includes Charges of $2.43 per Share for the Acquisition of Terns."
)
WDAY_Q2 = (
    "We now expect fiscal 2027 subscription revenue of $9.940 billion to $9.950 billion "
    "while increasing our fiscal 2027 non-GAAP operating margin guidance to 31.0%. "
    "Financial Outlook. Workday is providing guidance for the fiscal 2027 third quarter "
    "ending October 31, 2026 as follows: Subscription revenues of $2.515 billion and "
    "non-GAAP operating margin of 30.0%. Workday is updating guidance for the fiscal 2027 "
    "full year ending January 31, 2027 as follows: Subscription revenues of $9.940 billion "
    "to $9.950 billion and non-GAAP operating margin of 31.0%."
)


def _event(
    evidence_id: str,
    published_at: str,
    *,
    kind: str = "RESULTS",
    primary: bool = True,
    ranges: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    source_class = "PRIMARY_SEC" if primary else "THIRD_PARTY_REPORT"
    return {
        "kind": kind,
        "published_at": published_at,
        "summary": evidence_id,
        "facts": {
            "evidence_identifier": evidence_id,
            "novelty": "NEW_PRIMARY_DOCUMENT" if primary and kind == "RESULTS" else (
                None if primary else "NEW_RELEASE"
            ),
            "issuer_release": None,
            "primary_source": True if primary else None,
            "reported_metrics": [],
            "guidance_ranges": ranges or [],
            "guidance_language": None,
            "guidance_changes": [],
            "structured_sec_facts": [],
            "source_class": source_class,
            "confirmation_state": "CONFIRMED" if primary else "REPORTED",
        },
        "guidance": None,
    }


def _evidence(evidence_id: str, published_at: str, excerpt: str) -> dict[str, object]:
    return {
        "source": "SEC EDGAR" if evidence_id.startswith("sec:") else "issuer news",
        "published_at": published_at,
        "identifier": evidence_id,
        "url": f"https://example.test/{evidence_id}",
        "excerpt": excerpt or "No additional expectation language.",
    }


def _company(
    events: list[dict[str, object]], evidence: list[dict[str, object]], *, ticker: str = "MRK",
) -> dict[str, object]:
    return {
        "company_name": ticker, "ticker": ticker, "provider_symbol": f"{ticker}.US",
        "as_of": "2026-10-02", "schema_type": "GENERAL", "price": {},
        "fundamentals": {}, "financial_facts": {}, "events": events, "evidence": evidence,
    }


def _comparisons(company: dict[str, object]) -> dict[tuple[str, str], dict[str, object]]:
    normalized = with_expectation_comparisons(company)
    return {
        (event["facts"]["evidence_identifier"], item["metric"]): item
        for event in normalized["events"]
        for item in event["facts"]["expectation_comparisons"]
    }


def _range(
    evidence_id: str, metric: str, period: str, unit: str, low: float,
    high: float | None = None, *, excerpt: str = "full-year consolidated guidance",
) -> dict[str, object]:
    value = [low] if high is None else [low, high]
    return {
        "metric": metric, "period": period, "unit": unit,
        "previous_range": None, "current_range": value,
        "source_excerpt": excerpt, "evidence_identifier": evidence_id,
        "extraction_method": "literal_guidance_range_explicit_usd_per_share",
    }


def _two_structured_expectations(
    prior_value: list[float], current_value: list[float], *,
    prior_period: str = "FY2027", current_period: str = "FY2027",
    prior_unit: str = "PERCENT", current_unit: str = "PERCENT",
    prior_excerpt: str = "full-year consolidated guidance",
    current_excerpt: str = "full-year consolidated guidance",
) -> dict[tuple[str, str], dict[str, object]]:
    old = "sec:TEST:old"
    new = "sec:TEST:new"
    prior_range = _range(
        old, "NON_GAAP_OPERATING_MARGIN", prior_period, prior_unit,
        prior_value[0], prior_value[1] if len(prior_value) == 2 else None,
        excerpt=prior_excerpt,
    )
    current_range = _range(
        new, "NON_GAAP_OPERATING_MARGIN", current_period, current_unit,
        current_value[0], current_value[1] if len(current_value) == 2 else None,
        excerpt=current_excerpt,
    )
    return _comparisons(_company(
        [_event(old, "2026-05-01", ranges=[prior_range]),
         _event(new, "2026-07-31", ranges=[current_range])],
        [_evidence(old, "2026-05-01", ""), _evidence(new, "2026-07-31", "")],
        ticker="TEST",
    ))


def _two_raw_margin_points(prior: float, current: float) -> dict[str, object]:
    old = "sec:TEST:old"
    new = "sec:TEST:new"
    company = _company(
        [_event(old, "2026-05-01"), _event(new, "2026-07-31")],
        [_evidence(
            old, "2026-05-01",
            f"Full-Year 2027 consolidated guidance expects non-GAAP operating margin of {prior}%.",
        ), _evidence(
            new, "2026-07-31",
            f"Full-Year 2027 consolidated guidance expects non-GAAP operating margin of {current}%.",
        )],
        ticker="TEST",
    )
    normalized = with_expectation_comparisons(company)
    facts = build_facts_v3(normalized, "2026-10-02")
    return next(
        item for event in facts["events"]
        if event["facts"]["evidence_identifier"] == new
        for item in event["facts"]["expectation_comparisons"]
        if item["metric"] == "NON_GAAP_OPERATING_MARGIN"
    )


def test_point_to_point_increase_is_safe_and_raised():
    comparison = _two_raw_margin_points(30.0, 31.0)
    assert comparison["expectation_comparison_state"] == "VERIFIED_CHANGE"
    assert comparison["direction"] == "RAISED"


def test_point_to_point_decrease_is_safe_and_lowered():
    comparison = _two_raw_margin_points(31.0, 30.0)
    assert comparison["expectation_comparison_state"] == "VERIFIED_CHANGE"
    assert comparison["direction"] == "LOWERED"


def test_point_to_range_abstains():
    comparison = _two_structured_expectations([30.0], [30.0, 31.0])[(
        "sec:TEST:new", "NON_GAAP_OPERATING_MARGIN",
    )]
    assert comparison["expectation_comparison_state"] == "AMBIGUOUS"
    assert comparison["comparability_reason_codes"] == ["VALUE_SHAPE_MISMATCH"]


def test_range_to_point_abstains():
    comparison = _two_structured_expectations([30.0, 31.0], [31.0])[(
        "sec:TEST:new", "NON_GAAP_OPERATING_MARGIN",
    )]
    assert comparison["expectation_comparison_state"] == "AMBIGUOUS"
    assert comparison["comparability_reason_codes"] == ["VALUE_SHAPE_MISMATCH"]


def test_annual_and_quarterly_same_year_do_not_compare():
    comparison = _two_structured_expectations(
        [30.0], [31.0], prior_period="FY2027", current_period="2027Q2",
    )[("sec:TEST:new", "NON_GAAP_OPERATING_MARGIN")]
    assert comparison["expectation_comparison_state"] == "NOT_ESTABLISHED"
    assert comparison["comparability_reason_codes"] == ["PERIOD_GRANULARITY_MISMATCH"]


def test_q2_and_q3_same_year_do_not_compare():
    comparison = _two_structured_expectations(
        [30.0], [31.0], prior_period="2027Q2", current_period="2027Q3",
    )[("sec:TEST:new", "NON_GAAP_OPERATING_MARGIN")]
    assert comparison["expectation_comparison_state"] == "NOT_ESTABLISHED"
    assert comparison["comparability_reason_codes"] == ["TARGET_FISCAL_PERIOD_MISMATCH"]


def test_historical_actual_range_without_forward_language_is_not_extracted():
    event_id = "sec:TEST:actual"
    company = _company(
        [_event(event_id, "2026-07-31")],
        [_evidence(event_id, "2026-07-31",
                   "Fiscal 2027 second-quarter results: worldwide sales were "
                   "$2.0 billion to $2.2 billion across reported regions.")],
        ticker="TEST",
    )
    assert with_expectation_comparisons(company)["events"][0]["facts"]["expectation_comparisons"] == []


def test_explicit_forward_guidance_range_is_extracted():
    event_id = "sec:TEST:guidance"
    company = _company(
        [_event(event_id, "2026-07-31")],
        [_evidence(event_id, "2026-07-31",
                   "Full-Year 2027 consolidated outlook now expects worldwide sales "
                   "between $12.0 billion and $13.0 billion.")],
        ticker="TEST",
    )
    comparison = _comparisons(company)[(event_id, "SALES")]
    assert comparison["current_value"] == [12.0, 13.0]
    assert comparison["expectation_comparison_state"] == "NOT_ESTABLISHED"


def test_previously_then_now_uses_only_explicit_current_value():
    event_id = "sec:TEST:update"
    text = (
        "Full-Year 2027 consolidated guidance previously expected worldwide sales "
        "between $10.0 billion and $11.0 billion. Full-Year 2027 consolidated outlook "
        "now expects worldwide sales between $12.0 billion and $13.0 billion."
    )
    comparison = _comparisons(_company(
        [_event(event_id, "2026-07-31")], [_evidence(event_id, "2026-07-31", text)],
        ticker="TEST",
    ))[(event_id, "SALES")]
    assert comparison["current_value"] == [12.0, 13.0]
    assert comparison["expectation_comparison_state"] == "NOT_ESTABLISHED"


def test_unresolved_duplicate_current_ranges_are_ambiguous():
    event_id = "sec:TEST:duplicate"
    text = (
        "Full-Year 2027 consolidated guidance expects worldwide sales between "
        "$10.0 billion and $11.0 billion. Full-Year 2027 consolidated guidance expects "
        "worldwide sales between $12.0 billion and $13.0 billion."
    )
    comparison = _comparisons(_company(
        [_event(event_id, "2026-07-31")], [_evidence(event_id, "2026-07-31", text)],
        ticker="TEST",
    ))[(event_id, "SALES")]
    assert comparison["expectation_comparison_state"] == "AMBIGUOUS"
    assert comparison["current_value"] is None
    assert comparison["comparability_reason_codes"] == ["MULTIPLE_CURRENT_VALUES"]


def test_unspecified_perimeter_on_both_sides_abstains():
    comparison = _two_structured_expectations(
        [30.0], [31.0], prior_excerpt="full-year guidance",
        current_excerpt="full-year guidance",
    )[("sec:TEST:new", "NON_GAAP_OPERATING_MARGIN")]
    assert comparison["expectation_comparison_state"] == "AMBIGUOUS"
    assert comparison["comparability_reason_codes"] == ["BASIS_NOT_ESTABLISHED"]


def test_explicit_perimeter_mismatch_is_ambiguous():
    comparison = _two_structured_expectations(
        [30.0], [31.0],
        prior_excerpt="full-year guidance excludes transaction impact",
        current_excerpt="full-year guidance includes transaction impact",
    )[("sec:TEST:new", "NON_GAAP_OPERATING_MARGIN")]
    assert comparison["expectation_comparison_state"] == "AMBIGUOUS"
    assert comparison["comparability_reason_codes"] == ["PERIMETER_BASIS_MISMATCH"]


def test_segment_mismatch_is_ambiguous():
    comparison = _two_structured_expectations(
        [30.0], [31.0], prior_excerpt="full-year consolidated guidance",
        current_excerpt="full-year segment guidance",
    )[("sec:TEST:new", "NON_GAAP_OPERATING_MARGIN")]
    assert comparison["expectation_comparison_state"] == "AMBIGUOUS"
    assert comparison["comparability_reason_codes"] == ["SEGMENT_SCOPE_NOT_ESTABLISHED"]


def test_currency_mismatch_is_ambiguous():
    comparison = _two_structured_expectations(
        [30.0], [31.0], prior_unit="USD_PER_SHARE", current_unit="EUR_PER_SHARE",
    )[("sec:TEST:new", "NON_GAAP_OPERATING_MARGIN")]
    assert comparison["expectation_comparison_state"] == "AMBIGUOUS"
    assert comparison["comparability_reason_codes"] == ["CURRENCY_MISMATCH"]


def test_mrk_frozen_expectations_normalize_sales_and_eps_independently():
    q1 = "sec:MRK:0001104659-26-052081:ex-99-1"
    q2 = "sec:MRK:0001104659-26-090045:ex-99-1"
    company = _company(
        [_event(q1, "2026-04-30"), _event(q2, "2026-08-04")],
        [_evidence(q1, "2026-04-30", MRK_Q1), _evidence(q2, "2026-08-04", MRK_Q2)],
    )
    result = _comparisons(company)
    sales = result[(q2, "SALES")]
    eps = result[(q2, "NON_GAAP_EPS")]
    assert sales["expectation_comparison_state"] == "VERIFIED_CHANGE"
    assert sales["direction"] == "RAISED"
    assert sales["prior_value"] == [65.8, 67.0]
    assert sales["current_value"] == [66.3, 67.3]
    assert eps["expectation_comparison_state"] == "AMBIGUOUS"
    assert eps["direction"] is None
    assert eps["comparability_reason_codes"] == ["PERIMETER_BASIS_MISMATCH"]


def test_wday_frozen_ids_do_not_fabricate_expectation_change_and_keep_calibration_semantics():
    release = "sec:WDAY:0001327811-26-000042:ex-99-1"
    ten_q = "sec:WDAY:0001327811-26-000044:10-q"
    company = _company(
        [_event(release, "2026-08-27"),
         _event(ten_q, "2026-08-27", kind="SEC_PERIODIC_REPORT")],
        [_evidence(release, "2026-08-27", WDAY_Q2),
         _evidence(ten_q, "2026-08-27", "The 10-Q confirms the reported GAAP results.")],
        ticker="WDAY",
    )
    normalized = with_expectation_comparisons(company)
    facts_v3 = build_facts_v3(normalized, "2026-10-02")
    comparisons = [item for event in facts_v3["events"]
                   for item in event["facts"]["expectation_comparisons"]]
    assert {item["metric"] for item in comparisons} == {
        "SUBSCRIPTION_REVENUE", "NON_GAAP_OPERATING_MARGIN",
    }
    assert {item["expectation_comparison_state"] for item in comparisons} == {
        "NOT_ESTABLISHED",
    }
    assert not any(
        item["expectation_comparison_state"] == "VERIFIED_CHANGE"
        for item in comparisons
    )
    assert {
        (item["metric"], item["target_fiscal_period"], item["target_period_granularity"])
        for item in comparisons
    } == {
        ("SUBSCRIPTION_REVENUE", "2027", "UNKNOWN"),
        ("SUBSCRIPTION_REVENUE", "FY2027", "ANNUAL"),
        ("NON_GAAP_OPERATING_MARGIN", "2027", "UNKNOWN"),
        ("NON_GAAP_OPERATING_MARGIN", "2027Q3", "QUARTERLY"),
        ("NON_GAAP_OPERATING_MARGIN", "FY2027", "ANNUAL"),
    }
    assessments = [
        {"event_id": release, "classification": "NEW_INFORMATION", "material": True},
        {"event_id": ten_q, "classification": "CONFIRMATION", "material": False},
    ]
    assert calibrate_decision(assessments, "MEDIUM") == "WATCH"


def test_same_evidence_metric_with_annual_and_quarterly_facets_is_valid():
    event_id = "sec:TEST:multi-period"
    company = _company(
        [_event(event_id, "2026-07-31", ranges=[
            _range(event_id, "NON_GAAP_OPERATING_MARGIN", "FY2027", "PERCENT", 31.0),
            _range(event_id, "NON_GAAP_OPERATING_MARGIN", "2027Q3", "PERCENT", 30.0),
        ])],
        [_evidence(event_id, "2026-07-31", "")], ticker="TEST",
    )
    facts = build_facts_v3(with_expectation_comparisons(company), "2026-10-02")
    facets = facts["events"][0]["facts"]["expectation_comparisons"]
    assert {
        (item["target_fiscal_period"], item["target_period_granularity"])
        for item in facets
    } == {("FY2027", "ANNUAL"), ("2027Q3", "QUARTERLY")}


def test_ledger_uses_period_facet_identity_and_rejects_exact_duplicate():
    event_id = "sec:TEST:ledger-multi-period"
    company = _company(
        [_event(event_id, "2026-07-31", ranges=[
            _range(event_id, "NON_GAAP_OPERATING_MARGIN", "FY2027", "PERCENT", 31.0),
            _range(event_id, "NON_GAAP_OPERATING_MARGIN", "2027Q3", "PERCENT", 30.0),
        ])],
        [_evidence(event_id, "2026-07-31", "")], ticker="TEST",
    )
    facts = build_facts_v3(with_expectation_comparisons(company), "2026-10-02")
    ledger_event = json.loads(json.dumps(facts["events"][0]))
    ledger_event["facts"]["guidance_ranges"] = []
    for facet in ledger_event["facts"]["expectation_comparisons"]:
        facet["current_value"] = [canonical_decimal(value) for value in facet["current_value"]]
        if facet["prior_value"] is not None:
            facet["prior_value"] = [canonical_decimal(value) for value in facet["prior_value"]]
    _validate_event_v3(ledger_event, set(), "TEST")
    duplicate = json.loads(json.dumps(ledger_event))
    facets = duplicate["facts"]["expectation_comparisons"]
    facets.append(json.loads(json.dumps(facets[0])))
    with pytest.raises(ArtifactIntegrityError, match="duplicate expectation comparison"):
        _validate_event_v3(duplicate, set(), "TEST")


def test_exact_same_expectation_facet_duplicate_is_rejected():
    event_id = "sec:TEST:duplicate-facet"
    company = _company(
        [_event(event_id, "2026-07-31", ranges=[
            _range(event_id, "NON_GAAP_OPERATING_MARGIN", "FY2027", "PERCENT", 31.0),
        ])],
        [_evidence(event_id, "2026-07-31", "")], ticker="TEST",
    )
    normalized = with_expectation_comparisons(company)
    event = normalized["events"][0]
    event["facts"]["expectation_comparisons"].append(
        json.loads(json.dumps(event["facts"]["expectation_comparisons"][0]))
    )
    with pytest.raises(ItaliaV1InputError, match="duplicate expectation facet"):
        build_facts_v3(normalized, "2026-10-02")


def test_dominated_unknown_facet_is_removed_only_for_same_textual_observation():
    event_id = "sec:TEST:dominated-unknown"
    statement = "Fiscal 2027 guidance expects non-GAAP operating margin of 31.0%."
    company = _company(
        [_event(event_id, "2026-07-31", ranges=[
            _range(
                event_id, "NON_GAAP_OPERATING_MARGIN", "FY2027", "PERCENT", 31.0,
                excerpt=statement,
            ),
        ])],
        [_evidence(event_id, "2026-07-31", statement)], ticker="TEST",
    )
    facts = build_facts_v3(with_expectation_comparisons(company), "2026-10-02")
    facets = facts["events"][0]["facts"]["expectation_comparisons"]
    assert len(facets) == 1
    assert facets[0]["target_fiscal_period"] == "FY2027"
    assert facets[0]["target_period_granularity"] == "ANNUAL"


def test_independent_unknown_facet_is_not_silently_removed():
    event_id = "sec:TEST:independent-unknown"
    text = (
        "Fiscal 2027 guidance expects non-GAAP operating margin of 31.0%. "
        "Full-Year 2027 guidance expects non-GAAP operating margin of 31.0%."
    )
    company = _company(
        [_event(event_id, "2026-07-31")],
        [_evidence(event_id, "2026-07-31", text)], ticker="TEST",
    )
    facts = build_facts_v3(with_expectation_comparisons(company), "2026-10-02")
    facets = facts["events"][0]["facts"]["expectation_comparisons"]
    assert {
        (item["target_fiscal_period"], item["target_period_granularity"])
        for item in facets
    } == {("2027", "UNKNOWN"), ("FY2027", "ANNUAL")}


def test_true_confirmation_alone_remains_pass():
    assert calibrate_decision([{
        "event_id": "sec:WDAY:0001327811-26-000044:10-q",
        "classification": "CONFIRMATION",
        "material": False,
    }], "HIGH") == "PASS"


def test_existing_spr2015_new_information_has_no_expectation_facet():
    event_id = "news:MRK:cf30c69f7e741de5"
    event = _event(event_id, "2026-09-28", kind="CORPORATE_EVENT", primary=False)
    company = _company([event], [_evidence(
        event_id, "2026-09-28",
        "Merck entered an exclusive global licence for preclinical SPR2015.",
    )])
    normalized = with_expectation_comparisons(company)
    assert normalized["events"][0]["facts"]["expectation_comparisons"] == []
    assert calibrate_decision([{
        "event_id": event_id, "classification": "NEW_INFORMATION", "material": True,
    }], "MEDIUM") == "WATCH"


def test_existing_lin_guidance_is_a_true_expectation_change():
    q1 = "sec:LIN:0001654954-26-004202:ex-99-1"
    q2 = "sec:LIN:0001654954-26-007052:ex-99-1"
    company = _company(
        [_event(q1, "2026-05-01", ranges=[_range(q1, "ADJUSTED_EPS", "2026", "USD_PER_SHARE", 17.6, 17.9)]),
         _event(q2, "2026-07-31", ranges=[_range(q2, "ADJUSTED_EPS", "2026", "USD_PER_SHARE", 17.7, 17.9)])],
        [_evidence(q1, "2026-05-01", ""), _evidence(q2, "2026-07-31", "")],
        ticker="LIN",
    )
    comparison = _comparisons(company)[(q2, "ADJUSTED_EPS")]
    assert comparison["expectation_comparison_state"] == "VERIFIED_CHANGE"
    assert comparison["direction"] == "RAISED"


def test_mixed_event_preserves_new_disclosure_facts_and_expectation_comparisons():
    q1 = "sec:MRK:0001104659-26-052081:ex-99-1"
    q2 = "sec:MRK:0001104659-26-090045:ex-99-1"
    current = _event(q2, "2026-08-04")
    current["facts"]["reported_metrics"] = [{"literal": "new Q2 result"}]
    normalized = with_expectation_comparisons(_company(
        [_event(q1, "2026-04-30"), current],
        [_evidence(q1, "2026-04-30", MRK_Q1), _evidence(q2, "2026-08-04", MRK_Q2)],
    ))
    current_normalized = next(event for event in normalized["events"] if event["published_at"] == "2026-08-04")
    assert current_normalized["facts"]["reported_metrics"] == [{"literal": "new Q2 result"}]
    assert {item["expectation_comparison_state"] for item in current_normalized["facts"]["expectation_comparisons"]} == {
        "VERIFIED_CHANGE", "AMBIGUOUS",
    }


def test_same_metric_different_target_period_is_not_established():
    old = "sec:LIN:old"
    new = "sec:LIN:new"
    company = _company(
        [_event(old, "2026-05-01", ranges=[_range(old, "ADJUSTED_EPS", "2026", "USD_PER_SHARE", 17.6, 17.9)]),
         _event(new, "2026-07-31", ranges=[_range(new, "ADJUSTED_EPS", "2027", "USD_PER_SHARE", 18.0, 18.2)])],
        [_evidence(old, "2026-05-01", ""), _evidence(new, "2026-07-31", "")], ticker="LIN",
    )
    comparison = _comparisons(company)[(new, "ADJUSTED_EPS")]
    assert comparison["expectation_comparison_state"] == "NOT_ESTABLISHED"
    assert comparison["comparability_reason_codes"] == ["TARGET_FISCAL_PERIOD_MISMATCH"]


def test_identical_range_is_verified_unchanged():
    old = "sec:LIN:old"
    new = "sec:LIN:new"
    company = _company(
        [_event(old, "2026-05-01", ranges=[_range(old, "ADJUSTED_EPS", "2026", "USD_PER_SHARE", 17.6, 17.9)]),
         _event(new, "2026-07-31", ranges=[_range(new, "ADJUSTED_EPS", "2026", "USD_PER_SHARE", 17.6, 17.9)])],
        [_evidence(old, "2026-05-01", ""), _evidence(new, "2026-07-31", "")], ticker="LIN",
    )
    comparison = _comparisons(company)[(new, "ADJUSTED_EPS")]
    assert comparison["expectation_comparison_state"] == "VERIFIED_UNCHANGED"
    assert comparison["direction"] == "UNCHANGED"


def test_missing_prior_primary_source_is_not_established():
    event_id = "sec:LIN:new"
    company = _company(
        [_event(event_id, "2026-07-31", ranges=[_range(event_id, "ADJUSTED_EPS", "2026", "USD_PER_SHARE", 17.6, 17.9)])],
        [_evidence(event_id, "2026-07-31", "")], ticker="LIN",
    )
    comparison = _comparisons(company)[(event_id, "ADJUSTED_EPS")]
    assert comparison["expectation_comparison_state"] == "NOT_ESTABLISHED"
    assert comparison["comparability_reason_codes"] == ["NO_PRIOR_PRIMARY_BASELINE"]


def test_event_order_does_not_change_comparisons():
    q1 = "sec:MRK:0001104659-26-052081:ex-99-1"
    q2 = "sec:MRK:0001104659-26-090045:ex-99-1"
    events = [_event(q1, "2026-04-30"), _event(q2, "2026-08-04")]
    evidence = [_evidence(q1, "2026-04-30", MRK_Q1), _evidence(q2, "2026-08-04", MRK_Q2)]
    forward = _comparisons(_company(events, evidence))
    reverse = _comparisons(_company(list(reversed(events)), list(reversed(evidence))))
    assert forward == reverse


def test_same_date_does_not_infer_a_prior_baseline_from_identifier_order():
    first = "sec:LIN:a"
    second = "sec:LIN:b"
    company = _company(
        [_event(first, "2026-07-31", ranges=[
            _range(first, "ADJUSTED_EPS", "2026", "USD_PER_SHARE", 17.6, 17.9),
        ]), _event(second, "2026-07-31", ranges=[
            _range(second, "ADJUSTED_EPS", "2026", "USD_PER_SHARE", 17.7, 17.9),
        ])],
        [_evidence(first, "2026-07-31", ""), _evidence(second, "2026-07-31", "")],
        ticker="LIN",
    )
    comparisons = _comparisons(company)
    assert comparisons[(first, "ADJUSTED_EPS")]["expectation_comparison_state"] == "NOT_ESTABLISHED"
    assert comparisons[(second, "ADJUSTED_EPS")]["expectation_comparison_state"] == "NOT_ESTABLISHED"


def test_facts_v3_is_additive_and_v1_v2_remain_byte_stable():
    event_id = "sec:LIN:new"
    base = _company(
        [_event(event_id, "2026-07-31", ranges=[_range(event_id, "ADJUSTED_EPS", "2026", "USD_PER_SHARE", 17.6, 17.9)])],
        [_evidence(event_id, "2026-07-31", "")], ticker="LIN",
    )
    legacy = json.loads(json.dumps(base))
    legacy["events"][0]["facts"].pop("source_class")
    legacy["events"][0]["facts"].pop("confirmation_state")
    v1_before = json.dumps(build_facts(legacy, "2026-10-02"), sort_keys=True, separators=(",", ":"))
    v2_before = json.dumps(build_facts_v2(base, "2026-10-02"), sort_keys=True, separators=(",", ":"))
    normalized = with_expectation_comparisons(base)
    v3 = build_facts_v3(normalized, "2026-10-02")
    v1_after = json.dumps(build_facts(legacy, "2026-10-02"), sort_keys=True, separators=(",", ":"))
    v2_after = json.dumps(build_facts_v2(base, "2026-10-02"), sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(v1_before.encode()).digest() == hashlib.sha256(v1_after.encode()).digest()
    assert hashlib.sha256(v2_before.encode()).digest() == hashlib.sha256(v2_after.encode()).digest()
    assert v3["facts_contract_version"] == "3"
    assert "expectation_comparisons" in v3["events"][0]["facts"]


def test_v3_precedence_rule_is_present_in_both_prompts_and_absent_from_v2():
    event_id = "sec:LIN:new"
    base = _company(
        [_event(event_id, "2026-07-31", ranges=[
            _range(event_id, "ADJUSTED_EPS", "2026", "USD_PER_SHARE", 17.6, 17.9),
        ])],
        [_evidence(event_id, "2026-07-31", "")], ticker="LIN",
    )
    v3 = build_facts_v3(with_expectation_comparisons(base), "2026-10-02")
    v2 = build_facts_v2(base, "2026-10-02")

    class PromptCaptured(RuntimeError):
        pass

    class CapturingProvider(USAProvider):
        def __init__(self):
            super().__init__()
            self.prompt = ""

        def _request(self, prompt, schema):
            self.prompt = prompt
            raise PromptCaptured

    precedence = (
        "EXPECTATION_CHANGE takes precedence over NEW_INFORMATION. "
        "NEW_INFORMATION applies only when no verified expectation-change facet exists."
    )
    provider = CapturingProvider()
    try:
        provider.analyze(v3)
    except PromptCaptured:
        pass
    assert precedence in provider.prompt
    assert "market consensus is not required" in provider.prompt

    try:
        provider.critique(v3, {})
    except PromptCaptured:
        pass
    assert precedence in provider.prompt
    assert "market consensus is not required" in provider.prompt

    try:
        provider.analyze(v2)
    except PromptCaptured:
        pass
    assert "Facts V3 expectation_comparisons" not in provider.prompt
