import json

import pytest

from trinity.usa_documents import SEC_COMPANIES
from trinity.usa_issuer_registry import (
    IssuerRegistryError,
    REGISTRY,
    get_issuer,
    load_registry,
)
from trinity.usa_v2 import COMPANIES


ORIGINAL = {
    "AAPL": ("Apple", "TECHNOLOGY", ("Apple",), "0000320193"),
    "JPM": ("JPMorgan Chase", "BANK", ("JPMorganChase", "JPMorgan Chase"), "0000019617"),
    "JNJ": ("Johnson & Johnson", "HEALTHCARE", ("Johnson & Johnson",), "0000200406"),
    "XOM": ("Exxon Mobil", "ENERGY", ("ExxonMobil",), "0000034088"),
    "WMT": ("Walmart", "CONSUMER_STAPLES", ("Walmart",), "0000104169"),
    "CAT": ("Caterpillar", "INDUSTRIAL", ("Caterpillar",), "0000018230"),
    "NEE": ("NextEra Energy", "UTILITY", ("NextEra Energy",), "0000753308"),
    "AMZN": ("Amazon", "CONSUMER_DISCRETIONARY", ("Amazon.com",), "0001018724"),
    "PLD": ("Prologis", "REAL_ESTATE", ("Prologis",), "0001045609"),
    "LIN": ("Linde", "MATERIALS", ("Linde",), "0001707925"),
}


def _payload(rows):
    return {
        "schema_name": "trinity.usa-issuer-registry", "schema_version": "1",
        "canonical_ticker_count": len(rows), "issuers": rows,
    }


def _row(ticker="AAA", cik="0000000001", **changes):
    value = {
        "ticker": ticker, "provider_symbol": f"{ticker}.US",
        "company_name": f"{ticker} Corp", "sec_cik": cik, "sec_ticker": ticker,
        "sec_issuer_name": f"{ticker} Corp", "sec_sic": 7372,
        "sec_sic_description": "Services-Prepackaged Software",
        "schema_type": "GENERAL", "aliases": [f"{ticker} Corp"],
        "supported": True, "unsupported_reason": None,
        "provenance": {"sec": "authoritative-test"},
    }
    value.update(changes)
    return value


def _write(tmp_path, rows):
    path = tmp_path / "registry.fixture"
    path.write_text(json.dumps(_payload(rows)), encoding="utf-8")
    return path


def test_canonical_registry_has_exact_518_supported_tickers():
    assert len(REGISTRY.records) == 518
    assert len(REGISTRY.supported) == 518
    assert not REGISTRY.unsupported
    assert len(COMPANIES) == 518
    assert len(SEC_COMPANIES) == 518


@pytest.mark.parametrize("ticker,cik,name", [
    ("GDDY", "0001609711", "GoDaddy Inc."),
    ("MRK", "0000310158", "Merck & Co., Inc."),
    ("WDAY", "0001327811", "Workday, Inc."),
])
def test_acceptance_ticker_has_authoritative_identity(ticker, cik, name):
    issuer = get_issuer(ticker)
    assert issuer.sec_cik == cik
    assert issuer.sec_issuer_name == name
    assert issuer.provider_symbol == f"{ticker}.US"
    assert issuer.schema_type == "GENERAL"
    assert SEC_COMPANIES[ticker] == (cik, "GENERAL")


def test_original_ten_are_byte_semantically_unchanged():
    for ticker, (name, schema, aliases, cik) in ORIGINAL.items():
        assert COMPANIES[ticker] == (name, schema, aliases)
        assert SEC_COMPANIES[ticker] == (cik, schema)


def test_duplicate_ticker_is_rejected(tmp_path):
    with pytest.raises(IssuerRegistryError, match="duplicate canonical ticker"):
        load_registry(_write(tmp_path, [_row(), _row()]))


def test_duplicate_cik_security_identity_is_rejected(tmp_path):
    rows = [_row(), _row("BBB", provider_symbol="BBB.US", sec_ticker="AAA")]
    with pytest.raises(IssuerRegistryError, match="duplicate SEC CIK/ticker identity"):
        load_registry(_write(tmp_path, rows))


def test_missing_metadata_is_rejected(tmp_path):
    with pytest.raises(IssuerRegistryError, match="missing company_name"):
        load_registry(_write(tmp_path, [_row(company_name="")]))


def test_unsupported_ticker_rejections_are_explicit(tmp_path):
    registry = load_registry(_write(tmp_path, [_row(
        supported=False, unsupported_reason="SEC_SIC_UNAVAILABLE",
    )]))
    with pytest.raises(IssuerRegistryError, match="SEC_SIC_UNAVAILABLE"):
        registry.require_supported("AAA")
    with pytest.raises(IssuerRegistryError, match="outside canonical"):
        REGISTRY.require_supported("ZZZZ")


def test_authoritative_share_classes_have_distinct_security_identity():
    goog, googl = get_issuer("GOOG"), get_issuer("GOOGL")
    assert goog.sec_cik == googl.sec_cik
    assert goog.sec_ticker != googl.sec_ticker
