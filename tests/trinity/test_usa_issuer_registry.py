import json
import hashlib
from pathlib import Path

import pytest

from trinity.pilots.pre_research import load_canonical_universe
from trinity.usa_documents import SEC_COMPANIES
from trinity.usa_issuer_registry import (
    DEFAULT_REGISTRY,
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


def test_default_live_registry_promotes_skyd_with_auditable_psky_lineage():
    assert DEFAULT_REGISTRY.name == "usa_issuer_registry_v2.fixture"
    assert REGISTRY.get("PSKY") is None
    issuer = get_issuer("SKYD")
    assert issuer.provider_symbol == "SKYD.US"
    assert issuer.sec_cik == "0002041610"
    assert issuer.sec_ticker == "PSKY"
    assert issuer.provenance == {
        "canonical_mapping_sha256": "2e9d0e0ec05d16d1ba523a8d74483a912767c2a4a4911fe3fe3126a56d02faa5",
        "constituent_identity_sha256": "8a5d2dfb3d291e39c4065007c3b10b41ae7957abf5e5be668f04e9c50a20b587",
        "sec_ticker_source_sha256": "2b428c1502b616046f17d0d035d33ace052de5fc31e6a2c2cdcdd8c2fc32287a",
        "sec_cik_source": "SEC company_tickers_exchange.json",
        "sec_submissions_sha256": "63316a7f86afa9246818cb390f0fbf6b035c5b3a255ea673e61291333fcd64dd",
        "sec_submissions_url": "https://data.sec.gov/submissions/CIK0002041610.json",
        "previous_canonical_ticker": "PSKY",
        "identity_change": "PSKY->SKYD",
        "identity_effective_date": "2026-10-06",
        "identity_probe_run_id": "39970559903",
        "identity_probe_exchange": "NYSE",
        "identity_probe_mic": "XNYS",
        "identity_probe_first_valid_date": "2025-07-29",
        "identity_probe_last_valid_date": "2026-10-22",
        "sec_ticker_identity_status": "HISTORICAL_SOURCE_VALUE_PRESERVED",
    }


def test_live_universe_maps_skyd_to_eodhd_identity_and_excludes_psky():
    mapping = {member.ticker: member.provider_ticker for member in load_canonical_universe()}
    assert len(mapping) == 518
    assert mapping["SKYD"] == "SKYD.US"
    assert "PSKY" not in mapping


def test_historical_v1_registry_remains_byte_identical_and_retains_psky():
    historical = Path(__file__).parents[2] / "trinity" / "usa_issuer_registry_v1.fixture"
    assert hashlib.sha256(historical.read_bytes()).hexdigest() == (
        "ccadc756df22a3e868e62b20aeef22dd3ad6a872dd5611f0a4846dc2c1a147c8"
    )
    registry = load_registry(historical)
    assert registry.require_supported("PSKY").provider_symbol == "PSKY.US"
    assert registry.get("SKYD") is None


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
