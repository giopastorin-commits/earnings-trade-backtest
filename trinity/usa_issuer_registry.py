"""Validated issuer identity for the canonical TRINITY USA universe."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


DEFAULT_REGISTRY = Path(__file__).with_name("usa_issuer_registry_v1.fixture")
REGISTRY_SCHEMA = "trinity.usa-issuer-registry"
REGISTRY_VERSION = "1"


class IssuerRegistryError(ValueError):
    """The frozen registry is incomplete, ambiguous, or internally inconsistent."""


@dataclass(frozen=True)
class IssuerRecord:
    ticker: str
    provider_symbol: str
    company_name: str
    sec_cik: str
    sec_ticker: str
    sec_issuer_name: str
    sec_sic: int
    sec_sic_description: str
    schema_type: str
    aliases: tuple[str, ...]
    supported: bool
    unsupported_reason: str | None
    provenance: Mapping[str, str]


class IssuerRegistry:
    def __init__(self, records: Sequence[IssuerRecord], metadata: Mapping[str, Any]) -> None:
        self.records = tuple(records)
        self.metadata = dict(metadata)
        self._by_ticker = {record.ticker: record for record in records}

    def get(self, ticker: str) -> IssuerRecord | None:
        return self._by_ticker.get(str(ticker).strip().upper())

    def require_supported(self, ticker: str) -> IssuerRecord:
        normalized = str(ticker).strip().upper()
        record = self.get(normalized)
        if record is None:
            raise IssuerRegistryError(f"ticker is outside canonical USA universe: {normalized}")
        if not record.supported:
            raise IssuerRegistryError(
                f"unsupported canonical ticker {normalized}: {record.unsupported_reason}"
            )
        return record

    @property
    def supported(self) -> tuple[IssuerRecord, ...]:
        return tuple(record for record in self.records if record.supported)

    @property
    def unsupported(self) -> tuple[IssuerRecord, ...]:
        return tuple(record for record in self.records if not record.supported)


def _required_text(row: Mapping[str, Any], field: str, ticker: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise IssuerRegistryError(f"{ticker or '<unknown>'}: missing {field}")
    return value.strip()


def load_registry(path: str | Path = DEFAULT_REGISTRY) -> IssuerRegistry:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IssuerRegistryError(f"invalid issuer registry: {source}") from exc
    if not isinstance(payload, dict) or payload.get("schema_name") != REGISTRY_SCHEMA \
            or payload.get("schema_version") != REGISTRY_VERSION:
        raise IssuerRegistryError("unsupported issuer registry schema")
    rows = payload.get("issuers")
    if not isinstance(rows, list) or not rows:
        raise IssuerRegistryError("issuer registry has no records")
    records: list[IssuerRecord] = []
    seen_tickers: set[str] = set()
    seen_security_identity: set[tuple[str, str]] = set()
    provider_symbols: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise IssuerRegistryError("issuer registry contains a non-object record")
        ticker = _required_text(row, "ticker", "").upper()
        if ticker in seen_tickers:
            raise IssuerRegistryError(f"duplicate canonical ticker: {ticker}")
        seen_tickers.add(ticker)
        provider_symbol = _required_text(row, "provider_symbol", ticker).upper()
        if provider_symbol in provider_symbols:
            raise IssuerRegistryError(f"duplicate provider symbol: {provider_symbol}")
        provider_symbols.add(provider_symbol)
        cik = _required_text(row, "sec_cik", ticker)
        if len(cik) != 10 or not cik.isdigit():
            raise IssuerRegistryError(f"{ticker}: SEC CIK must be ten digits")
        sec_ticker = _required_text(row, "sec_ticker", ticker).upper()
        security_identity = (cik, sec_ticker)
        if security_identity in seen_security_identity:
            raise IssuerRegistryError(f"duplicate SEC CIK/ticker identity: {cik}/{sec_ticker}")
        seen_security_identity.add(security_identity)
        aliases = row.get("aliases")
        if not isinstance(aliases, list) or not aliases or any(
            not isinstance(alias, str) or not alias.strip() for alias in aliases
        ):
            raise IssuerRegistryError(f"{ticker}: aliases must be non-empty strings")
        if len({alias.casefold() for alias in aliases}) != len(aliases):
            raise IssuerRegistryError(f"{ticker}: duplicate aliases")
        supported = row.get("supported")
        if type(supported) is not bool:
            raise IssuerRegistryError(f"{ticker}: supported must be boolean")
        unsupported_reason = row.get("unsupported_reason")
        if supported and unsupported_reason is not None:
            raise IssuerRegistryError(f"{ticker}: supported record has an unsupported reason")
        if not supported and (not isinstance(unsupported_reason, str) or not unsupported_reason):
            raise IssuerRegistryError(f"{ticker}: unsupported record lacks a reason")
        provenance = row.get("provenance")
        if not isinstance(provenance, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) or not value
            for key, value in provenance.items()
        ):
            raise IssuerRegistryError(f"{ticker}: invalid provenance")
        try:
            sec_sic = int(row["sec_sic"])
        except (KeyError, TypeError, ValueError) as exc:
            raise IssuerRegistryError(f"{ticker}: missing or invalid SEC SIC") from exc
        records.append(IssuerRecord(
            ticker=ticker, provider_symbol=provider_symbol,
            company_name=_required_text(row, "company_name", ticker), sec_cik=cik,
            sec_ticker=sec_ticker,
            sec_issuer_name=_required_text(row, "sec_issuer_name", ticker),
            sec_sic=sec_sic,
            sec_sic_description=_required_text(row, "sec_sic_description", ticker),
            schema_type=_required_text(row, "schema_type", ticker).upper(),
            aliases=tuple(alias.strip() for alias in aliases), supported=supported,
            unsupported_reason=unsupported_reason, provenance=dict(provenance),
        ))
    expected = payload.get("canonical_ticker_count")
    if not isinstance(expected, int) or expected != len(records):
        raise IssuerRegistryError(
            f"canonical ticker count mismatch: expected {expected}, found {len(records)}"
        )
    return IssuerRegistry(records, {key: value for key, value in payload.items() if key != "issuers"})


REGISTRY = load_registry()


def get_issuer(ticker: str) -> IssuerRecord:
    return REGISTRY.require_supported(ticker)
