from __future__ import annotations

from copy import deepcopy

import pytest

from trinity.italia_v1 import build_facts, build_facts_v2
from trinity.ledger.contracts_v14 import _validate_event, _validate_event_v2
from trinity.ledger.errors import ArtifactIntegrityError
from trinity.usa_v2 import (
    _apply_material_evidence_continuity,
    _material_8k_items,
    _third_party_corporate_action,
    _third_party_event_summary,
)


def _news(
    *, title: str, content: str, ticker: str = "AAPL",
    digest: str = "a" * 64,
) -> dict[str, object]:
    return {
        "title": title,
        "content": content,
        "symbols": [f"{ticker}.US"],
        "published_ts": "2026-09-30T12:00:00Z",
        "link": "https://example.test/report",
        "canonical_record_sha256": digest,
    }


def _event(
    source_class: str, confirmation_state: str,
) -> dict[str, object]:
    primary = source_class == "PRIMARY_SEC"
    issuer = source_class == "ISSUER_RELEASE"
    return {
        "kind": "CORPORATE_EVENT",
        "published_at": "2026-09-30",
        "summary": "Material event",
        "facts": {
            "evidence_identifier": "evidence:event:1",
            "novelty": "NEW_RELEASE" if not primary else None,
            "issuer_release": True if issuer else None,
            "primary_source": True if primary else None,
            "reported_metrics": [],
            "guidance_ranges": [],
            "guidance_language": None,
            "guidance_changes": [],
            "structured_sec_facts": [],
            "source_class": source_class,
            "confirmation_state": confirmation_state,
        },
        "guidance": None,
    }


def _minimal_company(event: dict[str, object]) -> dict[str, object]:
    return {
        "company_name": "Apple Inc.", "ticker": "AAPL",
        "provider_symbol": "AAPL.US", "as_of": "2026-09-30",
        "schema_type": "TECHNOLOGY", "price": {}, "fundamentals": {},
        "financial_facts": {}, "events": [event],
        "evidence": [{
            "source": "captured source", "published_at": "2026-09-30",
            "identifier": "evidence:event:1", "url": "https://example.test/report",
            "excerpt": "Material event",
        }],
    }


def test_company_specific_takeover_report_is_admitted_as_reported():
    record = _news(
        title="Apple receives reported takeover approach",
        content="The Financial Times reported, citing people familiar with the matter, that talks began.",
    )
    assert _third_party_corporate_action(record, "AAPL") == (
        True, "REPORTED", "ADMITTED_MATERIAL_THIRD_PARTY_REPORT"
    )


def test_unrelated_acquisition_headline_is_rejected_despite_provider_symbol():
    record = _news(
        title="Acme receives reported acquisition proposal",
        content="Sources reported that Acme was approached. Apple shares were also mentioned.",
    )
    admitted, state, reason = _third_party_corporate_action(record, "AAPL")
    assert (admitted, state, reason) == (False, "UNKNOWN", "ISSUER_NOT_SPECIFIC_IN_TITLE")


def test_multi_company_market_roundup_is_not_company_specific():
    record = _news(
        title="Marvell, PayPal, Apple, and More Stocks That Explain Today's Market",
        content="Marvell led the session. Later, sources reported a possible Apple takeover approach.",
    )
    admitted, state, reason = _third_party_corporate_action(record, "AAPL")
    assert (admitted, state, reason) == (False, "UNKNOWN", "ISSUER_NOT_PRIMARY_SUBJECT")


@pytest.mark.parametrize(
    ("title", "content", "expected"),
    [
        ("Apple acquisition offer rumor", "Unconfirmed market chatter described a possible approach.", "RUMORED"),
        ("Apple go-private report", "Reuters reported, citing sources, that financing talks started.", "REPORTED"),
    ],
)
def test_third_party_uncertainty_is_preserved(title, content, expected):
    record = _news(title=title, content=content)
    admitted, state, _reason = _third_party_corporate_action(record, "AAPL")
    assert admitted is True
    assert state == expected
    record["_confirmation_state"] = state
    assert _third_party_event_summary(record).startswith(
        f"Third-party {expected.lower()} information:"
    )


def test_v2_source_contract_preserves_primary_issuer_and_third_party_branches():
    _validate_event_v2(_event("PRIMARY_SEC", "CONFIRMED"), set())
    _validate_event_v2(_event("ISSUER_RELEASE", "CONFIRMED"), set())
    _validate_event_v2(_event("THIRD_PARTY_REPORT", "REPORTED"), set())
    _validate_event_v2(_event("THIRD_PARTY_REPORT", "RUMORED"), set())
    with pytest.raises(ArtifactIntegrityError, match="confirmation state invalid"):
        _validate_event_v2(_event("THIRD_PARTY_REPORT", "CONFIRMED"), set())
    with pytest.raises(ArtifactIntegrityError, match="lacks primary source flag"):
        invalid = _event("PRIMARY_SEC", "CONFIRMED")
        invalid["facts"]["primary_source"] = None
        _validate_event_v2(invalid, set())


def test_v1_contract_is_unversioned_and_unchanged():
    event = _event("ISSUER_RELEASE", "CONFIRMED")
    event["facts"].pop("source_class")
    event["facts"].pop("confirmation_state")
    _validate_event(event, set())
    v1 = build_facts(_minimal_company(event), "2026-09-30")
    assert "facts_contract_version" not in v1
    assert "source_class" not in v1["events"][0]["facts"]


def test_v2_builder_adds_explicit_version_without_changing_v1_projection():
    event = _event("THIRD_PARTY_REPORT", "REPORTED")
    company = _minimal_company(event)
    v1 = build_facts(company, "2026-09-30")
    v2 = build_facts_v2(company, "2026-09-30")
    assert v2["facts_contract_version"] == "2"
    projection = deepcopy(v2)
    projection.pop("facts_contract_version")
    assert projection == v1


def test_mixed_earnings_restructuring_8k_preserves_second_event_classification():
    filing = {
        "document_kind": "SEC_8_K_EARNINGS",
        "accession_number": "0001327811-26-000048",
        "excerpt": "Item 2.02 Results of Operations. Item 2.05 Costs Associated with Exit or Disposal Activities.",
    }
    assert _material_8k_items(filing) == ("2.05",)
    exhibit = {
        "document_kind": "SEC_EARNINGS_RELEASE",
        "accession_number": filing["accession_number"],
    }
    assert _material_8k_items(exhibit) == ()


def test_escalation_evidence_is_admitted_or_has_explicit_exclusion_reason():
    admitted_record = _news(
        title="Apple go-private report",
        content="Reuters reported the proposal, citing sources.",
        digest="b" * 64,
    )
    admitted_id = "news:AAPL:" + "b" * 16
    rejected_id = "news:AAPL:" + "c" * 16
    wrong_ticker_id = "news:MSFT:" + "d" * 16
    unsupported_id = "earnings-calendar:AAPL.US:2026-10-29"
    chosen: list[dict[str, object]] = []
    result = _apply_material_evidence_continuity(
        (admitted_id, rejected_id, wrong_ticker_id, unsupported_id), ticker="AAPL",
        records_by_id={admitted_id: admitted_record},
        decisions={
            admitted_id: "ADMITTED_MATERIAL_THIRD_PARTY_REPORT",
            rejected_id: "NO_SUPPORTED_CORPORATE_ACTION",
        },
        chosen=chosen,
    )
    assert chosen == [admitted_record]
    assert result == [
        {"evidence_id": admitted_id, "status": "ADMITTED", "reason": "ADMITTED_MATERIAL_THIRD_PARTY_REPORT"},
        {"evidence_id": rejected_id, "status": "EXCLUDED", "reason": "NO_SUPPORTED_CORPORATE_ACTION"},
        {"evidence_id": wrong_ticker_id, "status": "EXCLUDED", "reason": "INVALID_TICKER_EVIDENCE_IDENTIFIER"},
        {"evidence_id": unsupported_id, "status": "EXCLUDED", "reason": "UNSUPPORTED_CONTINUITY_EVIDENCE_CLASS"},
    ]
