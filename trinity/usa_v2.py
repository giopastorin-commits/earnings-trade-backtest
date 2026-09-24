"""Read-only USA thesis slice over the existing frozen EODHD caches.

The cache is a historical observation, not a live feed. News is only admitted
when the issuer is explicit in the title and the cached text is a wire release.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import html
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping

from trinity.italia_real import CodexCLIProvider, _ANALYST_SCHEMA
from trinity.italia_v1 import analyze_company, build_facts, save_thesis
from trinity.usa_documents import load_document_coverage


AS_OF = "2026-09-12"
DATASET = Path("C:/Users/giopa/trinity-scanner-v1/historical/raw")
PRICES = DATASET / "eodhd_prices_518_daily_20220101_20260913_v1/provider_raw"
NEWS = DATASET / "eodhd_news/eodhd_news_historical_20250101_20260912_v2/records"
COMPANIES = {
    "AAPL": ("Apple", "TECHNOLOGY", ("Apple",)),
    "JPM": ("JPMorgan Chase", "BANK", ("JPMorganChase", "JPMorgan Chase")),
    "JNJ": ("Johnson & Johnson", "HEALTHCARE", ("Johnson & Johnson",)),
    "XOM": ("Exxon Mobil", "ENERGY", ("ExxonMobil",)),
    "WMT": ("Walmart", "CONSUMER_STAPLES", ("Walmart",)),
    "CAT": ("Caterpillar", "INDUSTRIAL", ("Caterpillar",)),
    "NEE": ("NextEra Energy", "UTILITY", ("NextEra Energy",)),
    "AMZN": ("Amazon", "CONSUMER_DISCRETIONARY", ("Amazon.com",)),
    "PLD": ("Prologis", "REAL_ESTATE", ("Prologis",)),
    "LIN": ("Linde", "MATERIALS", ("Linde",)),
}
WIRE = re.compile(r"(?:BUSINESS WIRE|PRNewswire|PR NEWSWIRE)", re.I)
RESULT = re.compile(r"(?:first|second|third|fourth)[- ]quarter.{0,35}results|quarterly.{0,20}results|revenue growth", re.I)
SCHEDULED = re.compile(r"to host|conference call|available on|to release|to announce|date for release|to report", re.I)
GUIDANCE = re.compile(r"\b(?:raises?|raised|reiterates?|reaffirm(?:s|ed)?|lowers?|lowered|updates?|provides?|issues?)\b.{0,75}\b(?:guidance|outlook)\b|\b(?:guidance|outlook)\b.{0,60}\b(?:raises?|raised|reiterates?|reaffirm(?:s|ed)?|lowers?|lowered|updates?|provides?|issues?)\b", re.I)
_METRIC_LABEL = r"adjusted operating profit|operating profit|operating income|sales and revenues|net sales|net income|net earnings|cash flow|revenue|earnings|sales"
METRIC = re.compile(
    rf"(?P<label>{_METRIC_LABEL})\b(?:(?!\b(?:{_METRIC_LABEL})\b)[^.!?\n]){{0,65}}?"
    r"(?P<amount>\$[\d,.]+\s*(?:billion|million|trillion))", re.I,
)
GUIDANCE_RANGE = re.compile(
    r"\$([\d,]+(?:\.\d+)?)\s*(?:-|to|–|—)\s*\$?([\d,]+(?:\.\d+)?)", re.I
)
GUIDANCE_TABLE_RANGE = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s+\$\s*([\d,]+(?:\.\d+)?)", re.I
)
REVISION_FIELDS = ("fundamental_analysis", "earnings_and_news_analysis", "price_context",
                   "bull_case", "bear_case", "catalysts", "risks", "thesis_invalidation")
CLAIM_FIELDS = frozenset(REVISION_FIELDS)
_CLAIM_REF_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["claim_id", "field", "text", "fact_ids", "materiality", "period"],
    "properties": {
        "claim_id": {"type": "string"},
        "field": {"type": "string", "enum": list(REVISION_FIELDS)},
        "text": {"type": "string"},
        "fact_ids": {"type": "array", "items": {"type": "string"}},
        "materiality": {"type": "string", "enum": ["MATERIAL", "SUPPORTING"]},
        "period": {"type": ["string", "null"]},
    },
}
_USA_ANALYST_SCHEMA = json.loads(json.dumps(_ANALYST_SCHEMA))
_USA_ANALYST_SCHEMA["required"].append("claim_refs")
_USA_ANALYST_SCHEMA["properties"]["claim_refs"] = {
    "type": "array", "items": _CLAIM_REF_SCHEMA,
}
_REVISED_SCHEMA = {"type": "object", "additionalProperties": False,
                   "required": [*REVISION_FIELDS, "claim_refs"],
                   "properties": {
                       **{key: _ANALYST_SCHEMA["properties"][key] for key in REVISION_FIELDS},
                       "claim_refs": {"type": "array", "items": _CLAIM_REF_SCHEMA},
                   }}
_USA_CRITIC_SCHEMA = {"type": "object", "additionalProperties": False,
                      "required": ["notes", "status", "confidence", "revised_analysis"],
                      "properties": {
                          "notes": {"type": "array", "items": {"type": "string"}},
                          "status": {"type": "string", "enum": ["PASS", "WATCH", "INVESTIGATE"]},
                          "confidence": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
                          "revised_analysis": _REVISED_SCHEMA,
                      }}


@dataclass(frozen=True)
class EvidencePack:
    ticker: str
    as_of: str
    company_input: dict[str, object]
    triage: dict[str, object]


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _slug(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).casefold()).strip("_") or "unknown"


def _period_key(period: str) -> str:
    match = re.fullmatch(r"(\d{4})-(\d{2})-\d{2}/(\d{4})-(\d{2})-\d{2}", period)
    if match and match.group(1) == match.group(3):
        quarter = (int(match.group(4)) - 1) // 3 + 1
        return f"{match.group(3)}Q{quarter}"
    return period.replace("/", "_")


def _fact_identity(
    ticker: str, fact: dict[str, object], *, fallback_period: str, discriminator: str = "",
) -> dict[str, object]:
    """Attach stable identity/provenance metadata without copying the fact value."""
    metric = str(fact.get("metric") or fact.get("metric_label") or "unknown")
    period = str(fact.get("period") or fallback_period)
    evidence_id = str(fact.get("evidence_id") or fact.get("evidence_identifier") or "")
    base = f"fact:{ticker}:{_period_key(period)}:{_slug(metric)}"
    if discriminator:
        digest = hashlib.sha256(
            f"{evidence_id}|{metric}|{fact.get('value')}|{fact.get('unit')}|{discriminator}".encode()
        ).hexdigest()[:10]
        base = f"{base}:{digest}"
    fact["fact_id"] = base
    fact["ticker"] = ticker
    fact["metric"] = metric
    fact["period"] = period
    fact["evidence_id"] = evidence_id
    unit = str(fact.get("unit") or "")
    fact["scale"] = {
        "USD": 1, "USD_THOUSANDS": 1_000, "USD_MILLIONS": 1_000_000,
        "USD_BILLIONS": 1_000_000_000, "USD_TRILLIONS": 1_000_000_000_000,
        "PERCENT": 1, "USD_PER_SHARE": 1, "USD/shares": 1,
    }.get(unit, 1)
    value = fact.get("value")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        decimal_value = Decimal(str(value))
        fact["display_precision"] = max(0, -decimal_value.as_tuple().exponent)
    else:
        fact["display_precision"] = None
    return fact


def _attach_fact_ids(
    ticker: str, price: dict[str, object], price_evidence_id: str,
    events: list[dict[str, object]],
) -> None:
    price["acquisition"]["fact_metadata"] = [
        {
            "fact_id": f"fact:{ticker}:{price['published_at']}:{metric}",
            "ticker": ticker, "metric": metric, "value_path": f"price.{metric}",
            "unit": "USD" if metric == "current_price" else "PERCENT",
            "period": price["published_at"], "evidence_id": price_evidence_id,
            "extraction_method": "deterministic_cached_ohlcv",
            "scale": 1, "display_precision": 4 if metric != "current_price" else 2,
        }
        for metric in ("current_price", "return_5d", "return_20d", "return_60d")
    ]
    for event in events:
        published = str(event["published_at"])
        groups = (
            ("reported_metrics", "reported"),
            ("guidance_ranges", "guidance_range"),
            ("guidance_changes", "guidance_change"),
            ("structured_sec_facts", ""),
        )
        for key, discriminator in groups:
            for fact in event["facts"].get(key, []):
                if key == "guidance_ranges":
                    fact.setdefault("value", fact.get("current_range"))
                elif key == "guidance_changes":
                    fact.setdefault("value", fact.get("direction"))
                    fact.setdefault("evidence_identifier", fact.get("current_evidence_identifier"))
                    fact.setdefault("unit", "DIRECTION")
                _fact_identity(
                    ticker, fact, fallback_period=published,
                    discriminator=f"{discriminator}:{published}" if discriminator else "",
                )


def _read_prices(ticker: str, as_of: str) -> tuple[dict[str, object], dict[str, object]]:
    path = PRICES / f"{ticker}.json"
    raw = path.read_bytes()
    bars = json.loads(raw)
    bars = [bar for bar in bars if bar["date"] <= as_of]
    if len(bars) < 61 or any(bars[-1][field] is None for field in ("open", "high", "low", "close", "volume")):
        raise ValueError(f"{ticker}: insufficient OHLCV")
    latest = bars[-1]
    if latest["close"] <= 0 or latest["volume"] < 0:
        raise ValueError(f"{ticker}: invalid latest OHLCV")
    returns = {f"return_{period}d": round(100 * (latest["close"] / bars[-period - 1]["close"] - 1), 4)
               for period in (5, 20, 60)}
    reference_bars = {str(period): {"date": bars[-period - 1]["date"],
                                    "close": bars[-period - 1]["close"]}
                      for period in (5, 20, 60)}
    evidence = {
        "source": "EODHD frozen daily USA cache", "published_at": latest["date"],
        "identifier": f"price:{ticker}:{latest['date']}", "url": None,
        "excerpt": json.dumps({"latest": {key: latest[key] for key in
                                            ("date", "open", "high", "low", "close", "volume")},
                               "reference_bars": reference_bars, "returns_percent": returns}),
        "title": f"{ticker} daily OHLCV", "retrieved_at": "2026-09-13",
        "content_sha256": _digest(raw), "raw_path": str(path),
        "published_at_precision": "day", "document_kind": "PRICE",
    }
    price = {"current_price": latest["close"], **returns, "published_at": latest["date"],
             "acquisition": {"currency": "USD", "market": "USA", "provider_symbol": f"{ticker}.US",
                             "last_bar": latest, "reference_bars": reference_bars,
                             "raw_path": str(path), "sha256": evidence["content_sha256"]}}
    return price, evidence


def _issuer_release(record: Mapping[str, object], ticker: str) -> bool:
    title = html.unescape(str(record.get("title") or ""))
    content = html.unescape(str(record.get("content") or ""))
    aliases = COMPANIES[ticker][2]
    return (f"{ticker}.US" in record.get("symbols", [])
            and any(title.casefold().startswith(alias.casefold()) for alias in aliases)
            and bool(WIRE.search(content[:650]))
            and any(alias.casefold() in content[:2000].casefold() for alias in aliases))


def _news_records(ticker: str, as_of: str) -> tuple[list[dict[str, object]], int]:
    accepted: list[dict[str, object]] = []
    rejected = 0
    for path in sorted((NEWS / ticker).glob("2026-*.jsonl")):
        if path.stem < "2026-04" or path.stem > as_of[:7]:
            continue
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                record = json.loads(line)
                published = str(record.get("published_ts") or "")[:10]
                if not published or published > as_of:
                    continue
                if not _issuer_release(record, ticker):
                    rejected += 1
                    continue
                record["_path"] = str(path)
                record["_line"] = line_number
                record["_line_sha256"] = _digest(line.encode("utf-8"))
                accepted.append(record)
    accepted.sort(key=lambda r: str(r["published_ts"]), reverse=True)
    return accepted, rejected


def _record_evidence(record: Mapping[str, object], ticker: str) -> dict[str, object]:
    title = html.unescape(str(record["title"]))
    content = html.unescape(str(record["content"]))
    identifier = f"news:{ticker}:{str(record['canonical_record_sha256'])[:16]}"
    return {
        "source": "EODHD cached issuer wire release", "published_at": str(record["published_ts"])[:10],
        "identifier": identifier, "url": record.get("link"), "excerpt": content[:4500],
        "title": title, "retrieved_at": str(record.get("ingestion_ts") or "2026-09-13")[:10],
        "content_sha256": str(record["_line_sha256"]), "raw_path": f"{record['_path']}:{record['_line']}",
        "published_at_precision": "timestamp", "document_kind": "ISSUER_RELEASE",
    }


def _reported_period(text: str) -> str:
    months = {name: index for index, name in enumerate(
        ("january", "february", "march", "april", "may", "june", "july", "august",
         "september", "october", "november", "december"), 1)}
    ended = re.search(
        r"quarter ended\s+(" + "|".join(months) + r")\s+(\d{1,2}),\s*(20\d{2})",
        text[:1200], re.I,
    )
    if ended:
        return f"{ended.group(3)}-{months[ended.group(1).lower()]:02d}-{int(ended.group(2)):02d}"
    quarter = re.search(
        r"\b(first|second|third|fourth)[- ]quarter\s+(20\d{2})\b|"
        r"\b(20\d{2})\s+(first|second|third|fourth)[- ]quarter\b",
        text[:1200], re.I,
    )
    if quarter:
        name = (quarter.group(1) or quarter.group(4)).lower()
        year = quarter.group(2) or quarter.group(3)
        return f"{year}Q{('first', 'second', 'third', 'fourth').index(name) + 1}"
    return "UNSPECIFIED"


def _literal_metrics(
    text: str, evidence_id: str, period: str | None = None,
) -> list[dict[str, object]]:
    claims: list[dict[str, object]] = []
    # The opening results section carries current-period highlights. Later
    # boilerplate often repeats prior-year company-wide figures.
    for match in METRIC.finditer(text[:1700]):
        bridge = text[match.end("label"):match.start("amount")]
        if re.match(r"\s+volume\b", bridge, re.I):
            continue
        amount = match.group("amount")
        unit = "USD_" + re.search(r"billion|million|trillion", amount, re.I).group().upper() + "S"
        value = float(re.search(r"[\d,.]+", amount).group().replace(",", ""))
        excerpt = text[max(0, match.start() - 35):min(len(text), match.end() + 45)].strip()
        suffix = text[match.end():match.end() + 45]
        measurement = ("ANNUALIZED_RUN_RATE" if re.search(r"annualized|run rate", suffix, re.I)
                       else "CHANGE_AMOUNT" if re.search(r"\b(?:up|down|increased|decreased|higher|lower|by)\b", bridge, re.I)
                       else "REPORTED_AMOUNT")
        claim = {"metric_label": match.group("label").lower(), "value": value, "unit": unit,
                 "period": period or _reported_period(text),
                 "measurement_type": measurement,
                 "source_excerpt": excerpt, "evidence_identifier": evidence_id,
                 "extraction_method": "literal_regex_explicit_currency_and_scale"}
        if not any((old["metric_label"], old["value"], old["unit"]) ==
                   (claim["metric_label"], claim["value"], claim["unit"]) for old in claims):
            claims.append(claim)
        if len(claims) == 5:
            break
    return claims


def _guidance(text: str, evidence_id: str) -> dict[str, str] | None:
    match = GUIDANCE.search(text[:4500])
    if not match:
        return None
    excerpt = text[max(0, match.start() - 50):min(len(text), match.end() + 70)].strip()
    return {"source_excerpt": excerpt, "evidence_identifier": evidence_id,
            "extraction_method": "literal_guidance_language"}


def _guidance_ranges(text: str, evidence_id: str) -> list[dict[str, object]]:
    """Capture explicit per-share guidance ranges; no inferred scale or delta."""
    ranges: list[dict[str, object]] = []
    for match in re.finditer(r"\b(adjusted EPS|Core FFO)\b", text[:6500], re.I):
        context = text[max(0, match.start() - 400):match.end() + 80]
        if not re.search(r"guidance", context, re.I):
            continue
        tail = text[match.end():min(len(text), match.end() + 150)]
        found = list(GUIDANCE_RANGE.finditer(tail))[:2]
        if not found:
            found = list(GUIDANCE_TABLE_RANGE.finditer(tail))[:2]
        if not found or found[0].start() > 100:
            continue
        if re.search(r"\bby\s*$", tail[:found[0].start()], re.I):
            continue
        # Rows can contain both Previous and Current; a highlight has Current only.
        def pair(item: re.Match[str]) -> list[float]:
            return [float(item.group(1).replace(",", "")),
                    float(item.group(2).replace(",", ""))]
        if any(low > high for low, high in (pair(item) for item in found)):
            continue
        year = re.search(r"20\d{2}", context)
        if year is None:
            continue
        previous = pair(found[0]) if len(found) == 2 else None
        current = pair(found[-1])
        excerpt = text[max(0, match.start() - 45):match.end() + found[-1].end()].strip()
        metric = "ADJUSTED_EPS" if match.group(1).lower().startswith("adjusted") else "CORE_FFO"
        claim = {"metric": metric, "period": year.group(), "unit": "USD_PER_SHARE",
                 "previous_range": previous, "current_range": current,
                 "source_excerpt": excerpt, "evidence_identifier": evidence_id,
                 "extraction_method": "literal_guidance_range_explicit_usd_per_share"}
        if not any(old["metric"] == metric and old["period"] == claim["period"] for old in ranges):
            ranges.append(claim)
    return ranges


def _numeric_audit(facts: Mapping[str, object], draft: Mapping[str, object]) -> list[str]:
    """Reject draft numbers absent from the supplied source text or price block."""
    evidence_by_id = {str(item["identifier"]): str(item.get("excerpt") or "")
                      for item in facts["evidence"]}
    price = facts["price"]
    price_text = " ".join(str(price.get(key)) for key in
                          ("current_price", "return_5d", "return_20d", "return_60d"))
    number = re.compile(r"(?<![\w])\d[\d,.]*", re.I)
    quarter = re.compile(r"\b[1-4]\s*[°º]?\s*(?:trimestre|quarter)\b", re.I)
    spelled = {word: str(index) for index, word in enumerate(
        ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"))}
    def source_numbers(source: str) -> set[str]:
        values = {canonical(token) for token in number.findall(source)}
        values.update(digit for word, digit in spelled.items()
                      if re.search(rf"\b{word}\b", source, re.I))
        return values
    def canonical(token: str) -> str:
        raw = re.sub(r"(?:st|nd|rd|th|%)$", "", token, flags=re.I)
        if "." in raw and "," in raw:
            decimal_separator = "." if raw.rfind(".") > raw.rfind(",") else ","
            raw = raw.replace("," if decimal_separator == "." else ".", "")
            raw = raw.replace(decimal_separator, ".")
        elif raw.count(".") > 1 or raw.count(",") > 1:
            raw = raw.replace(".", "").replace(",", "")
        else:
            raw = raw.replace(",", ".")
        return str(Decimal(raw).normalize())
    citation = re.compile(r"\[([^\]]+)\]")
    failures: list[str] = []
    for field in ("fundamental_analysis", "earnings_and_news_analysis", "price_context",
                  "bull_case", "bear_case", "thesis_invalidation"):
        statement = str(draft.get(field) or "")
        ids = citation.findall(statement)
        supplied = " ".join(evidence_by_id.get(item, "") for item in ids)
        if field == "price_context":
            supplied += " " + price_text
        allowed = source_numbers(supplied)
        if field == "price_context":
            allowed.update({"5", "20", "60"})
        if field == "thesis_invalidation":
            # Zero is a prospective break-even threshold, not an observed fact.
            allowed.add("0")
        without_citations = quarter.sub("", citation.sub("", statement))
        unsupported = sorted({canonical(token) for token in number.findall(without_citations)} - allowed)
        if unsupported:
            failures.append(f"{field}: numeric tokens without cited source {unsupported}")
    for field in ("catalysts", "risks"):
        for index, statement in enumerate(draft.get(field) or []):
            ids = citation.findall(statement)
            supplied = " ".join(evidence_by_id.get(item, "") for item in ids)
            allowed = source_numbers(supplied)
            unsupported = sorted({canonical(token) for token in number.findall(quarter.sub("", citation.sub("", statement)))} - allowed)
            if unsupported:
                failures.append(f"{field}[{index}]: numeric tokens without cited source {unsupported}")
    return failures


@dataclass(frozen=True)
class ClaimValidation:
    revised_analysis: dict[str, object]
    valid_claim_refs: list[dict[str, object]]
    rejected_claim_refs: list[dict[str, object]]
    essential_support_removed: bool


_QUANTIFIED_NUMBER = re.compile(
    r"(?<![\w])[-+âˆ’â€“â€”]?(?:\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d+)?(?![\w])"
)
_CITATION = re.compile(r"\[[^\]]+\]")


def _fact_catalog(facts: Mapping[str, object]) -> dict[str, dict[str, object]]:
    catalog: dict[str, dict[str, object]] = {}
    price = facts["price"]
    for metadata in price.get("acquisition", {}).get("fact_metadata", []):
        item = dict(metadata)
        item["value"] = price[item["metric"]]
        catalog[str(item["fact_id"])] = item
    for event in facts["events"]:
        for value in event["facts"].values():
            if not isinstance(value, list):
                continue
            for item in value:
                if isinstance(item, Mapping) and item.get("fact_id"):
                    catalog[str(item["fact_id"])] = dict(item)
    return catalog


def _decimal_token(raw: str, *, scaled: bool) -> Decimal:
    token = raw.strip().replace("+", "")
    sign = "-" if token[:1] in {"-", "âˆ’", "â€“", "â€”"} else ""
    token = token.lstrip("-âˆ’â€“â€”")
    if "." in token and "," in token:
        decimal_separator = "." if token.rfind(".") > token.rfind(",") else ","
        token = token.replace("," if decimal_separator == "." else ".", "")
        token = token.replace(decimal_separator, ".")
    elif token.count(".") + token.count(",") > 1:
        token = token.replace(".", "").replace(",", "")
    elif "." in token or "," in token:
        separator = "." if "." in token else ","
        left, right = token.split(separator)
        if scaled or len(right) != 3:
            token = f"{left}.{right}"
        else:
            token = left + right
    return Decimal(sign + token)


def _numeric_mentions(text: str) -> list[dict[str, object]]:
    clean = _CITATION.sub("", text)
    clean = re.sub(r"\b20\d{2}-\d{2}-\d{2}\b", "", clean)
    clean = re.sub(
        r"\b\d+\s*,\s*\d+\s+(?:e|and)\s+\d+\s+(?:giorni|sedute|days?)\b",
        "", clean, flags=re.I,
    )
    negative_context = bool(re.search(
        r"\b(?:ha perso|perdita|negativ[oa]|calo|ribasso|diminuzione|decline|declined|negative|loss|lost)\b",
        clean, re.I,
    ))
    mentions: list[dict[str, object]] = []
    for match in _QUANTIFIED_NUMBER.finditer(clean):
        raw = match.group()
        before = clean[max(0, match.start() - 28):match.start()].casefold()
        after = clean[match.end():match.end() + 32].casefold()
        context = before + raw.casefold() + after
        if re.search(r"\b(?:exhibit|allegato)\s*$", before, re.I):
            continue
        unsigned = raw.lstrip("+-\u2212\u2013\u2014")
        if re.search(rf"\b{re.escape(unsigned)}\s*[- ]?[qk]\b", context, re.I):
            continue
        if re.search(rf"\b(?:q|quarter|trimestre)\s*{re.escape(unsigned)}\b", context, re.I):
            continue
        if re.match(r"\s*(?:Â°|Âº|°|º|st|nd|rd|th)?\s*(?:trimestre|quarter)\b", after, re.I):
            continue
        if re.match(
            r"\s+(?:gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|"
            r"ottobre|novembre|dicembre|january|february|march|april|may|june|july|august|"
            r"september|october|november|december)\b", after, re.I,
        ):
            continue
        if re.match(r"\s*(?:giorni|sedute|days?)\b", after, re.I):
            continue
        if unsigned.isdigit() and 1900 <= int(unsigned) <= 2100:
            continue
        scale = 1
        if re.search(r"\b(?:trillion|trilioni?)\b", context):
            scale = 1_000_000_000_000
        elif re.search(r"\b(?:billion|miliard[oi])\b", context):
            scale = 1_000_000_000
        elif re.search(r"\b(?:million|milion[ei])\b", context):
            scale = 1_000_000
        elif re.search(r"\b(?:thousand|migliaia)\b", context):
            scale = 1_000
        unit = None
        if "%" in after[:4] or re.search(r"\bpercent", context):
            unit = "PERCENT"
        elif re.search(r"\b(?:per share|per azione)\b", context):
            unit = "USD_PER_SHARE"
        elif scale != 1 or re.search(r"(?:\$|\busd\b|\bdollar)", context):
            unit = "USD"
        try:
            value = _decimal_token(raw, scaled=scale != 1 or unit in {"PERCENT", "USD_PER_SHARE"})
        except InvalidOperation:
            continue
        precision = max(0, -value.as_tuple().exponent)
        mentions.append({"raw": raw, "value": value, "scale": scale,
                         "unit": unit, "precision": precision,
                         "negative_context": negative_context})
    return mentions


def _fact_values(fact: Mapping[str, object]) -> list[Decimal]:
    value = fact.get("value")
    values = value if isinstance(value, list) else [value]
    result: list[Decimal] = []
    for item in values:
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            result.append(Decimal(str(item)))
    return result


def _presentation_matches(mention: Mapping[str, object], fact: Mapping[str, object]) -> bool:
    unit = str(fact.get("unit") or "")
    claim_unit = mention["unit"]
    if claim_unit == "PERCENT" and unit != "PERCENT":
        return False
    if claim_unit == "USD_PER_SHARE" and unit not in {"USD_PER_SHARE", "USD/shares"}:
        return False
    if claim_unit == "USD" and not unit.startswith("USD"):
        return False
    scale = Decimal(str(fact.get("scale") or 1))
    display_scale = Decimal(str(mention["scale"]))
    precision = int(mention["precision"])
    quantum = Decimal(1).scaleb(-precision)
    for fact_value in _fact_values(fact):
        base_value = fact_value * scale
        displayed = base_value / display_scale
        if displayed.quantize(quantum, rounding=ROUND_HALF_UP) == mention["value"]:
            return True
        if (mention.get("negative_context") and displayed < 0 and mention["value"] >= 0 and
                abs(displayed).quantize(quantum, rounding=ROUND_HALF_UP) == mention["value"]):
            return True
    return False


def _field_texts(analysis: Mapping[str, object], field: str) -> list[str]:
    value = analysis.get(field, [] if field in {"catalysts", "risks"} else "")
    return [str(item) for item in value] if isinstance(value, list) else [str(value)]


def _period_matches(declared: object, fact_period: object) -> bool:
    claim = str(declared).strip()
    period = str(fact_period).strip()
    if claim == period or claim == _period_key(period):
        return True
    if "/" in claim and period == claim.rsplit("/", 1)[-1]:
        return True
    quarter_names = {"first": 1, "second": 2, "third": 3, "fourth": 4,
                     "primo": 1, "secondo": 2, "terzo": 3, "quarto": 4}
    match = re.search(
        r"\b(first|second|third|fourth|primo|secondo|terzo|quarto)[- ](?:quarter|trimestre)\s+(20\d{2})\b",
        claim, re.I,
    )
    if match and period == f"{match.group(2)}Q{quarter_names[match.group(1).lower()]}":
        return True
    return False


def _remove_claim_text(analysis: dict[str, object], field: str, text: str) -> None:
    if field in {"catalysts", "risks"}:
        updated = []
        for item in analysis.get(field, []):
            cleaned = str(item).replace(text, "").strip(" ;,.-")
            if cleaned:
                updated.append(cleaned)
        analysis[field] = updated
        return
    cleaned = str(analysis.get(field, "")).replace(text, "").strip()
    analysis[field] = cleaned or "Claim quantitativo rimosso per provenance insufficiente."


def validate_claim_refs(
    facts: Mapping[str, object], analysis: Mapping[str, object],
) -> ClaimValidation:
    """Validate CLAIM -> FACT_ID -> EVIDENCE_ID and remediate only invalid claims."""
    revised = {field: json.loads(json.dumps(analysis[field])) for field in REVISION_FIELDS}
    catalog = _fact_catalog(facts)
    ticker = str(facts["identity"]["ticker"])
    evidence_ids = {str(item["identifier"]) for item in facts["evidence"]}
    supplied_refs = [dict(item) for item in analysis.get("claim_refs", [])]
    valid: list[dict[str, object]] = []
    rejected: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for claim in supplied_refs:
        errors: list[str] = []
        claim_id = str(claim.get("claim_id") or "")
        field = str(claim.get("field") or "")
        text = str(claim.get("text") or "")
        fact_ids = [str(item) for item in claim.get("fact_ids", [])]
        materiality = str(claim.get("materiality") or "MATERIAL")
        if not claim_id or claim_id in seen_ids:
            errors.append("INVALID_CLAIM_ID")
        seen_ids.add(claim_id)
        if field not in CLAIM_FIELDS or not text or not any(text in item for item in _field_texts(analysis, field)):
            errors.append("CLAIM_TEXT_NOT_FOUND")
        linked: list[dict[str, object]] = []
        for fact_id in fact_ids:
            fact = catalog.get(fact_id)
            if fact is None:
                errors.append("UNKNOWN_FACT_ID")
                continue
            linked.append(fact)
            if str(fact.get("ticker")) != ticker or not fact_id.startswith(f"fact:{ticker}:"):
                errors.append("FOREIGN_COMPANY_FACT")
            if str(fact.get("evidence_id") or "") not in evidence_ids:
                errors.append("UNKNOWN_EVIDENCE_ID")
        declared_period = claim.get("period")
        if declared_period is not None and linked and not any(
            _period_matches(declared_period, fact.get("period"))
            for fact in linked
        ):
            errors.append("PERIOD_MISMATCH")
        mentions = _numeric_mentions(text)
        if mentions and not fact_ids:
            errors.append("MISSING_FACT_ID")
        for mention in mentions:
            if not any(_presentation_matches(mention, fact) for fact in linked):
                errors.append("UNREGISTERED_DERIVATION_OR_VALUE")
        record = {**claim, "validation_errors": sorted(set(errors))}
        if errors:
            rejected.append(record)
        else:
            valid.append({**claim, "validation_errors": []})

    referenced_text = [str(item.get("text") or "") for item in supplied_refs]
    for field in REVISION_FIELDS:
        for index, item in enumerate(_field_texts(analysis, field)):
            candidates = [part.strip() for part in re.split(r"(?<=[.!?])\s+", item) if part.strip()]
            for candidate in candidates:
                if not _numeric_mentions(candidate):
                    continue
                if any(candidate in text or text in candidate for text in referenced_text if text):
                    continue
                rejected.append({
                    "claim_id": f"claim:{ticker}:{field}:unbound:{index}", "field": field,
                    "text": candidate, "fact_ids": [], "materiality": "MATERIAL",
                    "period": None, "validation_errors": ["UNBOUND_NUMERIC_CLAIM"],
                })

    for claim in rejected:
        field = str(claim.get("field") or "")
        if field in CLAIM_FIELDS:
            _remove_claim_text(revised, field, str(claim.get("text") or ""))
    valid_material = [item for item in valid if item.get("materiality") == "MATERIAL"]
    material_rejected = any(item.get("materiality") == "MATERIAL" for item in rejected)
    remaining_fact_ids = {fact_id for item in valid_material for fact_id in item.get("fact_ids", [])}
    essential_removed = material_rejected and (
        len(valid_material) < 2 or len(remaining_fact_ids) < 2
    )
    revised["claim_refs"] = valid
    return ClaimValidation(revised, valid, rejected, essential_removed)


def _clean_citations(value: str, known: set[str]) -> str:
    """Keep only actual evidence IDs, splitting combined model citations."""
    def replacement(match: re.Match[str]) -> str:
        parts = [part.strip() for part in match.group(1).split(";")]
        return " ".join(f"[{part}]" for part in parts if part in known)
    cleaned = re.sub(r"\[([^\]]+)\]", replacement, value)
    return re.sub(r"\s+([,.!?])", r"\1", re.sub(r" {2,}", " ", cleaned)).strip()


def load_company(ticker: str, as_of: str = AS_OF) -> EvidencePack:
    """Assemble cached data and source-linked literal facts without network calls."""
    if ticker not in COMPANIES:
        raise ValueError(f"unsupported ticker: {ticker}")
    date.fromisoformat(as_of)
    if as_of > AS_OF:
        raise ValueError("as_of exceeds frozen news coverage")
    company, sector, _ = COMPANIES[ticker]
    price, price_evidence = _read_prices(ticker, as_of)
    records, rejected = _news_records(ticker, as_of)
    results = [r for r in records if RESULT.search(html.unescape(str(r["title"])))
               and not SCHEDULED.search(html.unescape(str(r["title"]))) ]
    announcement = [r for r in records if RESULT.search(html.unescape(str(r["title"])))
                    and r not in results]
    chosen = results[:2] or announcement[:1]
    latest_other = next((r for r in records if r not in chosen and
                         not RESULT.search(html.unescape(str(r["title"]))) and
                         re.search(r"guidance|acqui|approv|trial|dividend|buyback|contract|invest", str(r["title"]), re.I)), None)
    if latest_other:
        chosen.append(latest_other)
    # Retain publication order for novelty comparison, regardless of selection order.
    chosen.sort(key=lambda r: str(r["published_ts"]))
    evidence = [price_evidence] + [_record_evidence(r, ticker) for r in chosen]
    events = []
    prior_titles: set[str] = set()
    for record, item in zip(chosen, evidence[1:]):
        title = item["title"]
        normalized_title = re.sub(r"\W+", " ", title.casefold()).strip()
        novelty = "REITERATION" if normalized_title in prior_titles else "NEW_RELEASE"
        prior_titles.add(normalized_title)
        guidance = _guidance(item["excerpt"], item["identifier"])
        is_result = bool(RESULT.search(title) and not SCHEDULED.search(title))
        metrics = _literal_metrics(item["excerpt"], item["identifier"]) if is_result else []
        guidance_ranges = _guidance_ranges(item["excerpt"], item["identifier"]) if is_result else []
        events.append({"kind": "RESULTS" if is_result else "CORPORATE_EVENT",
                       "published_at": item["published_at"], "summary": title,
                       "facts": {"novelty": novelty, "reported_metrics": metrics,
                                 "guidance_ranges": guidance_ranges,
                                 "guidance_language": guidance, "evidence_identifier": item["identifier"],
                                 "issuer_release": True},
                       "guidance": guidance["source_excerpt"] if guidance else None})
    document_coverage = load_document_coverage(ticker, as_of)
    if document_coverage:
        documents = document_coverage["documents"]
        structured = document_coverage.get("structured_facts", [])
        guidance_observations: list[dict[str, object]] = []
        primary_results = [item for item in documents
                           if item["document_kind"] == "SEC_EARNINGS_RELEASE"]
        latest_cached_result = max((event["published_at"] for event in events
                                    if event["kind"] == "RESULTS"), default="")
        latest_primary_result = max((item["published_at"] for item in primary_results), default="")
        if primary_results and latest_primary_result >= latest_cached_result:
            removed_ids = {event["facts"]["evidence_identifier"] for event in events
                           if event["kind"] == "RESULTS"}
            events = [event for event in events if event["kind"] != "RESULTS"]
            evidence = [item for item in evidence if item.get("identifier") not in removed_ids]
        for document in documents:
            evidence.append({
                "source": document["source"], "published_at": document["published_at"],
                "identifier": document["evidence_id"], "url": document["url"],
                "excerpt": document["excerpt"], "title": document["title"],
                "retrieved_at": document["retrieved_at"],
                "content_sha256": document["content_sha256"], "raw_path": document["local_path"],
                "published_at_precision": "day", "document_kind": document["document_kind"],
                "filing_date": document["filing_date"], "accepted_at": document["accepted_at"],
                "form": document["form"], "accession_number": document["accession_number"],
            })
            if document["document_kind"] == "SEC_EARNINGS_RELEASE":
                ranges = _guidance_ranges(document["excerpt"], document["evidence_id"])
                guidance_observations.extend({**item, "published_at": document["published_at"]}
                                             for item in ranges)
                metrics = _literal_metrics(document["excerpt"], document["evidence_id"])
                events.append({
                    "kind": "RESULTS", "published_at": document["published_at"],
                    "summary": document["title"],
                    "facts": {"novelty": "NEW_PRIMARY_DOCUMENT", "reported_metrics": metrics,
                              "guidance_ranges": ranges, "guidance_language": None,
                              "evidence_identifier": document["evidence_id"],
                              "primary_source": True},
                    "guidance": (ranges[0]["source_excerpt"] if ranges else None),
                })
            elif document["document_kind"] == "SEC_8_K_MATERIAL":
                events.append({
                    "kind": "CORPORATE_EVENT", "published_at": document["published_at"],
                    "summary": document["title"],
                    "facts": {"evidence_identifier": document["evidence_id"],
                              "primary_source": True}, "guidance": None,
                })
        for current in sorted(guidance_observations,
                              key=lambda item: str(item["published_at"]), reverse=True):
            previous = next((item for item in guidance_observations
                             if item["metric"] == current["metric"] and
                             item["period"] == current["period"] and
                             item["unit"] == current["unit"] and
                             item["published_at"] < current["published_at"]), None)
            if previous is None:
                continue
            old_range = previous["current_range"]
            new_range = current["current_range"]
            direction = ("RAISED" if new_range[0] >= old_range[0] and
                         new_range[1] >= old_range[1] and new_range != old_range else
                         "LOWERED" if new_range[0] <= old_range[0] and
                         new_range[1] <= old_range[1] and new_range != old_range else
                         "UNCHANGED" if new_range == old_range else "MIXED")
            change = {"metric": current["metric"], "period": current["period"],
                      "unit": current["unit"], "previous_range": old_range,
                      "current_range": new_range, "direction": direction,
                      "previous_evidence_identifier": previous["evidence_identifier"],
                      "current_evidence_identifier": current["evidence_identifier"],
                      "extraction_method": "deterministic_same_metric_period_unit_range_comparison"}
            target = next(event for event in events if
                          event["facts"].get("evidence_identifier") == current["evidence_identifier"])
            target["facts"].setdefault("guidance_changes", []).append(change)
            break
        periodic = next((item for item in documents
                         if item["document_kind"] == "SEC_PERIODIC_REPORT"), None)
        if periodic:
            events.append({
                "kind": "SEC_PERIODIC_REPORT", "published_at": periodic["published_at"],
                "summary": periodic["title"],
                "facts": {"structured_sec_facts": structured,
                          "evidence_identifier": periodic["evidence_id"],
                          "primary_source": True}, "guidance": None,
            })
    _attach_fact_ids(ticker, price, str(price_evidence["identifier"]), events)
    company_input = {"company_name": company, "ticker": ticker, "provider_symbol": f"{ticker}.US",
                     "as_of": as_of, "schema_type": sector, "price": price,
                     "fundamentals": {}, "financial_facts": {}, "events": events, "evidence": evidence}
    # Run the reused point-in-time/provenance validator before any LLM call.
    build_facts(company_input, as_of)
    limitations = (["SEC periodic report and earnings filings are primary but issuer facts remain unaudited",
                    "Some non-GAAP and guidance metrics are only available in issuer exhibits"]
                   if document_coverage else
                   ["No primary SEC/IR financial document in frozen cache",
                    "Guidance changes are literal language only; quantitative delta unsupported"])
    triage = {"issuer_releases_in_cache": len(records), "rejected_non_issuer_news": rejected,
              "selected_release_count": len(chosen), "selected_titles": [r["title"] for r in chosen],
              "sec_document_count": len(document_coverage["documents"]) if document_coverage else 0,
              "sec_structured_fact_count": len(document_coverage.get("structured_facts", [])) if document_coverage else 0,
              "limitations": limitations}
    return EvidencePack(ticker, as_of, company_input, triage)


class USAProvider(CodexCLIProvider):
    """Two existing structured LLM calls with a USA evidence contract."""

    def __init__(self) -> None:
        self.revisions: dict[str, object] = {}
        self.validated_claim_refs: list[dict[str, object]] = []
        self.rejected_claim_refs: list[dict[str, object]] = []
        self.critic_recommendation: tuple[str, str] | None = None
        self.legacy_numeric_failures: list[str] = []

    def analyze(self, facts: Mapping[str, object]) -> Mapping[str, object]:
        prompt = (
            "You are TRINITY USA V2 Analyst. Return only schema JSON, in Italian. "
            "Use only provided facts and cite [evidence identifier] for every material claim. "
            "Treat reported_metrics as literal issuer claims with explicit USD units, not audited SEC facts. "
            "Respect measurement_type: CHANGE_AMOUNT is a change, ANNUALIZED_RUN_RATE is not quarterly sales; "
            "REPORTED_AMOUNT still needs context from source_excerpt. "
            "Use guidance_ranges when comparing per-share guidance. Describe direction only; do not calculate "
            "an unstated numerical delta. "
            "Do not add arithmetic, figures, causality, valuation or guidance deltas absent from facts. "
            "For every quantitative claim add a claim_refs entry whose text is an exact substring of the "
            "narrative, whose fact_ids list contains only supplied fact_id values, and whose period matches "
            "the fact when stated. fact_ids are the authoritative numeric provenance; citations remain for "
            "rendering. Do not create a claim_ref for arithmetic absent from a supplied fact. "
            "Distinguish a new disclosure from a repeated statement; a scheduled results call is not a catalyst. "
            "Only list a catalyst if the event is concrete, future-relevant and evidenced. "
            "State uncertainty when the cache lacks underlying results or independent confirmation. "
            "Lower confidence when evidence is thin; status is research triage, not a trade signal. "
            "PASS means insufficient supported thesis and requires LOW confidence; WATCH means monitor; "
            "INVESTIGATE means concrete evidence merits more work. as_of is the historical cutoff.\nFACTS:\n"
            + json.dumps(facts, ensure_ascii=False, allow_nan=False, sort_keys=True)
        )
        return self._request(prompt, _USA_ANALYST_SCHEMA)

    def critique(self, facts: Mapping[str, object], draft: Mapping[str, object]) -> Mapping[str, object]:
        prompt = (
            "You are TRINITY USA V2 Critic. Return only schema JSON, in Italian. Audit each material "
            "claim against evidence identifiers, including every number and unit. Identify repeated news, "
            "weak or irrelevant catalysts, unsupported causal claims, overstrong conclusions, and guidance "
            "changes claimed without a comparable earlier source. Distinguish source-reported metrics from "
            "independently verified facts. In notes give specific corrections with evidence IDs. "
            "Return revised_analysis with all narrative fields corrected; remove unsupported claims and "
            "numbers, preserve source citations, and make invalidation criteria precise. For every retained "
            "quantitative claim, include a claim_refs entry with exact narrative text and supplied fact_ids. "
            "Never create a fact_id or bind a calculated value to component facts. Mark a claim MATERIAL only "
            "when removing it would materially weaken the recommended status or confidence. If evidence "
            "is thin or issuer release only, confidence cannot be HIGH. If material unsupported claims remain, "
            "return PASS/LOW. PASS always requires LOW. Status is research triage, not a trade signal.\nFACTS:\n"
            + json.dumps(facts, ensure_ascii=False, allow_nan=False, sort_keys=True)
            + "\nDRAFT:\n" + json.dumps(draft, ensure_ascii=False, allow_nan=False, sort_keys=True)
        )
        result = self._request(prompt, _USA_CRITIC_SCHEMA)
        revised = result["revised_analysis"]
        self.critic_recommendation = (str(result["status"]), str(result["confidence"]))
        known_ids = {str(item["identifier"]) for item in facts["evidence"]}
        for field in REVISION_FIELDS:
            if isinstance(revised[field], list):
                revised[field] = [_clean_citations(item, known_ids) for item in revised[field]]
            else:
                revised[field] = _clean_citations(revised[field], known_ids)
        result["notes"] = [_clean_citations(item, known_ids) for item in result["notes"]]
        self.legacy_numeric_failures = _numeric_audit(facts, revised)
        validation = validate_claim_refs(facts, revised)
        self.validated_claim_refs = validation.valid_claim_refs
        self.rejected_claim_refs = validation.rejected_claim_refs
        for rejected in validation.rejected_claim_refs:
            result["notes"].append(
                f"{rejected['claim_id']}: structured provenance rejected "
                f"{rejected['validation_errors']}"
            )
        if validation.essential_support_removed:
            result["status"] = "PASS"
            result["confidence"] = "LOW"
            result["notes"].append(
                "Status degraded to PASS/LOW because material rejected claims left fewer than two "
                "validated material claims backed by distinct facts."
            )
        self.revisions = {field: validation.revised_analysis[field] for field in REVISION_FIELDS}
        if result.get("status") == "PASS" and result.get("confidence") != "LOW":
            raise ValueError("PASS requires LOW confidence")
        if result.get("confidence") == "HIGH":
            result["confidence"] = "MEDIUM"
            result["notes"].append("Confidence capped at MEDIUM: cached issuer wire releases lack independently verified filing/IR financial statements.")
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run ten historical USA V2 thesis slices from frozen caches")
    parser.add_argument("--as-of", default=AS_OF)
    parser.add_argument("--output-dir", default="data/trinity_usa_v2")
    parser.add_argument("--ticker", choices=COMPANIES, nargs="+")
    args = parser.parse_args()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    # The Windows sandbox may omit the standard home variables inherited by codex.exe.
    os.environ.setdefault("USERPROFILE", str(DATASET.parents[2]))
    os.environ.setdefault("HOME", os.environ["USERPROFILE"])
    os.environ.setdefault("CODEX_HOME", str(Path(os.environ["USERPROFILE"]) / ".codex"))
    failures = 0
    for ticker in (args.ticker or COMPANIES):
        try:
            pack = load_company(ticker, args.as_of)
            provider = USAProvider()
            thesis = analyze_company(pack.company_input, args.as_of, provider)
            thesis = replace(thesis, **provider.revisions,
                             claim_refs=provider.validated_claim_refs,
                             rejected_claim_refs=provider.rejected_claim_refs)
            thesis_path = save_thesis(thesis, output)
            structured_fact_count = sum(
                len(event["facts"].get("structured_sec_facts", [])) +
                len(event["facts"].get("reported_metrics", []))
                for event in pack.company_input["events"]
            )
            guidance_fact_count = sum(
                len(event["facts"].get("guidance_ranges", [])) +
                len(event["facts"].get("guidance_changes", []))
                for event in pack.company_input["events"]
            )
            result = {"ticker": ticker, "last_close": pack.company_input["price"]["current_price"],
                      "price_date": pack.company_input["price"]["published_at"],
                      "evidence_count": len(thesis.evidence), "events": pack.triage["selected_titles"],
                      "structured_facts": structured_fact_count,
                      "guidance_facts": guidance_fact_count,
                      "critic_status": provider.critic_recommendation[0],
                      "critic_confidence": provider.critic_recommendation[1],
                      "status": thesis.status, "confidence": thesis.confidence,
                      "validated_claims": len(provider.validated_claim_refs),
                      "rejected_claims": len(provider.rejected_claim_refs),
                      "legacy_numeric_failures": len(provider.legacy_numeric_failures),
                      "blocked_derivations": sum(
                          "UNREGISTERED_DERIVATION_OR_VALUE" in item["validation_errors"]
                          for item in provider.rejected_claim_refs
                      ),
                      "critic_notes": thesis.critic_notes, "thesis_path": str(thesis_path)}
        except Exception as exc:
            failures += 1
            result = {"ticker": ticker, "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
