"""Generate the frozen USA issuer registry from canonical and SEC sources.

This is an explicit maintenance utility, never called by an analysis run.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any

import requests


SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
LEGAL_SUFFIX = re.compile(
    r"(?:[, ]+(?:INCORPORATED|INC|CORPORATION|CORP|COMPANY|CO|PLC|LTD|LIMITED|LLC)\.?"
    r"|[/ ]+(?:DE|NEW|NY|MA|MN|OH|TX|UK|CAN)/?)$", re.I,
)
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


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def aliases(name: str) -> list[str]:
    values = [name.strip()]
    stem = name.strip()
    while True:
        shortened = LEGAL_SUFFIX.sub("", stem).strip(" ,./")
        if shortened == stem:
            break
        stem = shortened
    if (len(stem) >= 4 and not stem.endswith("&")
            and stem.casefold() not in {value.casefold() for value in values}):
        values.append(stem)
    ampersand_stem = re.split(r"\s+(?:&|AND)(?:\s+|$)", stem, maxsplit=1, flags=re.I)[0].strip()
    if len(ampersand_stem) >= 5 and ampersand_stem.casefold() not in {
        value.casefold() for value in values
    }:
        values.append(ampersand_stem)
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canonical", type=Path, required=True)
    parser.add_argument("--constituents", type=Path, required=True)
    parser.add_argument("--sec-tickers", type=Path, required=True)
    parser.add_argument("--sec-meta", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--user-agent", default="TRINITY research giopa@example.com")
    args = parser.parse_args()
    canonical = list(csv.DictReader(args.canonical.open(encoding="utf-8-sig", newline="")))
    constituents = {
        row["ticker"]: row for row in csv.DictReader(
            args.constituents.open(encoding="utf-8-sig", newline="")
        )
    }
    sec_payload = json.loads(args.sec_tickers.read_text(encoding="utf-8"))
    sec_meta = json.loads(args.sec_meta.read_text(encoding="utf-8"))
    sec_rows = [dict(zip(sec_payload["fields"], row)) for row in sec_payload["data"]]
    by_sec_ticker = {str(row["ticker"]).upper(): row for row in sec_rows}
    if len(canonical) != 518 or len(constituents) != 518:
        raise ValueError("canonical inputs must each contain exactly 518 tickers")
    import truststore
    truststore.inject_into_ssl()
    session = requests.Session()
    session.headers.update({"User-Agent": args.user_agent, "Accept-Encoding": "gzip, deflate"})
    args.cache.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).isoformat()
    output: list[dict[str, Any]] = []
    for index, canonical_row in enumerate(canonical):
        ticker = canonical_row["canonical_ticker"].upper()
        provider_symbol = canonical_row["provider_ticker"].upper()
        sec_ticker = ticker.replace(".", "-")
        sec_row = by_sec_ticker.get(sec_ticker)
        unsupported: str | None = None
        if sec_row is None:
            unsupported = "NO_AUTHORITATIVE_SEC_TICKER_MATCH"
            # A complete placeholder is still required for registry validation.
            raise ValueError(f"{ticker}: no SEC ticker match")
        discovered_cik = str(sec_row["cik"]).zfill(10)
        cik = ORIGINAL.get(ticker, (None, None, None, discovered_cik))[3]
        cache = args.cache / f"{cik}.json"
        if cache.is_file():
            raw = cache.read_bytes()
        else:
            response = session.get(SEC_SUBMISSIONS.format(cik=cik), timeout=45)
            response.raise_for_status()
            raw = bytes(response.content)
            cache.write_bytes(raw)
            time.sleep(0.12)
        submissions = json.loads(raw)
        sic_text = str(submissions.get("sic") or "")
        if not sic_text.isdigit():
            unsupported = "SEC_SIC_UNAVAILABLE"
            sic = 0
            sic_description = "UNAVAILABLE"
        else:
            sic = int(sic_text)
            sic_description = str(submissions.get("sicDescription") or "UNAVAILABLE")
        if ticker in ORIGINAL:
            company_name, schema_type, issuer_aliases, _old_cik = ORIGINAL[ticker]
        else:
            company_name = str(sec_row["name"]).strip()
            schema_type = "BANK" if 6000 <= sic <= 6099 else "GENERAL"
            issuer_aliases = tuple(aliases(company_name))
        output.append({
            "ticker": ticker, "provider_symbol": provider_symbol,
            "company_name": company_name, "sec_cik": cik,
            "sec_ticker": sec_ticker, "sec_issuer_name": str(submissions["name"]),
            "sec_sic": sic, "sec_sic_description": sic_description,
            "schema_type": schema_type, "aliases": list(issuer_aliases),
            "supported": unsupported is None, "unsupported_reason": unsupported,
            "provenance": {
                "canonical_mapping_sha256": sha(args.canonical),
                "constituent_identity_sha256": sha(args.constituents),
                "sec_ticker_source_sha256": sha(args.sec_tickers),
                "sec_cik_source": (
                    "existing USA V2 verified SEC capture"
                    if ticker in ORIGINAL else "SEC company_tickers_exchange.json"
                ),
                "sec_submissions_sha256": hashlib.sha256(raw).hexdigest(),
                "sec_submissions_url": SEC_SUBMISSIONS.format(cik=cik),
            },
        })
        print(f"{index + 1}/518 {ticker}")
    value = {
        "schema_name": "trinity.usa-issuer-registry", "schema_version": "1",
        "canonical_ticker_count": 518, "generated_at": generated_at,
        "policy": {
            "bank_schema": "SEC SIC 6000-6099; original ten retain frozen schema labels",
            "class_share_mapping": "canonical dot maps to SEC hyphen",
            "alias_policy": "SEC name plus deterministic legal-suffix and ampersand stem; original ten unchanged",
        },
        "sources": {
            "canonical_mapping": {
                "description": "PRE_RESEARCH_FUNNEL_V1 canonical ticker_mapping.csv",
                "sha256": sha(args.canonical),
            },
            "constituent_identity": {
                "description": "frozen index_constituents_sp500_nasdaq100_dow30.csv",
                "sha256": sha(args.constituents),
            },
            "sec_ticker_source": sec_meta,
            "sec_submissions": "https://data.sec.gov/submissions/CIK##########.json",
        },
        "issuers": output,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
